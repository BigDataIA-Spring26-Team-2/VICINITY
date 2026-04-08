FROM apache/airflow:2.10.4-python3.12

USER airflow

RUN pip install --no-cache-dir \
    snowflake-connector-python \
    snowflake-sqlalchemy \
    httpx \
    pandas \
    python-dotenv \
    homeharvest