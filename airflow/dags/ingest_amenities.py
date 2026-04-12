"""DAG: ingest_amenities — Overpass OSM amenities.

Target: RAW.AMENITIES | Source: Overpass API (35 subcategories)
Config: config/dags.yml → dags.ingest_amenities
"""

from airflow import DAG
from airflow.operators.bash import BashOperator

from dag_utils import (
    dag_config, default_args, PIPELINE_ENV, on_sla_miss,
    param_string, param_bool, param_list,
    value_flag, bool_flag, list_flag,
)

cfg = dag_config("ingest_amenities")
flags = cfg["pipeline_flags"]

with DAG(
    dag_id="ingest_amenities",
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
        "mode":       param_string(flags.get("mode", "full"), "full | incremental"),
        "dry_run":    param_bool(flags.get("dry_run", False), "Extract + validate only, skip writes"),
        "categories": param_list(flags.get("categories"), "Subset of subcategories to fetch"),
    },
):
    BashOperator(
        task_id="load_amenities",
        bash_command=" ".join([
            "python -m app.pipelines.ingest_amenities",
            value_flag("mode", "--mode"),
            bool_flag("dry_run", "--dry-run"),
            list_flag("categories", "--categories"),
        ]),
        env=PIPELINE_ENV,
        append_env=True,
        sla=cfg["sla"],
    )
