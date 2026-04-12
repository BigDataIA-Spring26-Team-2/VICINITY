"""DAG: ingest_listings_craigslist — Craigslist fallback listings.

Target: RAW.LISTINGS | Source: boston.craigslist.org via Scrapling
Config: config/dags.yml → dags.ingest_listings_craigslist
"""

from airflow import DAG
from airflow.operators.bash import BashOperator

from dag_utils import (
    dag_config, default_args, PIPELINE_ENV, on_sla_miss,
    param_string, param_int, param_bool,
    value_flag, bool_flag,
)

cfg = dag_config("ingest_listings_craigslist")
flags = cfg["pipeline_flags"]

with DAG(
    dag_id="ingest_listings_craigslist",
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
        "limit":       param_int(flags.get("limit", 200), "Cap listings per run"),
        "dry_run":     param_bool(flags.get("dry_run", False), "Extract + validate only, skip writes"),
        "min_price":   param_int(flags.get("min_price"), "Minimum listing price"),
        "max_price":   param_int(flags.get("max_price"), "Maximum listing price"),
        "delay":       param_string(flags.get("delay"), "Seconds between fetches (float)"),
        "no_headless": param_bool(flags.get("no_headless", False), "Visible browser mode (debugging)"),
    },
):
    BashOperator(
        task_id="load_craigslist_listings",
        bash_command=" ".join([
            "python -m app.pipelines.ingest_listings_craigslist",
            value_flag("mode", "--mode"),
            value_flag("limit", "--limit"),
            bool_flag("dry_run", "--dry-run"),
            value_flag("min_price", "--min-price"),
            value_flag("max_price", "--max-price"),
            value_flag("delay", "--delay"),
            bool_flag("no_headless", "--no-headless"),
        ]),
        env=PIPELINE_ENV,
        append_env=True,
        sla=cfg["sla"],
    )
