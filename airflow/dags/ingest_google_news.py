"""DAG: ingest_google_news — Google News neighbourhood intelligence signals.

Target: RAW.LIFESTYLE_SIGNALS | Source: Google News RSS + article extraction
Config: config/dags.yml → dags.ingest_google_news_livability (base config)

Standalone DAG for isolated testing.  Accepts category param to run
livability or lifestyle partition.  The master DAG runs both partitions
as separate tasks (ingest_google_news_livability, ingest_google_news_lifestyle).

Usage (Airflow UI trigger):
    {"category": "livability", "dry_run": true}
    {"category": "lifestyle"}
    {"preference_tag": "safety", "query": "boston crime"}
"""

from airflow import DAG
from airflow.operators.bash import BashOperator

from dag_utils import (
    dag_config, default_args, PIPELINE_ENV, on_sla_miss,
    param_string, param_bool,
    value_flag, bool_flag,
)

# Use livability config as base — same module, retries, timeouts
cfg = dag_config("ingest_google_news_livability")
flags = cfg.get("pipeline_flags", {})

with DAG(
    dag_id="ingest_google_news",
    default_args=default_args(cfg),
    description="Google News signals — standalone (livability or lifestyle via category param)",
    schedule=None,
    start_date=cfg["start_date"],
    catchup=False,
    max_active_runs=cfg.get("max_active_runs", 1),
    dagrun_timeout=cfg["dagrun_timeout"],
    tags=cfg.get("tags", ["vicinity", "ingest", "lifestyle"]),
    sla_miss_callback=on_sla_miss if cfg["sla"] else None,
    params={
        "category": param_string(
            flags.get("category"),
            "livability | lifestyle (omit to run all tags)",
        ),
        "preference_tag": param_string(None, "Single tag override (e.g. safety)"),
        "query":   param_string(None, "Single query override"),
        "dry_run": param_bool(flags.get("dry_run", False), "Extract + classify only"),
    },
):
    BashOperator(
        task_id="load_google_news_signals",
        bash_command=" ".join([
            "python -m app.pipelines.ingest_google_news",
            value_flag("category", "--category"),
            value_flag("preference_tag", "--preference-tag"),
            value_flag("query", "--query"),
            bool_flag("dry_run", "--dry-run"),
        ]),
        env=PIPELINE_ENV,
        append_env=True,
        sla=cfg["sla"],
    )