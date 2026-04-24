"""Chat Agent — async ReAct node for user-facing read-only queries.

Primary user-facing agent. Runs a ReAct loop:
  LLM proposes tool calls -> tools execute -> LLM synthesizes.

When sub-agents have run first, their results arrive in
state["sub_agent_result"]. The Chat Agent synthesizes those
into a user-friendly response without re-fetching.

Graph wiring:
  input_gate -> (route=chat) -> chat_react <-> chat_tools -> guardrail -> END
  sub-agent -> chat_react -> guardrail -> END
"""

from __future__ import annotations

import json
from typing import Any

import structlog
import yaml
from langchain_core.messages import SystemMessage

from app.agents.llm import create_llm
from app.agents.message_utils import sanitize_messages
from app.agents.state import AgentState
from app.agents.tools.read_tools import CHAT_AGENT_TOOLS
from app.core.config_loader import CONFIG_DIR

logger = structlog.get_logger()

# -- Config (cached at module level) ----------------------------------

_system_prompt: str | None = None


def _get_system_prompt() -> str:
    global _system_prompt
    if _system_prompt is None:
        with open(CONFIG_DIR / "agents.yml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        _system_prompt = cfg.get("chat_agent", {}).get("system_prompt", "")
    return _system_prompt


# -- Context builder ---------------------------------------------------

def _build_context(state: AgentState) -> str:
    """Build context string from user_context, sub_agent_result, and organizer_event."""
    parts = []
    uc = state.get("user_context", {})

    if uc.get("user_id"):
        parts.append(f"User: {uc.get('display_name', uc['user_id'])}")
    if uc.get("budget_min") or uc.get("budget_max"):
        parts.append(f"Budget: ${uc.get('budget_min', '?')}-${uc.get('budget_max', '?')}")
    if uc.get("bedrooms_min"):
        parts.append(f"Bedrooms: {uc['bedrooms_min']}+")
    if uc.get("work_address"):
        parts.append(f"Work: {uc['work_address']}")
    if uc.get("preference_tags"):
        parts.append(f"Preferences: {', '.join(uc['preference_tags'])}")
    if uc.get("active_bookmarks"):
        ids = [b.get("listing_id", "?") for b in uc["active_bookmarks"]]
        parts.append(f"Bookmarked listings: {', '.join(ids[:10])}")
    if uc.get("recent_summaries"):
        for s in uc["recent_summaries"][:2]:
            parts.append(f"Previous session: {s.get('summary', '')[:200]}")

    sub = state.get("sub_agent_result")
    if sub:
        parts.append(f"Sub-agent result:\n{json.dumps(sub, default=str)}")

    # Organizer event — chat_react's signal to verbalize the outcome of
    # an internal write operation. Kinds and required behavior are
    # defined in the system prompt (agents.yml / chat_agent).
    event = state.get("organizer_event")
    if event:
        parts.append(f"Organizer event:\n{json.dumps(event, default=str)}")

    return "\n".join(parts) if parts else ""


# -- Node --------------------------------------------------------------

async def chat_react(state: AgentState) -> dict[str, Any]:
    """Async ReAct node for the Chat Agent.

    Builds system prompt + user context, invokes LLM.

    Tool binding is suppressed when chat_react is running in
    "verbalization-only" mode — i.e., when its sole job is to turn a
    structured organizer_event into natural language. In those modes
    the LLM has everything it needs in context and must not call tools
    (a tool call would preempt the organizer_confirm hand-off in
    should_continue_chat). Removing tools from the binding makes this
    structural rather than prompt-dependent.

    Verbalization-only modes:
      - organizer_event.kind in {"preview", "rejected", "auth_required",
        "clarification", "completed"}
    """
    log = logger.bind(trace_id=state.get("trace_id"), node="chat_react")

    messages = [SystemMessage(content=_get_system_prompt())]

    context = _build_context(state)
    if context:
        messages.append(SystemMessage(content=f"USER CONTEXT:\n{context}"))

    messages.extend(sanitize_messages(state["messages"]))

    event = state.get("organizer_event") or {}
    verbalize_only = event.get("kind") in {
        "preview", "rejected", "auth_required", "clarification", "completed",
    }

    if verbalize_only:
        llm = create_llm()  # no tools bound in verbalization mode
    else:
        llm = create_llm(tools=CHAT_AGENT_TOOLS)

    response = await llm.ainvoke(messages)

    log.info(
        "chat_react_complete",
        has_tool_calls=bool(response.tool_calls),
        tool_calls=len(response.tool_calls) if response.tool_calls else 0,
        mode="verbalize" if verbalize_only else "react",
        organizer_event_kind=event.get("kind") or None,
    )

    return {"messages": [response]}