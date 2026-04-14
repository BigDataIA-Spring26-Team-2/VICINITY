"""DAG: ingest_google_news — Google News neighbourhood intelligence signals.

Target: RAW.LIFESTYLE_SIGNALS | Source: Google News RSS + article extraction
Config: config/dags.yml → dags.ingest_google_news

Single task — no config validation needed (RSS feeds are public, queries
are just search strings, no subreddit-like entities to prune).

Triggered per preference tag. Pass {"preference_tag": "safety"}
via dag_run.conf or Airflow UI params.  Optional overrides:
  {"query": "boston crime", "dry_run": true}
"""

from airflow import DAG
from airflow.operators.bash import BashOperator

from dag_utils import (
    dag_config, default_args, PIPELINE_ENV, on_sla_miss,
    param_string, param_bool,
    value_flag, bool_flag,
)

cfg = dag_config("ingest_google_news")
flags = cfg["pipeline_flags"]

with DAG(
    dag_id="ingest_google_news",
    default_args=default_args(cfg),
    description=cfg["description"],
    schedule=cfg["schedule"],
    start_date=cfg["start_date"],
    catchup=cfg.get("catchup", False),
    max_active_runs=cfg.get("max_active_runs", 1),
    dagrun_timeout=cfg["dagrun_timeout"],
    tags=cfg.get("tags", ["vicinity", "ingest"]),
    sla_miss_callback=on_sla_miss if cfg["sla"] else None,
    params={
        "preference_tag": param_string(
            flags.get("preference_tag", ""),
            "Tag to query (e.g. safety, korean_food)",
        ),
        "query":    param_string(flags.get("query"), "Single query override"),
        "dry_run":  param_bool(flags.get("dry_run", False), "Extract + classify only"),
    },
):
    BashOperator(
        task_id="load_google_news_signals",
        bash_command=" ".join([
            "python -m app.pipelines.ingest_google_news",
            value_flag("preference_tag", "--preference-tag"),
            value_flag("query", "--query"),
            bool_flag("dry_run", "--dry-run"),
        ]),
        env=PIPELINE_ENV,
        append_env=True,
        sla=cfg["sla"],
    )
