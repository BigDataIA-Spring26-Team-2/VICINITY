"""Search Supervisor — async node for listing search and scoring.

Runs search_and_filter, then score_listing on top candidates.
Writes ranked results to sub_agent_result for Chat Agent synthesis.

Graph wiring:
  input_gate -> (route=search) -> search_react <-> search_tools
    -> route_after_search:
         tool_calls pending              -> search_tools
         sub_agent_result.status=bounce  -> record_bounce -> input_gate
         otherwise                       -> chat_react -> guardrail -> END

Bounce mechanism:
  When the gate misroutes an explanation or lookup to search, the
  LLM (per the system prompt in agents.yml) begins its response with
  "BOUNCE:" followed by a brief reason. This node detects the prefix
  and sets sub_agent_result.status="bounce" so the graph routes back
  to input_gate instead of letting chat fabricate synthesis from the
  non-answer.
"""

from __future__ import annotations

from typing import Any

import structlog
import yaml
from langchain_core.messages import SystemMessage

from app.agents.llm import create_llm
from app.agents.state import AgentState
from app.agents.tools.search_tools import SEARCH_SUPERVISOR_TOOLS
from app.core.config_loader import CONFIG_DIR

logger = structlog.get_logger()

# -- Config (cached at module level) ----------------------------------

_system_prompt: str | None = None


def _get_system_prompt() -> str:
    global _system_prompt
    if _system_prompt is None:
        with open(CONFIG_DIR / "agents.yml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        _system_prompt = cfg.get("search_supervisor", {}).get("system_prompt", "")
    return _system_prompt


# -- Context builder ---------------------------------------------------

def _build_context(state: AgentState) -> str:
    """Build context from user profile for search criteria extraction.

    Also injects gate_reasoning so the sub-agent knows WHY it was
    invoked — helpful for deciding whether to bounce when the gate
    was borderline.
    """
    parts = []
    uc = state.get("user_context", {})
    if uc.get("budget_min") or uc.get("budget_max"):
        parts.append(f"Budget: ${uc.get('budget_min', '?')}-${uc.get('budget_max', '?')}")
    if uc.get("bedrooms_min"):
        parts.append(f"Bedrooms: {uc['bedrooms_min']}+")
    if uc.get("work_address"):
        parts.append(f"Work: {uc['work_address']} ({uc.get('work_lat')}, {uc.get('work_lon')})")
    if uc.get("preference_tags"):
        parts.append(f"Preferences: {', '.join(uc['preference_tags'])}")
    if uc.get("max_commute_min"):
        parts.append(f"Max commute: {uc['max_commute_min']} min")

    # Gate reasoning — why you were routed here
    gr = state.get("gate_reasoning")
    if gr:
        parts.append(f"\nGate reasoning (why you were invoked): {gr}")

    return "\n".join(parts) if parts else ""


# -- Bounce detection --------------------------------------------------

def _is_bounce_response(content: str) -> tuple[bool, str]:
    """True if the LLM output begins with 'BOUNCE:' (per system prompt).

    Returns (is_bounce, reason). The reason is the text after 'BOUNCE:'
    with leading/trailing whitespace stripped.
    """
    if not isinstance(content, str):
        return False, ""
    stripped = content.lstrip()
    if stripped.upper().startswith("BOUNCE:"):
        reason = stripped[7:].strip()
        return True, reason
    return False, ""


# -- Node --------------------------------------------------------------

async def search_node(state: AgentState) -> dict[str, Any]:
    """Async Search Supervisor node.

    Invokes LLM with search tools. The LLM decides criteria and scoring
    strategy via the ReAct loop. When it finishes (no tool_calls),
    the result is placed in sub_agent_result.

    Bounce: if the LLM response starts with "BOUNCE:" (per the system
    prompt's BAIL OUT instructions), set status="bounce" so the graph
    reroutes to input_gate.
    """
    log = logger.bind(trace_id=state.get("trace_id"), node="search")

    messages = []
    messages.append(SystemMessage(content=_get_system_prompt()))

    context = _build_context(state)
    if context:
        messages.append(SystemMessage(content=f"USER CONTEXT:\n{context}"))

    messages.extend(state["messages"])

    llm = create_llm(tools=SEARCH_SUPERVISOR_TOOLS)
    response = await llm.ainvoke(messages)

    log.info("search_node_complete", has_tool_calls=bool(response.tool_calls))

    if response.tool_calls:
        return {"messages": [response]}

    # Check for bounce prefix
    is_bounce, bounce_reason = _is_bounce_response(response.content)
    if is_bounce:
        log.info("search_bounced", reason=bounce_reason[:200])
        return {
            # Don't append the BOUNCE: message to the conversation —
            # the user should never see it. The graph's record_bounce
            # + input_gate handle the reclassification.
            "sub_agent_result": {
                "status": "bounce",
                "agent": "search_supervisor",
                "reason": bounce_reason,
                "content": "",
            },
        }

    return {
        "messages": [response],
        "sub_agent_result": {
            "status": "complete",
            "agent": "search_supervisor",
            "content": response.content,
        },
    }