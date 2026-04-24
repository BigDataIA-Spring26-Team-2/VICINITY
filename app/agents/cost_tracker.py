"""Chat LLM cost tracker.

Writes one aggregated row per turn to the existing RAW.LLM_USAGE_LOG
table — the same table the ingestion pipelines write to. No schema
changes. Authenticated turns only (matches _persist_exchange semantics).

Column mapping for chat rows:
    pipeline_run_id  -> trace_id  (per-turn grouping key)
    source           -> "chat"    (distinguishes chat from pipeline rows)
    operation        -> session_id (per-session grouping key)
    model            -> model string reported by the LLM
    input_tokens     -> summed across all LLM calls within the turn
    output_tokens    -> summed
    total_tokens     -> summed
    input_cost_usd   -> summed
    output_cost_usd  -> summed
    total_cost_usd   -> summed
    batch_size       -> 1
    duration_ms      -> summed across all LLM calls within the turn

Per-turn aggregation policy was chosen to match how _persist_exchange
writes one row per turn, and to keep the usage log at a sane row volume.
Per-node breakdown is still recoverable from structlog events if needed
for debugging, but the canonical billing record is the per-turn row.

Cost rates come from config/classification.yml -> cost_per_million_tokens,
shared with the pipeline CostTracker so chat and pipeline costs use the
same source of truth.

Usage:
    # Inside a turn, collect per-LLM-call usage:
    acc = ChatUsageAccumulator()
    acc.add(model="deepseek-chat", input_tokens=120, output_tokens=340,
            duration_ms=580)
    acc.add(model="deepseek-chat", input_tokens=95, output_tokens=210,
            duration_ms=430)

    # At turn-end, flush to Snowflake:
    tracker = ChatCostTracker(cursor)
    tracker.flush_turn(
        trace_id=trace_id,
        session_id=session_id,
        usage=acc.snapshot(),
    )
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

import structlog

from app.core.config_loader import load_classification

logger = structlog.get_logger()


# -- Config (cached at module level) ----------------------------------

_cost_cfg: Optional[dict] = None


def _costs() -> dict:
    """Return the cost_per_million_tokens mapping, cached."""
    global _cost_cfg
    if _cost_cfg is None:
        cfg = load_classification() or {}
        _cost_cfg = cfg.get("cost_per_million_tokens", {}) or {}
    return _cost_cfg


def _model_costs(model: str) -> dict:
    """Return {"input": $/M, "output": $/M} for the given model, or zeros."""
    return _costs().get(model, {"input": 0.0, "output": 0.0})


# -- Per-turn accumulator ---------------------------------------------

@dataclass
class _PerModelCounters:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    duration_ms: int = 0
    calls: int = 0


@dataclass
class ChatUsageSnapshot:
    """Aggregated per-turn usage + cost. Emitted by ChatUsageAccumulator.snapshot()."""
    # Primary (pick a single representative model — the one with the most tokens)
    model: str
    # Aggregated counts
    input_tokens: int
    output_tokens: int
    total_tokens: int
    duration_ms: int
    # Aggregated costs (USD)
    input_cost_usd: float
    output_cost_usd: float
    total_cost_usd: float
    # Ledger for debugging
    calls: int
    per_model: dict[str, _PerModelCounters] = field(default_factory=dict)


class ChatUsageAccumulator:
    """Collects LLM usage across one turn. Thread-safe within a turn if
    only the owning pipeline calls .add() from its own event loop.

    The accumulator is intentionally sync — add() is called from inside
    the astream_events loop where there's no useful async work to do per
    token-count update.
    """

    def __init__(self):
        self._by_model: dict[str, _PerModelCounters] = {}

    def add(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int = 0,
    ) -> None:
        """Record one LLM call's usage."""
        if not model:
            model = "unknown"
        ctr = self._by_model.setdefault(model, _PerModelCounters())
        ctr.input_tokens += max(0, int(input_tokens or 0))
        ctr.output_tokens += max(0, int(output_tokens or 0))
        ctr.total_tokens += max(0, int((input_tokens or 0) + (output_tokens or 0)))
        ctr.duration_ms += max(0, int(duration_ms or 0))
        ctr.calls += 1

    def is_empty(self) -> bool:
        return not self._by_model or all(
            c.total_tokens == 0 for c in self._by_model.values()
        )

    def snapshot(self) -> Optional[ChatUsageSnapshot]:
        """Produce an aggregated snapshot, or None if nothing was recorded."""
        if self.is_empty():
            return None

        in_tok = sum(c.input_tokens for c in self._by_model.values())
        out_tok = sum(c.output_tokens for c in self._by_model.values())
        tot_tok = sum(c.total_tokens for c in self._by_model.values())
        dur_ms = sum(c.duration_ms for c in self._by_model.values())
        calls = sum(c.calls for c in self._by_model.values())

        # Cost is summed across per-model rates. Each model's tokens get
        # priced with its own rate, then added — this is the only way to
        # price multi-provider turns (e.g. DeepSeek then OpenAI fallback)
        # accurately.
        in_cost = 0.0
        out_cost = 0.0
        for model, ctr in self._by_model.items():
            rates = _model_costs(model)
            in_cost += ctr.input_tokens * rates["input"] / 1_000_000
            out_cost += ctr.output_tokens * rates["output"] / 1_000_000

        # Representative model = whichever produced the most tokens.
        rep_model = max(
            self._by_model.items(),
            key=lambda kv: kv[1].total_tokens,
        )[0]

        return ChatUsageSnapshot(
            model=rep_model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=tot_tok,
            duration_ms=dur_ms,
            input_cost_usd=round(in_cost, 6),
            output_cost_usd=round(out_cost, 6),
            total_cost_usd=round(in_cost + out_cost, 6),
            calls=calls,
            per_model=dict(self._by_model),
        )


# -- Writer -----------------------------------------------------------

class ChatCostTracker:
    """Writes aggregated chat usage rows to RAW.LLM_USAGE_LOG.

    Mirrors the pattern of app.core.classifier.CostTracker but is
    cursor-injected per call, matching how the rest of the chat code
    manages short-lived cursors.
    """

    def __init__(self, cursor):
        self._cursor = cursor
        self._log = logger.bind(component="chat_cost_tracker")

    def flush_turn(
        self,
        trace_id: str,
        session_id: str,
        usage: ChatUsageSnapshot,
    ) -> None:
        """Write one aggregated row for a completed turn.

        Swallows database errors with a warning log — cost tracking is
        observability, not a hard dependency. The conversation log is
        still written by _persist_exchange independently.
        """
        if usage is None:
            return

        try:
            self._cursor.execute(
                "INSERT INTO RAW.LLM_USAGE_LOG "
                "(id, pipeline_run_id, source, operation, model, "
                " input_tokens, output_tokens, total_tokens, "
                " input_cost_usd, output_cost_usd, total_cost_usd, "
                " batch_size, duration_ms) "
                "SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s",
                (
                    str(uuid.uuid4()),
                    trace_id,
                    "chat",
                    session_id,
                    usage.model,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.total_tokens,
                    round(usage.input_cost_usd, 6),
                    round(usage.output_cost_usd, 6),
                    round(usage.total_cost_usd, 6),
                    1,
                    usage.duration_ms,
                ),
            )
        except Exception as e:
            self._log.warning(
                "chat_usage_write_failed",
                trace_id=trace_id,
                session_id=session_id,
                error=str(e)[:200],
            )
            return

        self._log.info(
            "chat_usage_logged",
            trace_id=trace_id,
            session_id=session_id,
            model=usage.model,
            tokens=usage.total_tokens,
            cost_usd=usage.total_cost_usd,
            calls=usage.calls,
        )


# -- Event-payload extractor ------------------------------------------

def extract_usage_from_event(event: dict) -> Optional[dict]:
    """Pull (model, input_tokens, output_tokens) from an astream_events
    on_chat_model_end event.

    Returns {"model": str, "input_tokens": int, "output_tokens": int}
    if the event carries usage metadata, else None.

    LangChain's ChatDeepSeek and ChatOpenAI both populate
    AIMessage.usage_metadata on the final chunk. The shape is:
        {"input_tokens": int, "output_tokens": int, "total_tokens": int}

    Model name is taken from the chat model's metadata if present, else
    from the response's response_metadata.
    """
    data = event.get("data") or {}
    output = data.get("output")
    if output is None:
        return None

    # output is typically an AIMessage (or AIMessageChunk for streaming).
    usage = getattr(output, "usage_metadata", None)
    if not usage:
        # Fallback: some providers put token counts in response_metadata.
        resp_meta = getattr(output, "response_metadata", None) or {}
        token_usage = resp_meta.get("token_usage") or resp_meta.get("usage") or {}
        if not token_usage:
            return None
        usage = {
            "input_tokens": token_usage.get("prompt_tokens")
                            or token_usage.get("input_tokens") or 0,
            "output_tokens": token_usage.get("completion_tokens")
                             or token_usage.get("output_tokens") or 0,
        }

    # Model: prefer metadata.ls_model_name, then response_metadata.model_name
    meta = event.get("metadata") or {}
    model = meta.get("ls_model_name") or ""
    if not model:
        resp_meta = getattr(output, "response_metadata", None) or {}
        model = resp_meta.get("model_name") or resp_meta.get("model") or ""

    return {
        "model": str(model or "unknown"),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
    }