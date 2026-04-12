FROM apache/airflow:2.10.4-python3.12

# ── System deps for C extensions (snowflake, cryptography, scrapling) ──
USER root
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc g++ git curl && \
    rm -rf /var/lib/apt/lists/*

USER airflow

# ── Copy pinned requirements (from project venv, conflict-resolved) ──
COPY requirements.txt /tmp/requirements.txt

# 1. Install pipeline dependencies
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# 2. Re-pin Airflow-critical deps LAST
#    snowflake-connector and openai can bump SQLAlchemy/click.
#    This step forces everything back to the exact versions
#    that ship with apache/airflow:2.10.4-python3.12.
RUN pip install --no-cache-dir \
    'SQLAlchemy==1.4.54' \
    'click==8.1.7' \
    'celery==5.4.0' \
    'kombu==5.4.2'

# 3. Scrapling needs Playwright browsers for headless scraping
#    Install playwright python package, then system deps (as root), then browsers (as airflow)
RUN pip install --no-cache-dir playwright
USER root
RUN playwright install-deps chromium
USER airflow
RUN playwright install chromium