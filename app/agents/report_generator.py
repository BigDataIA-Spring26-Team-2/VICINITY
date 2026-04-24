"""Report Generator — async node for watch period comparison reports.

Calls compile_evidence, then LLM synthesizes a structured comparison
report. Writes to sub_agent_result for Chat Agent to present.

Graph wiring:
  input_gate -> (route=report) -> report_react <-> report_tools
    -> route_after_report:
         tool_calls pending              -> report_tools
         sub_agent_result.status=bounce  -> record_bounce -> input_gate
         otherwise                       -> chat_react -> guardrail -> END

Bounce mechanisms (two):

  1. Pre-LLM bookmark check: if user has fewer than 2 active
     bookmarks, compile_evidence has nothing useful to compare.
     Bounce BEFORE calling the ~20 second Snowflake query. The
     input_gate already applies this rule but we double-check here
     in case the graph arrived via a reroute path.

  2. Post-LLM BOUNCE: prefix: if the LLM, per the agents.yml
     BAIL OUT section, writes a response starting with "BOUNCE:",
     set status="bounce". Handles explanation questions like
     "how are scores calculated" that the gate misrouted here.
"""

from __future__ import annotations

from typing import Any

import structlog
import yaml
from langchain_core.messages import SystemMessage

from app.agents.llm import create_llm
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

    gr = state.get("gate_reasoning")
    if gr:
        parts.append(f"\nGate reasoning (why you were invoked): {gr}")

    return "\n".join(parts) if parts else ""


# -- Bounce detection --------------------------------------------------

def _is_bounce_response(content: str) -> tuple[bool, str]:
    """True if the LLM output begins with 'BOUNCE:'. See search_supervisor."""
    if not isinstance(content, str):
        return False, ""
    stripped = content.lstrip()
    if stripped.upper().startswith("BOUNCE:"):
        reason = stripped[7:].strip()
        return True, reason
    return False, ""


# -- Node --------------------------------------------------------------

async def report_node(state: AgentState) -> dict[str, Any]:
    """Async Report Generator node.

    Invokes LLM with compile_evidence tool. The LLM calls it to
    gather watch-period data, then synthesizes the report.

    Two bounce paths:
      (1) Pre-LLM: <2 bookmarks → bounce, no LLM call, no Snowflake.
      (2) Post-LLM: response starts with "BOUNCE:" → bounce.
    """
    log = logger.bind(trace_id=state.get("trace_id"), node="report")

    # -- Pre-LLM bookmark guard --
    uc = state.get("user_context", {}) or {}
    bookmarks = uc.get("active_bookmarks") or []
    if len(bookmarks) < 2:
        log.info("report_bounced_bookmark_count", count=len(bookmarks))
        return {
            "sub_agent_result": {
                "status": "bounce",
                "agent": "report_generator",
                "reason": (
                    f"User has {len(bookmarks)} active bookmark(s). "
                    "Report comparison requires at least 2 bookmarks."
                ),
                "content": "",
            },
        }

    messages = []
    messages.append(SystemMessage(content=_get_system_prompt()))

    context = _build_context(state)
    if context:
        messages.append(SystemMessage(content=f"USER CONTEXT:\n{context}"))

    messages.extend(state["messages"])

    llm = create_llm(tools=REPORT_GENERATOR_TOOLS)
    response = await llm.ainvoke(messages)

    log.info("report_node_complete", has_tool_calls=bool(response.tool_calls))

    if response.tool_calls:
        return {"messages": [response]}

    # Check for bounce prefix in LLM response
    is_bounce, bounce_reason = _is_bounce_response(response.content)
    if is_bounce:
        log.info("report_bounced_llm", reason=bounce_reason[:200])
        return {
            "sub_agent_result": {
                "status": "bounce",
                "agent": "report_generator",
                "reason": bounce_reason,
                "content": "",
            },
        }

    return {
        "messages": [response],
        "sub_agent_result": {
            "status": "complete",
            "agent": "report_generator",
            "content": response.content,
        },
    }