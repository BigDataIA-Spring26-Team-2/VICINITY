"""Vicinity master DAG — config-driven orchestration from dags.yml.

Phases:
    1  Ingest   — parallel, independent failure, fallback wiring
    2  Sync     — waits for phase 1 gate, skips if no new data
    3  Score    — waits for phase 2 completion

Execution:
    Preflight validates infrastructure before any tasks launch.
    Ingest gate enforces min success count + required-task checks.
    Each task supports skip via dag_run.conf: {"skip_<name>": true}.
    Master-level dry_run propagates --dry-run to all tasks.

Notifications:
    Start + end summary only.  Per-task retries/failures are surfaced
    by dag_utils callbacks, not duplicated here.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator, ShortCircuitOperator
from airflow.models.param import Param
from airflow.utils.trigger_rule import TriggerRule

from dag_utils import (
    _load_config, _slack_cfg, _post_slack, _task_log_url,
    PIPELINE_ENV,
    on_failure, on_retry, on_sla_miss,
)

log = logging.getLogger(__name__)

# ── Config (single source of truth: dag_utils._load_config) ──

_CFG = _load_config()
_DEFAULTS = _CFG.get("defaults", {})
_MASTER = _CFG.get("master", {})
_DAGS = _CFG.get("dags", {})
_GATES = _MASTER.get("gates", {})


# ── Helpers ──────────────────────────────────────────────────

def _get_master_managed() -> dict[int, list[tuple[str, dict]]]:
    """Group master-managed tasks by phase number.

    schedule: null + phase set  → master-managed
    schedule: cron or no phase  → excluded
    """
    phases: dict[int, list[tuple[str, dict]]] = {}
    for name, cfg in _DAGS.items():
        if cfg.get("schedule") is not None:
            continue
        phase = cfg.get("phase")
        if phase is not None:
            phases.setdefault(phase, []).append((name, cfg))
    return phases


def _build_command(name: str, cfg: dict) -> str:
    """Build CLI string from config module path + pipeline_flags.

    Master params (dry_run, limit, tags) are Jinja-templated for
    runtime override.  Per-task flags use config defaults directly.
    """
    parts = [f"python -m {cfg['command']}"]

    parts.append("{% if params.dry_run %}--dry-run{% endif %}")
    parts.append("{% if params.limit %}--limit {{ params.limit }}{% endif %}")

    flags = cfg.get("pipeline_flags", {})

    # Lifestyle pipelines accept --tags override from master trigger
    if "preference_tag" in flags or "category" in flags:
        parts.append(
            "{% if params.tags %}--tags {{ params.tags | join(' ') }}{% endif %}"
        )

    for flag_name, default_val in flags.items():
        if flag_name in ("dry_run", "limit"):
            continue
        cli_flag = f"--{flag_name.replace('_', '-')}"
        if default_val is None:
            continue
        elif isinstance(default_val, bool):
            if default_val:
                parts.append(cli_flag)
        else:
            parts.append(f"{cli_flag} {default_val}")

    return " ".join(parts)


def _build_bash_command(name: str, cfg: dict) -> str:
    """Wrap command with per-task skip check."""
    cmd = _build_command(name, cfg)
    return (
        "{% if params.skip_" + name + " %}"
        "echo 'SKIPPED by trigger param' && exit 0"
        "{% else %}" + cmd + "{% endif %}"
    )


def _build_task(name: str, cfg: dict) -> BashOperator:
    """Create a BashOperator for a master-managed pipeline task.

    execution_timeout uses dagrun_timeout_min from per-task config.
    Intentionally generous for long-running scrapers (reddit, google_news).
    Pipelines are idempotent via signal_id dedup — a retry after timeout
    resumes from where data was last committed to Snowflake.
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
            minutes=cfg.get("dagrun_timeout_min", _DEFAULTS.get("dagrun_timeout_min", 120))
        ),
        pool=cfg.get("pool", "default_pool"),
        trigger_rule=TriggerRule.ALL_DONE,
        sla=timedelta(minutes=cfg["sla_min"]) if cfg.get("sla_min") else None,
        on_failure_callback=on_failure,
        on_retry_callback=on_retry,
    )


def _format_duration(start, end) -> str:
    """Human-readable elapsed time between two datetimes."""
    if not start:
        return "—"
    from datetime import timezone
    end = end or datetime.now(timezone.utc)
    total = int((end - start).total_seconds())
    if total < 0:
        return "—"
    hrs, remainder = divmod(total, 3600)
    mins, secs = divmod(remainder, 60)
    if hrs:
        return f"{hrs}h {mins}m"
    if mins:
        return f"{mins}m {secs}s"
    return f"{secs}s"


# ── Pool provisioning ────────────────────────────────────────

def _ensure_pools():
    """Create configured pools if absent.  Idempotent, called from preflight."""
    from airflow.models import Pool
    from airflow.utils.session import create_session

    pools = _MASTER.get("pools", {})
    if not pools:
        return
    with create_session() as session:
        existing = {p.pool for p in session.query(Pool).all()}
        for pool_name, slots in pools.items():
            if pool_name not in existing:
                session.add(Pool(
                    pool=pool_name, slots=slots,
                    description=f"Vicinity: {pool_name}",
                    include_deferred=False,
                ))
        session.commit()


# ── Preflight ────────────────────────────────────────────────

def _preflight(**context):
    """Validate infrastructure before launching pipeline tasks.

    Single Snowflake connection for warmup + schema check.
    Catches issues that would cause cryptic failures across tasks.
    """
    import snowflake.connector
    import httpx
    from app.config import get_settings

    _ensure_pools()

    errors = []
    settings = get_settings()

    # Snowflake: warm warehouse + verify schemas in one connection
    try:
        conn = snowflake.connector.connect(
            account=settings.snowflake_account,
            user=settings.snowflake_user,
            password=settings.snowflake_password.get_secret_value(),
            database=settings.snowflake_database,
            warehouse=settings.snowflake_warehouse,
            role=settings.snowflake_role,
            login_timeout=15,
            network_timeout=30,
        )
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT CURRENT_TIMESTAMP()")
            for schema in ("RAW", "SCORECARDS", "USER_DATA"):
                cursor.execute(
                    "SELECT 1 FROM INFORMATION_SCHEMA.SCHEMATA "
                    "WHERE SCHEMA_NAME = %s", (schema,)
                )
                if not cursor.fetchone():
                    errors.append(f"missing schema: {schema}")
            cursor.close()
        finally:
            conn.close()
    except Exception as e:
        errors.append(f"snowflake: {str(e)[:120]}")

    # API keys
    for env_var, purpose in {
        "DEEPSEEK_API_KEY": "LLM classification",
        "OPENAI_API_KEY": "embeddings",
        "PINECONE_API_KEY": "vector sync",
    }.items():
        if not os.getenv(env_var, "").strip():
            errors.append(f"missing {env_var} ({purpose})")

    # Playwright
    try:
        result = subprocess.run(
            ["python", "-c", "from playwright.sync_api import sync_playwright"],
            capture_output=True, timeout=10,
        )
        if result.returncode != 0:
            errors.append("playwright not importable")
    except Exception:
        errors.append("playwright check failed")

    # External endpoints (DNS + TCP only)
    for name, url in {
        "data.boston.gov": "https://data.boston.gov",
        "citizen.com": "https://citizen.com",
        "api.deepseek.com": "https://api.deepseek.com",
    }.items():
        try:
            httpx.head(url, timeout=5.0)
        except Exception:
            errors.append(f"unreachable: {name}")

    # Disk space
    disk = shutil.disk_usage("/")
    free_mb = disk.free // (1024 * 1024)
    if free_mb < 500:
        errors.append(f"low disk: {free_mb}MB free")

    if errors:
        raise RuntimeError(
            f"Preflight failed ({len(errors)} issues): " + "; ".join(errors)
        )

    log.info(
        "preflight passed: snowflake warm, schemas ok, "
        "keys ok, playwright ok, apis reachable, disk %dMB free",
        free_mb,
    )

    # Start notification — Slack failure must never block the pipeline
    try:
        _notify_pipeline_start(free_mb)
    except Exception:
        log.warning("start notification failed", exc_info=True)


def _notify_pipeline_start(free_mb: int):
    """Post start notification to Slack."""
    phases = _get_master_managed()
    task_count = sum(len(tasks) for tasks in phases.values())
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": ":rocket:  Pipeline Started", "emoji": True},
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": "*DAG:*\n`vicinity_master`"},
                {"type": "mrkdwn", "text": f"*Tasks:*\n{task_count}"},
                {"type": "mrkdwn", "text": "*Preflight:*\nPassed"},
                {"type": "mrkdwn", "text": f"*Disk:*\n{free_mb} MB free"},
            ],
        },
        {"type": "divider"},
    ]
    _post_slack(blocks, ":rocket: Vicinity Pipeline Started")


# ── Gates ────────────────────────────────────────────────────

def _check_ingest_gate(**context):
    """Phase 1 → 2 gate.  Enforces min success count + required tasks.

    Pushes gate result to XCom for the summary to read.
    """
    ti = context["ti"]
    dag_run = context["dag_run"]
    phase1_tasks = _get_master_managed().get(1, [])

    succeeded, failed, skipped = [], [], []
    for name, _ in phase1_tasks:
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
    gate_passed = len(succeeded) >= min_success and len(required_failed) == 0

    ti.xcom_push(key="gate_result", value=json.dumps({
        "succeeded": succeeded, "failed": failed, "skipped": skipped,
        "required_failed": required_failed, "gate_passed": gate_passed,
    }))

    log.info(
        "ingest_gate: passed=%s succeeded=%d failed=%d skipped=%d required_failed=%s",
        gate_passed, len(succeeded), len(failed), len(skipped), required_failed,
    )

    if not gate_passed:
        raise RuntimeError(
            f"Ingest gate failed: {len(succeeded)} succeeded "
            f"(need {min_success}), required failures: {required_failed}"
        )


def _check_new_signals(**context):
    """ShortCircuit: skip Pinecone sync if no new lifestyle signals."""
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
        login_timeout=15,
        network_timeout=30,
    )
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM RAW.LIFESTYLE_SIGNALS ls
            LEFT JOIN RAW.EMBEDDING_SYNC es ON ls.signal_id = es.signal_id
            WHERE es.signal_id IS NULL OR es.content_hash != ls.content_hash
        """)
        new_count = cursor.fetchone()[0]
        cursor.close()
    finally:
        conn.close()

    log.info("new_signals_check: %d unsynced signals", new_count)
    return new_count > 0


# ── Slack summary ────────────────────────────────────────────

_PHASE_LABELS = {1: "Ingest", 2: "Sync", 3: "Score"}
_STATE_ICONS = {
    "success":         ":large_green_circle:",
    "failed":          ":red_circle:",
    "skipped":         ":fast_forward:",
    "upstream_failed": ":no_entry:",
    "up_for_retry":    ":warning:",
}
_MAX_SLACK_BLOCKS = 48  # Slack cap is 50; leave room for footer


def _slack_summary(**context):
    """Post end-of-pipeline summary with per-task status and failed task detail."""
    ti = context["ti"]
    dag_run = context["dag_run"]

    # Gate results from XCom
    gate_json = ti.xcom_pull(task_ids="ingest_gate", key="gate_result")
    gate = json.loads(gate_json) if gate_json else {}

    gate_passed = gate.get("gate_passed", False)
    gate_succeeded = gate.get("succeeded", [])
    required_failed = gate.get("required_failed", [])
    min_required = _GATES.get("min_ingest_success", 5)

    icon = ":large_green_circle:" if gate_passed else ":red_circle:"
    label = "COMPLETE" if gate_passed else "FAILED"
    exec_date = dag_run.execution_date.strftime("%Y-%m-%d %H:%M UTC")
    pipeline_duration = _format_duration(dag_run.start_date, datetime.now(timezone.utc))

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{icon}  Pipeline {label}", "emoji": True},
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": "*DAG:*\n`vicinity_master`"},
                {"type": "mrkdwn", "text": f"*Execution:*\n{exec_date}"},
                {"type": "mrkdwn", "text": f"*Duration:*\n{pipeline_duration}"},
                {"type": "mrkdwn", "text": f"*Gate:*\n{'Passed' if gate_passed else 'Failed'} ({len(gate_succeeded)}/{min_required} required)"},
            ],
        },
    ]

    # Gate failure detail
    if not gate_passed:
        detail_parts = []
        if required_failed:
            detail_parts.append(f"*Required failures:* {', '.join(f'`{n}`' for n in required_failed)}")
        if len(gate_succeeded) < min_required:
            detail_parts.append(f"*Succeeded:* {len(gate_succeeded)} (need {min_required})")
        if detail_parts:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(detail_parts)},
            })

    blocks.append({"type": "divider"})

    # Per-task status
    phases = _get_master_managed()
    task_lines = []
    failed_tasks = []

    for phase_num in sorted(phases.keys()):
        task_lines.append(f"*— {_PHASE_LABELS.get(phase_num, f'Phase {phase_num}')} —*")
        for name, cfg in phases[phase_num]:
            state_obj = dag_run.get_task_instance(name)
            if state_obj is None:
                task_lines.append(f":white_circle:  `{name}`  —  not run")
                continue

            state = state_obj.state or "unknown"
            s_icon = _STATE_ICONS.get(state, ":grey_question:")
            duration = _format_duration(state_obj.start_date, state_obj.end_date)
            attempt = state_obj.try_number or 1

            line = f"{s_icon}  `{name}`"
            if state in ("success", "failed", "up_for_retry"):
                line += f"  ·  {duration}  ·  attempt {attempt}"
            elif state == "skipped" and cfg.get("fallback_for"):
                line += "  ·  fallback not needed"
            task_lines.append(line)

            if state == "failed":
                failed_tasks.append((name, state_obj, cfg))

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": "\n".join(task_lines)},
    })

    # Failed task detail with log links
    if failed_tasks:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "header",
            "text": {"type": "plain_text", "text": ":mag:  Failed Task Detail", "emoji": True},
        })
        for idx, (name, state_obj, task_cfg) in enumerate(failed_tasks):
            if len(blocks) >= _MAX_SLACK_BLOCKS - 4:
                remaining = len(failed_tasks) - idx
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"_… and {remaining} more failures (see Airflow UI)_"},
                })
                break

            max_retries = task_cfg.get("retries", _DEFAULTS.get("retries", 2))
            blocks.append({
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Task:* `{name}`"},
                    {"type": "mrkdwn", "text": f"*Attempts:* {state_obj.try_number or 1}/{max_retries + 1}"},
                    {"type": "mrkdwn", "text": f"*Duration:* {_format_duration(state_obj.start_date, state_obj.end_date)}"},
                    {"type": "mrkdwn", "text": f"*Pool:* `{task_cfg.get('pool', 'default_pool')}`"},
                ],
            })
            blocks.append({
                "type": "actions",
                "elements": [{
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":page_facing_up: View Logs", "emoji": True},
                    "url": _task_log_url(state_obj),
                    "style": "danger",
                }],
            })

    blocks.append({"type": "divider"})

    # Mention on failure
    slack_cfg = _slack_cfg()
    if not gate_passed and slack_cfg.get("mention_on_failure"):
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": slack_cfg["mention_on_failure"]},
        })

    # Footer
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": f"vicinity_master · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        }],
    })

    _post_slack(blocks, f"{icon} Vicinity Pipeline {label}")


# ── DAG definition ───────────────────────────────────────────

_dag_args = {
    "owner": _DEFAULTS.get("owner", "vicinity"),
    "retries": 0,
    "start_date": datetime.strptime(
        _DEFAULTS.get("start_date", "2026-04-11"), "%Y-%m-%d"
    ),
    "sla": timedelta(minutes=_MASTER.get("sla_min", 120)),
}

_params = {
    "dry_run": Param(False, type="boolean", description="Dry-run all pipelines"),
    "limit": Param(None, type=["integer", "null"], description="Max records per pipeline"),
    "tags": Param(None, type=["array", "null"], description="Limit lifestyle pipelines to these tags"),
}
for name, cfg in _DAGS.items():
    if cfg.get("schedule") is None and cfg.get("phase") is not None:
        _params[f"skip_{name}"] = Param(False, type="boolean", description=f"Skip {name}")

with DAG(
    dag_id="vicinity_master",
    description=_MASTER.get("description", ""),
    schedule=_MASTER.get("schedule"),
    default_args=_dag_args,
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=_MASTER.get("dagrun_timeout_min", 150)),
    catchup=False,
    tags=_MASTER.get("tags", ["vicinity", "master"]),
    params=_params,
    sla_miss_callback=on_sla_miss,
) as dag:

    phases = _get_master_managed()

    # ── Preflight ────────────────────────────────────────────
    preflight = PythonOperator(
        task_id="preflight",
        python_callable=_preflight,
        on_failure_callback=on_failure,
    )

    # ── Phase 1: Ingest (parallel) ───────────────────────────
    phase1_ops = []
    task_map: dict[str, BashOperator] = {}

    for name, cfg in phases.get(1, []):
        task = _build_task(name, cfg)
        task_map[name] = task
        phase1_ops.append(task)

    for name, cfg in phases.get(1, []):
        fallback_target = cfg.get("fallback_for")
        if fallback_target and fallback_target in task_map:
            task_map[fallback_target] >> task_map[name]
            task_map[name].trigger_rule = TriggerRule.ALL_FAILED
        else:
            preflight >> task_map[name]

    # ── Ingest gate ──────────────────────────────────────────
    ingest_gate = PythonOperator(
        task_id="ingest_gate",
        python_callable=_check_ingest_gate,
        trigger_rule=TriggerRule.ALL_DONE,
        on_failure_callback=on_failure,
    )
    if phase1_ops:
        phase1_ops >> ingest_gate
    else:
        preflight >> ingest_gate

    # ── Phase 2: Sync (sequential, skips if nothing new) ─────
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

    # ── Phase 3: Score (sequential) ──────────────────────────
    for name, cfg in phases.get(3, []):
        task = _build_task(name, cfg)
        task.trigger_rule = TriggerRule.ALL_DONE
        prev_anchor >> task
        prev_anchor = task

    # ── Summary ──────────────────────────────────────────────
    summary = PythonOperator(
        task_id="slack_summary",
        python_callable=_slack_summary,
        trigger_rule=TriggerRule.ALL_DONE,
    )
    prev_anchor >> summary