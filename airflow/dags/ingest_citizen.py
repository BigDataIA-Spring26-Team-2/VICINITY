"""DAG: ingest_citizen — Citizen App real-time incidents.

Target: RAW.CITIZEN_INCIDENTS | Source: citizen.com/api/incident/trending
Config: config/dags.yml → dags.ingest_citizen
"""

from airflow import DAG
from airflow.operators.bash import BashOperator

from dag_utils import (
    dag_config, default_args, PIPELINE_ENV, on_sla_miss,
    param_string, param_int, param_bool,
    value_flag, bool_flag,
)

cfg = dag_config("ingest_citizen")
flags = cfg["pipeline_flags"]

with DAG(
    dag_id="ingest_citizen",
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
        "mode":    param_string(flags.get("mode", "full"), "full | incremental"),
        "limit":   param_int(flags.get("limit"), "Cap fetched incidents"),
        "dry_run": param_bool(flags.get("dry_run", False), "Extract + validate only, skip writes"),
    },
):
    BashOperator(
        task_id="load_citizen_incidents",
        bash_command=" ".join([
            "python -m app.pipelines.ingest_citizen",
            value_flag("mode", "--mode"),
            value_flag("limit", "--limit"),
            bool_flag("dry_run", "--dry-run"),
        ]),
        env=PIPELINE_ENV,
        append_env=True,
        sla=cfg["sla"],
    )
