import os
import glob
import papermill as pm
import boto3
import json
import requests
import tempfile
import logging
from datetime import timedelta
from jinja2 import Environment, FileSystemLoader, select_autoescape


import airflow
from airflow import DAG
from airflow.contrib.hooks.snowflake_hook import SnowflakeHook
from airflow.utils.dates import days_ago
from airflow.operators.python_operator import PythonOperator
from airflow.contrib.operators.snowflake_operator import SnowflakeOperator
from airflow.operators.dummy_operator import DummyOperator
from airflow.models import Variable
from airflow.configuration import conf


DAGS_FOLDER = conf.get('core', 'dags_folder')
NOTEBOOKS_FOLDER = os.path.abspath(
    conf.get('core', 'dags_folder') + "/../notebooks") + "/"
OUTPUT_FOLDER = os.path.abspath(
    conf.get('core', 'dags_folder') + "/../output") + "/"
QA_FOLDER = os.path.abspath(
    conf.get('core', 'dags_folder') + "/../snowflake/qa") + "/"
SQL_FOLDER = os.path.abspath(
    conf.get('core', 'dags_folder') + "/../snowflake/sql") + "/"
GIT_USER = Variable.get('GIT_USER', default_var=None)
GIT_TOKEN = Variable.get('GIT_TOKEN', default_var=None)
ENVIRONMENT = Variable.get('ENVIRONMENT', default_var=None)

with open(DAGS_FOLDER + "/../refresh_schedules.json", 'r') as f:
    schedules = json.load(f)


with open(SQL_FOLDER + "template_params.json", "r") as f:
    template_params = json.load(f)

# create jinja env
env = Environment(
    loader=FileSystemLoader(SQL_FOLDER),
    autoescape=select_autoescape(['sql'])
)


def create_etl_dag(dag_id, args):

    # notebook name without extension, for example "JHU_COVID-19"
    basename = args.get('basename')

    # notebook file, this will be executed, for example: /home/ec2-user/COVID-19-data/notebooks/JHU_COVID-19.ipynb
    notebook_file = NOTEBOOKS_FOLDER + basename + ".ipynb"

    # the csv file location which wil be generated by the notebook, for example: /home/ec2-user/output/JHU_COVID-19.csv
    output_file_glob = OUTPUT_FOLDER + basename + '*'

    # the QA sql file to perform checks , for example: /home/ec2-user/COVID-19-data/snowflake/qa/JHU_COVID-19_QA.sql
    qa_file = QA_FOLDER + basename + "_QA.sql"

    # the QA sql file to perform checks , for example: /home/ec2-user/COVID-19-data/snowflake/sql/JHU_COVID-19_HIST.sql
    sql_file_glob = SQL_FOLDER + basename + "*.sql"

    dag = DAG(
        dag_id=dag_id,
        default_args=args,
        max_active_runs=1,
        schedule_interval=schedules["recurring"].get(basename, None),
        dagrun_timeout=timedelta(minutes=60)
    )

    with dag:
        start = DummyOperator(
            task_id='start',
            dag=dag
        )

        def clean_generated_files():
            for output_file in glob.glob(output_file_glob):
                if os.path.exists(output_file):
                    os.remove(output_file)

        def execute_notebook():
            pm.execute_notebook(
                input_path=notebook_file,
                output_path="/dev/null",
                parameters=dict({
                    "output_folder": OUTPUT_FOLDER,
                    "GIT_USER": GIT_USER,
                    "GIT_TOKEN": GIT_TOKEN}),
                log_output=True,
                report_mode=True
            )
            return

        def upload_to_s3():
            """Upload a file to an S3 bucket

            :param basename
            :return: True if file was uploaded, else False
            """
            response = False

            s3_client = boto3.client('s3', aws_access_key_id=Variable.get("AWS_ACCESS_KEY_ID"),
                                     aws_secret_access_key=Variable.get("AWS_SECRET_ACCESS_KEY"))
            for output_file in glob.glob(output_file_glob):
                s3_file_name = os.path.basename(output_file)
                response = s3_client.upload_file(output_file,
                                                 Variable.get("S3_BUCKET"),
                                                 s3_file_name)

            return response

        def make_github_issue(title, body=None, labels=None):
            url = 'https://api.github.com/repos/starschema/COVID-19-data/issues'
            ses = requests.session()
            ses.auth = (GIT_USER, GIT_TOKEN)
            issue = {'title': title,
                     'body': body,
                     'labels': labels}
            r = ses.post(url, json.dumps(issue))
            if r.status_code == 201:
                logger.info('Successfully created Issue "%s"' % title)
            else:
                logger.error('Could not create Issue "%s"' % title)
                logger.info('Response:', r.content)

        def upload_to_snowflake(task_id):
            sql_statements = []

            # look for CSVs to ignest to snowflake
            for output_file in glob.glob(output_file_glob + ".csv"):
                s3_file_name = os.path.basename(output_file)
                tablename = os.path.splitext(s3_file_name)[0].replace("-", "_")
                snowflake_stage = Variable.get(
                    "SNOWFLAKE_STAGE", default_var="COVID_PROD")

                truncate_st = f'TRUNCATE TABLE {tablename}'
                insert_st = f'copy into {tablename} from @{snowflake_stage}/{s3_file_name} file_format = (type = "csv" field_delimiter = "," NULL_IF = (\'NULL\', \'null\',\'\') EMPTY_FIELD_AS_NULL = true FIELD_OPTIONALLY_ENCLOSED_BY=\'"\' skip_header = 1)'
                sql_statements.append(truncate_st)
                sql_statements.append(insert_st)

            sql_statements.append("COMMIT")

            create_insert_task = SnowflakeOperator(
                task_id=task_id,
                sql=sql_statements,
                autocommit=False,
                snowflake_conn_id=Variable.get(
                    "SNOWFLAKE_CONNECTION", default_var="SNOWFLAKE"),
            )

            return create_insert_task

        def qa_checks(**context):
            if os.path.exists(qa_file) and GIT_USER is not None and GIT_TOKEN is not None and ENVIRONMENT != 'CI':
                with open(qa_file, 'r') as fd:
                    sqlfile = fd.read()
                    sqllist = sqlfile.split(";")
                    sf_hook = SnowflakeHook(snowflake_conn_id=Variable.get(
                        "SNOWFLAKE_CONNECTION", default_var="SNOWFLAKE"))
                    for sql in sqllist:
                        if len(sql.strip()) > 5:
                            result = sf_hook.get_pandas_df(sql)
                            if len(result.index) > 0:
                                for index, row in result.iterrows():
                                    make_github_issue('QA Failed for ' + row['TABLE_NAME'],
                                                      "Error: " + row['ERROR_DESC'] + "\n" + "Error Count: " + str(
                                                          row['ERROR_COUNT']) + "\n" + row['ERROR_CONDITION'],
                                                      ['bug', 'qa'])

        def create_dynamic_etl(task_id, callable_function):
            task = PythonOperator(
                task_id=task_id,
                python_callable=callable_function,
                dag=dag,
            )
            return task

        end = DummyOperator(
            task_id='end',
            dag=dag)

        def execute_script(sql_file):
            script = env.get_template(sql_file).render(**template_params)
            sql_statements = script.split(";")
            return SnowflakeOperator(
                task_id="execute_script_" + sql_file,
                sql=sql_statements,
                autocommit=False,
                snowflake_conn_id=Variable.get(
                    "SNOWFLAKE_CONNECTION", default_var="SNOWFLAKE"),
            )

        cleanup_output_folder_task = create_dynamic_etl(
            'cleanup', clean_generated_files)

        execute_notebook_task = create_dynamic_etl(
            'execute_notebook', execute_notebook)

        upload_to_s3_task = create_dynamic_etl('upload_to_s3', upload_to_s3)

        upload_to_snowflake_task = upload_to_snowflake('upload_to_snowflake')

        # Execution flow & dependencies
        start >> cleanup_output_folder_task
        cleanup_output_folder_task >> execute_notebook_task
        execute_notebook_task >> upload_to_s3_task
        upload_to_s3_task >> upload_to_snowflake_task
        if not len(sql_file_glob):
            upload_to_snowflake_task >> end
        else:
            for sql_file in sql_file_glob:
                execute_script_task = execute_script(sql_file)
                upload_to_snowflake_task >> execute_script_task
                execute_script_task >> end

        return dag


#  Look for python notebooks to execute
for file in os.listdir(NOTEBOOKS_FOLDER):
    if file.startswith("."):
        continue
    filename_without_extension = os.path.splitext(file)[0]
    dag_id = 'etl_{}'.format(str(filename_without_extension))

    default_args = {'owner': 'admin',
                    'start_date': days_ago(2),
                    'basename': filename_without_extension
                    }
    globals()[dag_id] = create_etl_dag(dag_id, default_args)
