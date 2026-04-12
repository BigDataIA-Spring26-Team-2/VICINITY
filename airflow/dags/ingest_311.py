"""DAG: ingest_311 — Boston 311 complaints from CKAN.

Target: RAW.COMPLAINTS_311 | Source: CKAN datastore_search (3 resource variants)
Config: config/dags.yml → dags.ingest_311
"""

from airflow import DAG
from airflow.operators.bash import BashOperator

from dag_utils import (
    dag_config, default_args, PIPELINE_ENV, on_sla_miss,
    param_string, param_int, param_bool,
    value_flag, bool_flag,
)

cfg = dag_config("ingest_311")
flags = cfg["pipeline_flags"]

with DAG(
    dag_id="ingest_311",
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
        "mode":       param_string(flags.get("mode", "incremental"), "full | incremental"),
        "limit":      param_int(flags.get("limit"), "Cap extracted records"),
        "start_date": param_string(flags.get("start_date"), "Override watermark (YYYY-MM-DD)"),
        "end_date":   param_string(flags.get("end_date"), "Upper bound date (YYYY-MM-DD)"),
        "dry_run":    param_bool(flags.get("dry_run", False), "Extract + validate only, skip writes"),
    },
):
    BashOperator(
        task_id="load_311_complaints",
        bash_command=" ".join([
            "python -m app.pipelines.ingest_311",
            value_flag("mode", "--mode"),
            value_flag("limit", "--limit"),
            value_flag("start_date", "--start-date"),
            value_flag("end_date", "--end-date"),
            bool_flag("dry_run", "--dry-run"),
        ]),
        env=PIPELINE_ENV,
        append_env=True,
        sla=cfg["sla"],
    )
