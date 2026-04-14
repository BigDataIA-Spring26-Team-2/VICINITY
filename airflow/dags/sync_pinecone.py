"""DAG: sync_pinecone — embed lifestyle signals to Pinecone.

Runs after all lifestyle ingest DAGs complete.
Config: config/dags.yml → dags.sync_pinecone
"""

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.models.param import Param

from dag_utils import (
    dag_config, default_args, PIPELINE_ENV,
    param_string, param_bool, param_int,
    value_flag, bool_flag,
    on_success, on_failure, on_retry,
)

cfg = dag_config("sync_pinecone")
flags = cfg.get("pipeline_flags", {})

with DAG(
    dag_id="sync_pinecone",
    description=cfg.get("description", ""),
    schedule=cfg.get("schedule"),
    default_args=default_args(cfg),
    max_active_runs=cfg.get("max_active_runs", 1),
    catchup=False,
    tags=cfg.get("tags", []),
    params={
        "preference_tag": param_string(
            flags.get("preference_tag"),
            "Sync only this tag (e.g. safety, korean_food)",
        ),
        "batch_size": param_int(
            flags.get("batch_size"),
            "Override embedding batch size",
        ),
        "force_reembed": param_bool(
            flags.get("force_reembed", False),
            "Re-embed all signals ignoring content_hash",
        ),
        "gc": param_bool(
            flags.get("gc", False),
            "Run garbage collection (delete orphan vectors)",
        ),
        "dry_run": param_bool(
            flags.get("dry_run", False),
            "Embed but skip Pinecone upsert and sync update",
        ),
    },
) as dag:
    BashOperator(
        task_id="sync_pinecone",
        bash_command=" ".join(filter(None, [
            "python -m app.pipelines.sync_pinecone",
            value_flag("preference_tag", "--preference-tag"),
            value_flag("batch_size", "--batch-size"),
            bool_flag("force_reembed", "--force-reembed"),
            bool_flag("gc", "--gc"),
            bool_flag("dry_run", "--dry-run"),
        ])),
        env=PIPELINE_ENV,
        on_success_callback=on_success,
        on_failure_callback=on_failure,
        on_retry_callback=on_retry,
    )