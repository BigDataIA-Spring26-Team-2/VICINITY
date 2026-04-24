"""Per-turn LLM usage + cost tracking for the chat pipeline.

Hooks into `astream_events` by reading `on_chat_model_end` events.
Every LLM call across every agent node fires one. We sum input and
output tokens, apply the cost-per-million-tokens table from
config/classification.yml, and persist one row per turn to
RAW.LLM_USAGE_LOG.

Authentication-only persistence: anonymous sessions still accumulate
usage (so the DONE event can carry it) but no DB write happens.

Reuses existing RAW.LLM_USAGE_LOG table:
  source           = 'chat'
  pipeline_run_id  = trace_id (UUID per turn)
  operation        = session_id (for grouping across turns in a session)
  batch_size       = number of LLM calls in this turn

Schema (exact columns, types from sql_freeform.py):
  id (PK UUID), pipeline_run_id, source, operation, model,
  input_tokens INT, output_tokens INT, total_tokens INT,
  input_cost_usd DECIMAL, output_cost_usd DECIMAL, total_cost_usd DECIMAL,
  batch_size INT, duration_ms INT, created_at TIMESTAMP
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

import structlog
import yaml

from app.core.config_loader import CONFIG_DIR

logger = structlog.get_logger()


# ---------------------------------------------------------------------
# Cost lookup
# ---------------------------------------------------------------------

_COST_CACHE: Optional[dict] = None


def _load_cost_table() -> dict:
    """Return {model_name: {"input": $/M, "output": $/M}} from classification.yml.

    Falls back to a conservative default if the file or keys are missing.
    """
    global _COST_CACHE
    if _COST_CACHE is not None:
        return _COST_CACHE

    defaults = {
        "deepseek-chat":   {"input": 0.14, "output": 0.28},
        "gpt-4o":          {"input": 2.50, "output": 10.00},
        "gpt-4o-mini":     {"input": 0.15, "output": 0.60},
    }

    try:
        with open(CONFIG_DIR / "classification.yml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        table = cfg.get("cost_per_million_tokens") or {}
        if isinstance(table, dict) and table:
            _COST_CACHE = {**defaults, **table}
        else:
            _COST_CACHE = defaults
    except FileNotFoundError:
        _COST_CACHE = defaults
    except Exception as e:
        logger.warning("cost_table_load_failed", error=str(e)[:200])
        _COST_CACHE = defaults

    return _COST_CACHE


def _rates_for_model(model: str) -> dict:
    table = _load_cost_table()
    for key, value in table.items():
        if key in model or model.startswith(key):
            return value
    return {"input": 0.0, "output": 0.0}


def _cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = _rates_for_model(model)
    return (
        (input_tokens / 1_000_000) * rates.get("input", 0.0)
        + (output_tokens / 1_000_000) * rates.get("output", 0.0)
    )


# ---------------------------------------------------------------------
# Per-turn accumulator
# ---------------------------------------------------------------------

@dataclass
class UsageSnapshot:
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    total_cost_usd: float
    calls: int
    duration_ms: int


@dataclass
class ChatUsageAccumulator:
    """Accumulate usage for one turn across N LLM calls."""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    duration_ms: int = 0

    def add(self, model: str, input_tokens: int, output_tokens: int, duration_ms: int = 0):
        if model and not self.model:
            self.model = model
        self.input_tokens += int(input_tokens or 0)
        self.output_tokens += int(output_tokens or 0)
        self.duration_ms += int(duration_ms or 0)
        self.calls += 1

    def snapshot(self) -> Optional[UsageSnapshot]:
        if self.calls == 0:
            return None
        total_tokens = self.input_tokens + self.output_tokens
        return UsageSnapshot(
            model=self.model or "unknown",
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=total_tokens,
            total_cost_usd=_cost_usd(self.model or "", self.input_tokens, self.output_tokens),
            calls=self.calls,
            duration_ms=self.duration_ms,
        )


# ---------------------------------------------------------------------
# Event extraction
# ---------------------------------------------------------------------

def extract_usage_from_event(event: dict) -> Optional[dict]:
    """Parse a LangGraph on_chat_model_end event into a usage dict."""
    data = event.get("data") or {}
    output = data.get("output")
    if output is None:
        return None

    usage = getattr(output, "usage_metadata", None)
    if not usage:
        return None

    model = ""
    rm = getattr(output, "response_metadata", None) or {}
    if isinstance(rm, dict):
        model = rm.get("model_name") or rm.get("model") or ""
    if not model:
        model = event.get("name", "") or ""

    return {
        "model": model,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
    }


# ---------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------

class ChatCostTracker:
    """Per-turn cost persister. Writes ONE row per turn to RAW.LLM_USAGE_LOG.

    Uses an existing cursor owned by _persist_exchange; does not open
    connections. Failures are logged and swallowed — cost tracking
    never blocks user-facing persist.

    Matches the same RAW.LLM_USAGE_LOG schema the pipeline CostTracker
    writes to (app/core/classifier.py), with source='chat'.
    """

    _INSERT_SQL = """
    INSERT INTO RAW.LLM_USAGE_LOG (
        id,
        pipeline_run_id,
        source,
        operation,
        model,
        input_tokens,
        output_tokens,
        total_tokens,
        input_cost_usd,
        output_cost_usd,
        total_cost_usd,
        batch_size,
        duration_ms
    )
    SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
    """

    def __init__(self, cursor):
        self._cursor = cursor

    def flush_turn(self, trace_id: str, session_id: str, snapshot: UsageSnapshot):
        if snapshot is None or snapshot.calls == 0:
            return

        # Split total cost into input/output buckets so the row is
        # consistent with pipeline rows in the same table.
        rates = _rates_for_model(snapshot.model or "")
        input_cost = (snapshot.input_tokens / 1_000_000) * rates.get("input", 0.0)
        output_cost = (snapshot.output_tokens / 1_000_000) * rates.get("output", 0.0)

        try:
            self._cursor.execute(
                self._INSERT_SQL,
                (
                    str(uuid.uuid4()),
                    trace_id,
                    "chat",
                    session_id,
                    snapshot.model,
                    snapshot.input_tokens,
                    snapshot.output_tokens,
                    snapshot.total_tokens,
                    round(input_cost, 6),
                    round(output_cost, 6),
                    round(snapshot.total_cost_usd, 6),
                    snapshot.calls,          # batch_size = LLM calls this turn
                    snapshot.duration_ms,
                ),
            )
            logger.info(
                "chat_usage_logged",
                trace_id=trace_id,
                session_id=session_id,
                model=snapshot.model,
                tokens=snapshot.total_tokens,
                cost_usd=round(snapshot.total_cost_usd, 6),
                calls=snapshot.calls,
                component="chat_cost_tracker",
            )
        except Exception as e:
            logger.warning(
                "chat_usage_log_failed",
                trace_id=trace_id,
                error=str(e)[:200],
                component="chat_cost_tracker",
            )