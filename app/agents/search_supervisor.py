"""Search Supervisor — async node for listing search and scoring.

Runs search_and_filter, then score_listing on top candidates.
Writes ranked results to sub_agent_result for Chat Agent synthesis.

Graph wiring:
  input_gate -> (route=search) -> search_react <-> search_tools -> chat_react -> END
"""

from __future__ import annotations

from typing import Any

import structlog
import yaml
from langchain_core.messages import SystemMessage

from app.agents.llm import create_llm
from app.agents.message_utils import sanitize_messages
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
    """Build context from user profile for search criteria extraction."""
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
    return "\n".join(parts) if parts else ""


# -- Node --------------------------------------------------------------

async def search_node(state: AgentState) -> dict[str, Any]:
    """Async Search Supervisor node.

    Invokes LLM with search tools. The LLM decides criteria and scoring
    strategy via the ReAct loop. When it finishes (no tool_calls),
    the result is placed in sub_agent_result for Chat Agent synthesis.
    """
    log = logger.bind(trace_id=state.get("trace_id"), node="search")

    messages = []
    messages.append(SystemMessage(content=_get_system_prompt()))

    context = _build_context(state)
    if context:
        messages.append(SystemMessage(content=f"USER CONTEXT:\n{context}"))

    messages.extend(sanitize_messages(state["messages"]))

    llm = create_llm(tools=SEARCH_SUPERVISOR_TOOLS)
    response = await llm.ainvoke(messages)

    log.info("search_node_complete", has_tool_calls=bool(response.tool_calls))

    if response.tool_calls:
        return {"messages": [response]}

    return {
        "messages": [response],
        "sub_agent_result": {
            "status": "complete",
            "agent": "search_supervisor",
            "content": response.content,
        },
    }