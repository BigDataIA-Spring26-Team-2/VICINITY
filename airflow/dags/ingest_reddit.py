"""DAG: ingest_reddit — Reddit neighbourhood intelligence signals.

Target: RAW.LIFESTYLE_SIGNALS | Source: Reddit public JSON API
Config: config/dags.yml → dags.ingest_reddit_livability (base config)

Standalone DAG for isolated testing.  Accepts category param to run
livability or lifestyle partition.  The master DAG runs both partitions
as separate tasks (ingest_reddit_livability, ingest_reddit_lifestyle).

Usage (Airflow UI trigger):
    {"category": "livability", "dry_run": true}
    {"category": "lifestyle"}
    {"preference_tag": "safety", "query": "safe at night"}
"""

from airflow import DAG
from airflow.operators.bash import BashOperator

from dag_utils import (
    dag_config, default_args, PIPELINE_ENV, on_sla_miss,
    param_string, param_bool,
    value_flag, bool_flag,
)

# Use livability config as base — same module, retries, timeouts
cfg = dag_config("ingest_reddit_livability")
flags = cfg.get("pipeline_flags", {})

with DAG(
    dag_id="ingest_reddit",
    default_args=default_args(cfg),
    description="Reddit signals — standalone (livability or lifestyle via category param)",
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
        "query":     param_string(None, "Single query override"),
        "subreddit": param_string(None, "Force single subreddit"),
        "dry_run":   param_bool(flags.get("dry_run", False), "Extract + classify only"),
    },
):
    validate = BashOperator(
        task_id="validate_reddit_config",
        bash_command="python -m app.pipelines.validate_reddit_config",
        env=PIPELINE_ENV,
        append_env=True,
    )

    load = BashOperator(
        task_id="load_reddit_signals",
        bash_command=" ".join([
            "python -m app.pipelines.ingest_reddit",
            value_flag("category", "--category"),
            value_flag("preference_tag", "--preference-tag"),
            value_flag("query", "--query"),
            value_flag("subreddit", "--subreddit"),
            bool_flag("dry_run", "--dry-run"),
        ]),
        env=PIPELINE_ENV,
        append_env=True,
        sla=cfg["sla"],
    )

    validate >> load