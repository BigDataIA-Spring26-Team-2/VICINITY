"""Vicinity master DAG — dynamic orchestration from dags.yml.

Reads phase/command/pool/required from config. Generates tasks
per phase with gated transitions. Adding a new pipeline to
dags.yml with schedule: null and a phase number auto-includes
it in orchestration. No code changes required.

Phase 1: Ingest (parallel, independent failure)
Phase 2: Sync   (waits for phase 1 gate, skips if no new data)
Phase 3: Score  (waits for phase 2 completion)

Preflight validates infrastructure before any tasks launch.
Gates enforce minimum success count and required-task checks.
Each task supports skip via dag_run.conf: {"skip_<name>": true}.
Master-level dry_run propagates --dry-run to all tasks.

Slack: summary only. No per-task notifications from master runs.
Individual DAGs retain their own callbacks for manual triggers.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import yaml
from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator, ShortCircuitOperator
from airflow.models.param import Param
from airflow.utils.trigger_rule import TriggerRule

from dag_utils import (
    PIPELINE_ENV,
    on_failure, on_sla_miss,
    _post_slack, _slack_cfg,
)

log = logging.getLogger(__name__)

# ── Load config ──────────────────────────────────────────────

_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "dags.yml"
with open(_CONFIG_PATH) as _f:
    _RAW = yaml.safe_load(_f)

_DEFAULTS = _RAW.get("defaults", {})
_MASTER = _RAW.get("master", {})
_DAGS = _RAW.get("dags", {})
_NOTIFICATIONS = _RAW.get("notifications", {})
_GATES = _MASTER.get("gates", {})


# ── Helpers ──────────────────────────────────────────────────

def _get_master_managed() -> dict[int, list[tuple[str, dict]]]:
    """Group master-managed tasks by phase.

    schedule: null  -> master-managed
    schedule: cron  -> independent, excluded
    no phase field  -> excluded
    """
    phases: dict[int, list[tuple[str, dict]]] = {}
    for name, cfg in _DAGS.items():
        if cfg.get("schedule") is not None:
            continue
        phase = cfg.get("phase")
        if phase is None:
            continue
        phases.setdefault(phase, []).append((name, cfg))
    return phases


def _build_command(name: str, cfg: dict) -> str:
    """Build CLI command from module path and pipeline_flags.

    Master-level dry_run injects --dry-run into every command.
    Per-task dry_run flag is ignored — master controls it.
    """
    module = cfg["command"]
    parts = [f"python -m {module}"]

    # Master dry-run override
    parts.append(
        "{% if params.dry_run %}"
        "--dry-run"
        "{% endif %}"
    )

    flags = cfg.get("pipeline_flags", {})
    for flag_name, default_val in flags.items():
        if flag_name == "dry_run":
            continue

        cli_flag = f"--{flag_name.replace('_', '-')}"
        if isinstance(default_val, bool):
            parts.append(
                "{% if params." + flag_name + " %}" + cli_flag + "{% endif %}"
            )
        elif default_val is not None:
            jinja_val = "{{ params." + flag_name + " }}"
            parts.append(f"{cli_flag} {jinja_val}")
        else:
            parts.append(
                "{% if params." + flag_name + " %}"
                + cli_flag + " {{ params." + flag_name + " }}"
                + "{% endif %}"
            )
    return " ".join(parts)


def _build_bash_command(name: str, cfg: dict) -> str:
    """Wrap pipeline command with skip-flag check."""
    cmd = _build_command(name, cfg)
    return (
        "{% if params.skip_" + name + " %}"
        "echo 'SKIPPED by trigger param' && exit 0"
        "{% else %}"
        + cmd +
        "{% endif %}"
    )


def _build_task(name: str, cfg: dict) -> BashOperator:
    """Create a BashOperator from config.

    No Slack callbacks. Master DAG posts summary only.
    """
    return BashOperator(
        task_id=name,
        bash_command=_build_bash_command(name, cfg),
        env=PIPELINE_ENV,
        retries=cfg.get("retries", _DEFAULTS.get("retries", 2)),
        retry_delay=timedelta(
            minutes=cfg.get("retry_delay_min", _DEFAULTS.get("retry_delay_min", 5))
        ),
        execution_timeout=timedelta(
            minutes=cfg.get("dagrun_timeout_min", 60)
        ),
        pool=cfg.get("pool", "default_pool"),
        trigger_rule=TriggerRule.ALL_DONE,
        sla=timedelta(minutes=cfg["sla_min"]) if cfg.get("sla_min") else None,
        on_failure_callback=on_failure,
    )


# ── Preflight ────────────────────────────────────────────────

def _preflight(**context):
    """Infrastructure readiness before launching pipeline tasks.

    Catches issues that cause cryptic failures across tasks.
    Data state is checked in the ingest gate, not here.
    Warms the Snowflake warehouse so phase 1 skips cold-start.
    """
    import snowflake.connector
    import httpx
    from app.config import get_settings

    errors = []
    settings = get_settings()

    # Snowflake connection + warehouse warm-up
    try:
        conn = snowflake.connector.connect(
            account=settings.snowflake_account,
            user=settings.snowflake_user,
            password=settings.snowflake_password.get_secret_value(),
            database=settings.snowflake_database,
            warehouse=settings.snowflake_warehouse,
            role=settings.snowflake_role,
        )
        cursor = conn.cursor()
        cursor.execute("SELECT CURRENT_TIMESTAMP()")
        cursor.close()
        conn.close()
    except Exception as e:
        errors.append(f"snowflake: {str(e)[:100]}")

    # Required schemas
    try:
        conn = snowflake.connector.connect(
            account=settings.snowflake_account,
            user=settings.snowflake_user,
            password=settings.snowflake_password.get_secret_value(),
            database=settings.snowflake_database,
            warehouse=settings.snowflake_warehouse,
            role=settings.snowflake_role,
        )
        cursor = conn.cursor()
        for schema in ("RAW", "SCORECARDS", "USER_DATA"):
            cursor.execute(
                f"SELECT 1 FROM INFORMATION_SCHEMA.SCHEMATA "
                f"WHERE SCHEMA_NAME = '{schema}'"
            )
            if not cursor.fetchone():
                errors.append(f"missing schema: {schema}")
        cursor.close()
        conn.close()
    except Exception as e:
        errors.append(f"schema_check: {str(e)[:100]}")

    # API keys present
    key_checks = {
        "DEEPSEEK_API_KEY": "LLM classification",
        "OPENAI_API_KEY": "embeddings",
        "PINECONE_API_KEY": "vector sync",
    }
    for env_var, purpose in key_checks.items():
        if not os.getenv(env_var, "").strip():
            errors.append(f"missing {env_var} ({purpose})")

    # Playwright chromium importable
    try:
        result = subprocess.run(
            ["python", "-c", "from playwright.sync_api import sync_playwright"],
            capture_output=True, timeout=10,
        )
        if result.returncode != 0:
            errors.append("playwright not importable")
    except Exception:
        errors.append("playwright check failed")

    # External API reachability (DNS + TCP only)
    endpoints = {
        "data.boston.gov": "https://data.boston.gov",
        "citizen.com": "https://citizen.com",
        "api.deepseek.com": "https://api.deepseek.com",
    }
    for name, url in endpoints.items():
        try:
            with httpx.Client(timeout=5.0) as client:
                client.head(url)
        except Exception:
            errors.append(f"unreachable: {name}")

    # Worker disk space
    disk = shutil.disk_usage("/")
    free_mb = disk.free // (1024 * 1024)
    if free_mb < 500:
        errors.append(f"low disk: {free_mb}MB free")

    if errors:
        raise RuntimeError(
            f"Preflight failed ({len(errors)} issues): "
            + "; ".join(errors)
        )

    log.info(
        "preflight passed: snowflake warm, schemas ok, "
        "keys ok, playwright ok, apis reachable, disk %dMB free",
        free_mb,
    )


# ── Gates ────────────────────────────────────────────────────

def _check_ingest_gate(**context):
    """Phase 1 -> Phase 2 gate.

    Reads task states from current DAG run. Checks required tasks
    and minimum success count from config. Pushes gate result to
    XCom for Slack summary.
    """
    ti = context["ti"]
    dag_run = context["dag_run"]
    phases = _get_master_managed()
    phase1_tasks = phases.get(1, [])

    succeeded = []
    failed = []
    skipped = []

    for name, cfg in phase1_tasks:
        state_obj = dag_run.get_task_instance(name)
        if state_obj is None:
            skipped.append(name)
        elif state_obj.state == "success":
            succeeded.append(name)
        elif state_obj.state == "skipped":
            skipped.append(name)
        else:
            failed.append(name)

    required_failed = [
        name for name, cfg in phase1_tasks
        if cfg.get("required", False) and name in failed
    ]

    min_success = _GATES.get("min_ingest_success", 5)
    gate_passed = (
        len(succeeded) >= min_success
        and len(required_failed) == 0
    )

    gate_result = {
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "required_failed": required_failed,
        "gate_passed": gate_passed,
    }
    ti.xcom_push(key="gate_result", value=json.dumps(gate_result))

    log.info(
        "ingest_gate: passed=%s succeeded=%d failed=%d skipped=%d "
        "required_failed=%s",
        gate_passed, len(succeeded), len(failed),
        len(skipped), required_failed,
    )

    if not gate_passed:
        raise RuntimeError(
            f"Ingest gate failed: {len(succeeded)} succeeded "
            f"(need {min_success}), required failures: {required_failed}"
        )


def _check_new_signals(**context):
    """ShortCircuit: skip Pinecone sync if no new lifestyle signals.

    Compares LIFESTYLE_SIGNALS against EMBEDDING_SYNC. Returns False
    to skip downstream sync tasks if nothing needs embedding.
    """
    import snowflake.connector
    from app.config import get_settings

    settings = get_settings()
    conn = snowflake.connector.connect(
        account=settings.snowflake_account,
        user=settings.snowflake_user,
        password=settings.snowflake_password.get_secret_value(),
        database=settings.snowflake_database,
        warehouse=settings.snowflake_warehouse,
        role=settings.snowflake_role,
    )
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM RAW.LIFESTYLE_SIGNALS ls
            LEFT JOIN RAW.EMBEDDING_SYNC es ON ls.signal_id = es.signal_id
            WHERE es.signal_id IS NULL
               OR es.content_hash != ls.content_hash
        """)
        new_count = cursor.fetchone()[0]
        cursor.close()
    finally:
        conn.close()

    log.info("new_signals_check: %d unsynced signals", new_count)
    return new_count > 0


# ── Slack Summary ────────────────────────────────────────────

def _slack_summary(**context):
    """Post end-of-pipeline summary via existing _post_slack.

    Single Block Kit message. This is the only Slack notification
    from the master DAG.
    """
    ti = context["ti"]
    dag_run = context["dag_run"]

    gate_json = ti.xcom_pull(task_ids="ingest_gate", key="gate_result")
    gate = json.loads(gate_json) if gate_json else {}

    gate_passed = gate.get("gate_passed", False)
    succeeded = gate.get("succeeded", [])
    failed = gate.get("failed", [])

    icon = ":large_green_circle:" if gate_passed else ":red_circle:"
    label = "COMPLETE" if gate_passed else "GATE FAILED"
    exec_date = dag_run.execution_date.strftime("%Y-%m-%d %H:%M")

    # Per-task status
    phases = _get_master_managed()
    task_lines = []
    for phase_num in sorted(phases.keys()):
        for name, cfg in phases[phase_num]:
            state_obj = dag_run.get_task_instance(name)
            state = state_obj.state if state_obj else "not_run"
            state_icon = {
                "success": ":large_green_circle:",
                "failed": ":red_circle:",
                "skipped": ":fast_forward:",
                "upstream_failed": ":no_entry:",
            }.get(state, ":grey_question:")
            task_lines.append(f"{state_icon} `{name}`: {state}")

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{icon}  Pipeline {label}",
                "emoji": True,
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*DAG:*\n`vicinity_master`"},
                {"type": "mrkdwn", "text": f"*Execution:*\n{exec_date}"},
                {"type": "mrkdwn", "text": f"*Succeeded:*\n{len(succeeded)}"},
                {"type": "mrkdwn", "text": f"*Failed:*\n{len(failed)}"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "\n".join(task_lines),
            },
        },
        {"type": "divider"},
    ]

    cfg = _slack_cfg()
    if not gate_passed and cfg.get("mention_on_failure"):
        blocks.insert(-1, {
            "type": "section",
            "text": {"type": "mrkdwn", "text": cfg["mention_on_failure"]},
        })

    _post_slack(blocks, f"{icon} Vicinity Pipeline {label}")


# ── DAG Definition ───────────────────────────────────────────

_dag_args = {
    "owner": _DEFAULTS.get("owner", "vicinity"),
    "retries": 0,
    "sla": timedelta(minutes=_MASTER.get("sla_min", 120)),
}

# Params: master dry_run + per-task skip flags
_params = {
    "dry_run": Param(False, type="boolean", description="Dry-run all pipelines"),
}
for name, cfg in _DAGS.items():
    if cfg.get("schedule") is None and cfg.get("phase") is not None:
        _params[f"skip_{name}"] = Param(
            False, type="boolean", description=f"Skip {name}",
        )

with DAG(
    dag_id="vicinity_master",
    description=_MASTER.get("description", ""),
    schedule=_MASTER.get("schedule"),
    default_args=_dag_args,
    max_active_runs=1,
    dagrun_timeout=timedelta(
        minutes=_MASTER.get("dagrun_timeout_min", 150),
    ),
    catchup=False,
    tags=_MASTER.get("tags", ["vicinity", "master"]),
    params=_params,
    sla_miss_callback=on_sla_miss,
) as dag:

    phases = _get_master_managed()

    # Preflight — alerts on failure only
    preflight = PythonOperator(
        task_id="preflight",
        python_callable=_preflight,
        on_failure_callback=on_failure,
    )

    # Phase 1: Ingest (parallel, no Slack per task)
    phase1_tasks = []
    for name, cfg in phases.get(1, []):
        task = _build_task(name, cfg)
        preflight >> task
        phase1_tasks.append(task)

    # Ingest gate — alerts on failure only
    ingest_gate = PythonOperator(
        task_id="ingest_gate",
        python_callable=_check_ingest_gate,
        trigger_rule=TriggerRule.ALL_DONE,
        on_failure_callback=on_failure,
    )

    if phase1_tasks:
        phase1_tasks >> ingest_gate
    else:
        preflight >> ingest_gate

    # Phase 2: Sync (skips if no new signals)
    prev_anchor = ingest_gate

    if phases.get(2):
        signal_check = ShortCircuitOperator(
            task_id="check_new_signals",
            python_callable=_check_new_signals,
            trigger_rule=TriggerRule.ALL_SUCCESS,
            on_failure_callback=on_failure,
        )
        prev_anchor >> signal_check
        prev_anchor = signal_check

        for name, cfg in phases[2]:
            task = _build_task(name, cfg)
            prev_anchor >> task
            prev_anchor = task

    # Phase 3: Score
    for name, cfg in phases.get(3, []):
        task = _build_task(name, cfg)
        task.trigger_rule = TriggerRule.ALL_DONE
        prev_anchor >> task
        prev_anchor = task

    # Summary — only Slack message from master
    summary = PythonOperator(
        task_id="slack_summary",
        python_callable=_slack_summary,
        trigger_rule=TriggerRule.ALL_DONE,
    )
    prev_anchor >> summary