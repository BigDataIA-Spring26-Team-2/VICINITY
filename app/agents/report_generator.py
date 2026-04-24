"""Report Generator — async node for watch period comparison reports.

Calls compile_evidence, then LLM synthesizes a structured comparison
report. The report IS the final user-facing artifact — it follows a
fixed template (Recommendation / At a Glance / Per-Listing Analysis /
Tradeoffs / Data Confidence) and does not get rewritten by the Chat
Agent. The graph routes report_react -> guardrail directly when the
ReAct loop finishes.

Graph wiring:
  input_gate -> (route=report) -> report_react <-> report_tools -> guardrail -> END
"""

from __future__ import annotations

from typing import Any

import structlog
import yaml
from langchain_core.messages import SystemMessage

from app.agents.llm import create_llm
from app.agents.message_utils import sanitize_messages
from app.agents.state import AgentState
from app.agents.tools.search_tools import REPORT_GENERATOR_TOOLS
from app.core.config_loader import CONFIG_DIR

logger = structlog.get_logger()

# -- Config (cached at module level) ----------------------------------

_system_prompt: str | None = None


def _get_system_prompt() -> str:
    global _system_prompt
    if _system_prompt is None:
        with open(CONFIG_DIR / "agents.yml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        _system_prompt = cfg.get("report_generator", {}).get("system_prompt", "")
    return _system_prompt


# -- Context builder ---------------------------------------------------

def _build_context(state: AgentState) -> str:
    """Build context emphasizing user priorities and bookmarks."""
    parts = []
    uc = state.get("user_context", {})
    if uc.get("user_id"):
        parts.append(f"user_id: {uc['user_id']}")
    if uc.get("preference_tags"):
        parts.append(f"Priorities: {', '.join(uc['preference_tags'])}")
    if uc.get("preferences_text"):
        parts.append(f"Preferences: {uc['preferences_text']}")
    if uc.get("active_bookmarks"):
        for b in uc["active_bookmarks"]:
            parts.append(
                f"Bookmark: {b.get('listing_id')} — "
                f"{b.get('street', '?')}, {b.get('neighborhood', '?')} "
                f"${b.get('price', '?')}"
            )
    return "\n".join(parts) if parts else ""


# -- Node --------------------------------------------------------------

async def report_node(state: AgentState) -> dict[str, Any]:
    """Async Report Generator node.

    Invokes LLM with compile_evidence tool. The LLM calls it to
    gather watch period data, then synthesizes the report.
    """
    log = logger.bind(trace_id=state.get("trace_id"), node="report")

    messages = [SystemMessage(content=_get_system_prompt())]

    context = _build_context(state)
    if context:
        messages.append(SystemMessage(content=f"USER CONTEXT:\n{context}"))

    messages.extend(sanitize_messages(state["messages"]))

    llm = create_llm(tools=REPORT_GENERATOR_TOOLS)
    response = await llm.ainvoke(messages)

    log.info("report_node_complete", has_tool_calls=bool(response.tool_calls))

    # When the ReAct loop finishes (no more tool_calls), the LLM's
    # response IS the final structured report. It flows directly to
    # guardrail as the user-facing output — no Chat Agent re-synthesis,
    # which would strip the template structure (## Recommendation,
    # ## At a Glance table, etc.) the user prompt demands.
    return {"messages": [response]}