"""DAG: ingest_transit — MBTA transit stops and routes.

Target: RAW.TRANSIT_STOPS | Source: MBTA v3 API
Config: config/dags.yml → dags.ingest_transit
"""

from airflow import DAG
from airflow.operators.bash import BashOperator

from dag_utils import (
    dag_config, default_args, PIPELINE_ENV, on_sla_miss,
    param_string, param_bool, param_list,
    value_flag, bool_flag, list_flag,
)

cfg = dag_config("ingest_transit")
flags = cfg["pipeline_flags"]

with DAG(
    dag_id="ingest_transit",
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
        "mode":        param_string(flags.get("mode", "full"), "full | incremental"),
        "dry_run":     param_bool(flags.get("dry_run", False), "Extract + validate only, skip writes"),
        "route_types": param_list(flags.get("route_types"), "Route types [0=LRT, 1=Heavy, 2=CR]"),
    },
):
    BashOperator(
        task_id="load_transit_stops",
        bash_command=" ".join([
            "python -m app.pipelines.ingest_transit",
            value_flag("mode", "--mode"),
            bool_flag("dry_run", "--dry-run"),
            list_flag("route_types", "--route-types"),
        ]),
        env=PIPELINE_ENV,
        append_env=True,
        sla=cfg["sla"],
    )
