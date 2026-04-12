"""DAG: ingest_listings — HomeHarvest MLS listings (primary).

Target: RAW.LISTINGS | Source: Realtor.com MLS via HomeHarvest
Config: config/dags.yml → dags.ingest_listings
"""

from airflow import DAG
from airflow.operators.bash import BashOperator

from dag_utils import (
    dag_config, default_args, PIPELINE_ENV, on_sla_miss,
    param_string, param_int, param_bool,
    value_flag, bool_flag,
)

cfg = dag_config("ingest_listings")
flags = cfg["pipeline_flags"]

with DAG(
    dag_id="ingest_listings",
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
        "mode":              param_string(flags.get("mode", "incremental"), "full | incremental"),
        "limit":             param_int(flags.get("limit"), "Cap extracted listings"),
        "start_date":        param_string(flags.get("start_date"), "Override watermark (YYYY-MM-DD)"),
        "end_date":          param_string(flags.get("end_date"), "Upper bound date (YYYY-MM-DD)"),
        "dry_run":           param_bool(flags.get("dry_run", False), "Extract + validate only, skip writes"),
        "location":          param_string(flags.get("location"), "Single city override (e.g. 'Boston, MA')"),
        "past_days":         param_int(flags.get("past_days"), "Listing recency window in days"),
        "min_price":         param_int(flags.get("min_price"), "Minimum listing price"),
        "max_price":         param_int(flags.get("max_price"), "Maximum listing price"),
        "skip_deactivation": param_bool(flags.get("skip_deactivation", False), "Skip marking unseen listings inactive"),
    },
):
    BashOperator(
        task_id="load_mls_listings",
        bash_command=" ".join([
            "python -m app.pipelines.ingest_listings",
            value_flag("mode", "--mode"),
            value_flag("limit", "--limit"),
            value_flag("start_date", "--start-date"),
            value_flag("end_date", "--end-date"),
            bool_flag("dry_run", "--dry-run"),
            value_flag("location", "--location"),
            value_flag("past_days", "--past-days"),
            value_flag("min_price", "--min-price"),
            value_flag("max_price", "--max-price"),
            bool_flag("skip_deactivation", "--skip-deactivation"),
        ]),
        env=PIPELINE_ENV,
        append_env=True,
        sla=cfg["sla"],
    )
