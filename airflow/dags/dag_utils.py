"""Shared utilities for Vicinity Airflow DAGs.

Provides:
- dag_config()    — per-DAG config from config/dags.yml, merged with defaults
- default_args()  — Airflow default_args dict from config
- PIPELINE_ENV    — environment dict for pipeline BashOperators
- Param helpers   — typed Param factories for Airflow trigger forms
- Flag helpers    — Jinja2 snippets for CLI flag templating
- Slack callbacks — on_success, on_failure, on_retry, on_sla_miss
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen

import yaml
from airflow.models.param import Param

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────

PROJECT_DIR = "/opt/airflow/project"
PIPELINE_ENV = {"PYTHONPATH": PROJECT_DIR}


# ── Config Loader ────────────────────────────────────────────

_CONFIG_PATH = Path(PROJECT_DIR) / "config" / "dags.yml"
_config_cache: dict | None = None


def _load_config() -> dict:
    """Load and cache config/dags.yml for scheduler lifetime."""
    global _config_cache
    if _config_cache is None:
        with open(_CONFIG_PATH) as f:
            _config_cache = yaml.safe_load(f) or {}
    return _config_cache


def dag_config(dag_name: str) -> dict:
    """Return merged config for a DAG (defaults + per-DAG overrides).

    Pre-computes for direct use in DAG constructors:
      start_date     → datetime
      dagrun_timeout → timedelta
      sla            → timedelta | None
    """
    cfg = _load_config()
    merged = {**cfg.get("defaults", {}), **cfg.get("dags", {}).get(dag_name, {})}

    merged["start_date"] = datetime.strptime(
        merged.get("start_date", "2026-04-11"), "%Y-%m-%d"
    )
    merged["dagrun_timeout"] = timedelta(
        minutes=merged.get("dagrun_timeout_min", 120)
    )
    sla_min = merged.get("sla_min")
    merged["sla"] = timedelta(minutes=sla_min) if sla_min else None
    merged.setdefault("pipeline_flags", {})

    return merged


def default_args(cfg: dict) -> dict:
    """Build Airflow default_args from a dag_config() result."""
    return {
        "owner": cfg.get("owner", "vicinity"),
        "retries": cfg.get("retries", 2),
        "retry_delay": timedelta(minutes=cfg.get("retry_delay_min", 5)),
        "on_success_callback": on_success,
        "on_failure_callback": on_failure,
        "on_retry_callback": on_retry,
    }


# ── Slack Alerting ───────────────────────────────────────────

# Airflow webserver base URL for deep-linking logs.
# In Docker Compose the webserver listens on container port 8080,
# but is published on the host at 8081.
_AIRFLOW_BASE_URL = "http://localhost:8081"


def _slack_cfg() -> dict:
    """Return Slack config from dags.yml. Cached after first read."""
    return _load_config().get("notifications", {}).get("slack", {})


def _post_slack(blocks: list[dict], text: str = ""):
    """Post Block Kit message to Slack via direct webhook POST. Fails silently."""
    cfg = _slack_cfg()
    if not cfg.get("enabled"):
        return
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        log.warning("SLACK_WEBHOOK_URL not set — skipping notification")
        return
    try:
        payload = json.dumps({"blocks": blocks, "text": text or "Vicinity Pipeline Alert"})
        req = Request(
            webhook_url,
            data=payload.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urlopen(req, timeout=10)  # noqa: S310
    except Exception:
        log.warning("Slack notification failed", exc_info=True)


def _task_log_url(ti) -> str:
    """Build a direct URL to the task log page."""
    return (
        f"{_AIRFLOW_BASE_URL}/dags/{ti.dag_id}/grid"
        f"?dag_run_id={ti.run_id}&task_id={ti.task_id}"
    )


def _duration_str(ti) -> str:
    """Human-readable task duration from start to now/end."""
    try:
        start = ti.start_date
        end = ti.end_date or datetime.utcnow()
        delta = end - start
        total = int(delta.total_seconds())
        mins, secs = divmod(total, 60)
        return f"{mins}m {secs}s" if mins else f"{secs}s"
    except Exception:
        return "—"


def _build_alert(context: dict, event: str) -> tuple[list[dict], str]:
    """Build Slack Block Kit blocks for a pipeline event.

    Events: success, failure, retry, sla_miss
    Returns (blocks, fallback_text).
    """
    ti = context.get("task_instance")
    cfg = _slack_cfg()

    status_map = {
        "success":  (":large_green_circle:", "SUCCESS"),
        "failure":  (":red_circle:", "FAILURE"),
        "retry":    (":warning:", "RETRY"),
        "sla_miss": (":clock1:", "SLA MISS"),
    }
    icon, label = status_map.get(event, (":grey_question:", event.upper()))

    dag_id = ti.dag_id if ti else "unknown"
    task_id = ti.task_id if ti else "unknown"
    attempt = ti.try_number if ti else "—"
    duration = _duration_str(ti) if ti else "—"
    exec_date = str(context.get("execution_date", ""))[:19]
    log_url = _task_log_url(ti) if ti else ""

    fallback = f"{icon} {label}: {dag_id}/{task_id}"

    # ── Header
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{icon}  Pipeline {label}", "emoji": True},
        },
        {"type": "divider"},
    ]

    # ── Details
    fields = [
        f"*DAG:*\n`{dag_id}`",
        f"*Task:*\n`{task_id}`",
        f"*Duration:*\n{duration}",
        f"*Attempt:*\n{attempt}",
        f"*Execution:*\n{exec_date}",
        f"*Worker:*\n`{ti.hostname or '—'}`" if ti and hasattr(ti, "hostname") else "",
    ]
    blocks.append({
        "type": "section",
        "fields": [{"type": "mrkdwn", "text": f} for f in fields if f],
    })

    # ── Error snippet (failure only)
    if event == "failure":
        exc = context.get("exception")
        if exc:
            snippet = str(exc)[:1500]
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Error:*\n```{snippet}```"},
            })

    # ── Log link button
    if log_url:
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":page_facing_up: View Logs", "emoji": True},
                    "url": log_url,
                }
            ],
        })

    # ── Mention line for failures
    if event == "failure" and cfg.get("mention_on_failure"):
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": cfg["mention_on_failure"]},
        })

    blocks.append({"type": "divider"})

    return blocks, fallback


def on_success(context: dict):
    """Task success callback — fires after task completes normally."""
    cfg = _slack_cfg()
    if not cfg.get("notify_on_success", True):
        return
    blocks, text = _build_alert(context, "success")
    _post_slack(blocks, text)


def on_failure(context: dict):
    """Task failure callback — fires after all retries exhausted."""
    blocks, text = _build_alert(context, "failure")
    _post_slack(blocks, text)


def on_retry(context: dict):
    """Task retry callback — fires on each retry attempt."""
    blocks, text = _build_alert(context, "retry")
    _post_slack(blocks, text)


def on_sla_miss(dag, task_list, blocking_task_list, slas, blocking_tis):
    """SLA miss callback — fires when a task exceeds its time threshold."""
    tasks = ", ".join(f"`{t}`" for t in task_list[:5])
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": ":clock1:  Pipeline SLA MISS", "emoji": True},
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*DAG:*\n`{dag.dag_id}`"},
                {"type": "mrkdwn", "text": f"*Overdue Tasks:*\n{tasks}"},
            ],
        },
        {"type": "divider"},
    ]
    cfg = _slack_cfg()
    if cfg.get("mention_on_failure"):
        blocks.insert(-1, {
            "type": "section",
            "text": {"type": "mrkdwn", "text": cfg["mention_on_failure"]},
        })
    _post_slack(blocks, f":clock1: SLA MISS: {dag.dag_id}")


# ── Param Helpers ────────────────────────────────────────────
# Typed Param factories for the Airflow UI trigger form.
# Defaults come from config pipeline_flags; overrides via dag_run.conf.


def param_string(default=None, description=""):
    """String parameter — None default makes it optional."""
    return Param(default, type=["string", "null"], description=description)


def param_int(default=None, description=""):
    """Integer parameter — None default makes it optional."""
    return Param(default, type=["integer", "null"], description=description)


def param_bool(default=False, description=""):
    """Boolean parameter — renders as toggle in Airflow UI."""
    return Param(default, type="boolean", description=description)


def param_list(default=None, description=""):
    """List parameter — for multi-value CLI flags like --route-types."""
    return Param(default, type=["array", "null"], description=description)


# ── Flag Helpers ─────────────────────────────────────────────
# Jinja2 template snippets for CLI flags in BashOperator commands.
# Each returns a string that Airflow renders at runtime from `params`.
# Compose: " ".join(["python -m mod", value_flag(...), bool_flag(...)])


def value_flag(name: str, cli_arg: str) -> str:
    """Value flag: --flag value (omitted when param is None)."""
    return (
        "{% if params." + name + " is not none %}"
        + cli_arg + " {{ params." + name + " }}"
        + "{% endif %}"
    )


def bool_flag(name: str, cli_arg: str) -> str:
    """Boolean flag: --flag (omitted when param is falsy)."""
    return "{% if params." + name + " %}" + cli_arg + "{% endif %}"


def list_flag(name: str, cli_arg: str) -> str:
    """List flag: --flag a b c (omitted when param is None/empty)."""
    return (
        "{% if params." + name + " %}"
        + cli_arg + " {{ params." + name + " | join(' ') }}"
        + "{% endif %}"
    )
