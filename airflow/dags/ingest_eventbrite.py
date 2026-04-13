"""DAG: ingest_eventbrite — Eventbrite lifestyle signals.

Target: RAW.LIFESTYLE_SIGNALS | Source: eventbrite.com search HTML
Config: config/dags.yml → dags.ingest_eventbrite

Triggered per preference tag. Pass {"preference_tag": "live_music"}
via dag_run.conf or Airflow UI params.
"""

from airflow import DAG
from airflow.operators.bash import BashOperator

from dag_utils import (
    dag_config, default_args, PIPELINE_ENV, on_sla_miss,
    param_string, param_int, param_bool,
    value_flag, bool_flag,
)

cfg = dag_config("ingest_eventbrite")
flags = cfg["pipeline_flags"]

with DAG(
    dag_id="ingest_eventbrite",
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
            "Preference tag to query (e.g. live_music)",
        ),
        "query":    param_string(flags.get("query"), "Custom search slug override"),
        "pages":    param_int(flags.get("pages", 1), "Result pages to fetch"),
        "dry_run":  param_bool(flags.get("dry_run", False), "Extract + validate only"),
    },
):
    BashOperator(
        task_id="load_eventbrite_signals",
        bash_command=" ".join([
            "python -m app.pipelines.ingest_eventbrite",
            value_flag("preference_tag", "--preference-tag"),
            value_flag("query", "--query"),
            value_flag("pages", "--pages"),
            bool_flag("dry_run", "--dry-run"),
        ]),
        env=PIPELINE_ENV,
        append_env=True,
        sla=cfg["sla"],
    )
