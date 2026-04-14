"""DAG: ingest_reddit — Reddit neighbourhood intelligence signals.

Target: RAW.LIFESTYLE_SIGNALS | Source: Reddit public JSON API
Config: config/dags.yml → dags.ingest_reddit

Two tasks:
  1. validate_reddit_config — prune dead subreddits from reddit.yml
  2. load_reddit_signals    — search, classify, load

Triggered per preference tag. Pass {"preference_tag": "safety"}
via dag_run.conf or Airflow UI params.  Optional overrides:
  {"query": "safe at night", "subreddit": "boston", "dry_run": true}
"""

from airflow import DAG
from airflow.operators.bash import BashOperator

from dag_utils import (
    dag_config, default_args, PIPELINE_ENV, on_sla_miss,
    param_string, param_bool,
    value_flag, bool_flag,
)

cfg = dag_config("ingest_reddit")
flags = cfg["pipeline_flags"]

with DAG(
    dag_id="ingest_reddit",
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
            "Tag to query (e.g. safety, live_music)",
        ),
        "query":     param_string(flags.get("query"), "Single query override"),
        "subreddit": param_string(flags.get("subreddit"), "Force single subreddit"),
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
