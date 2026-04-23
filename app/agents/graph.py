"""Vicinity agent graph — async StateGraph with production guardrails.

Graph topology:

  Entry -> input_gate -> conditional routing:

    chat ->
      chat_react <-> chat_tools (ReAct loop, counted, limited)
      -> guardrail (PII + tool health + empty + length)
      -> retry | END

    search ->
      search_react <-> search_tools (counted, limited)
      -> chat_react -> guardrail -> END

    report ->
      report_react <-> report_tools (counted, limited)
      -> chat_react -> guardrail -> END

    organizer ->
      organizer_plan -> organizer_confirm -> interrupt() -> PAUSE
      Command(resume) -> org_tools -> chat_react -> guardrail -> END

    block -> END

Guardrail checks (in order):
  1. PII scrub     — remove SSNs, credit cards, phone numbers
  2. Tool health   — all tools failed → honest fallback
  3. Empty         — retry with nudge (up to max_retries)
  4. Length        — truncate if over limit

Usage:
    graph = build_graph(checkpointer=MemorySaver())
    result = await graph.ainvoke(state, config={
        "configurable": {"thread_id": "t1"},
        "recursion_limit": 50,
    })
"""

from __future__ import annotations

import json
from typing import Any, Optional

import structlog
import yaml
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from app.agents.state import AgentState
from app.agents.chat_agent import chat_react
from app.agents.organizer import organizer_plan, organizer_confirm
from app.agents.search_supervisor import search_node
from app.agents.report_generator import report_node
from app.agents.guardrails import (
    scrub_pii,
    check_tool_health,
    TOOL_FAILURE_MSG,
    EMPTY_RETRY_NUDGE,
    EMPTY_EXHAUSTED_MSG,
)
from app.agents.llm import create_chain
from app.agents.tools.read_tools import CHAT_AGENT_TOOLS
from app.agents.tools.write_tools import ORGANIZER_TOOLS
from app.agents.tools.search_tools import SEARCH_SUPERVISOR_TOOLS, REPORT_GENERATOR_TOOLS
from app.core.config_loader import CONFIG_DIR

logger = structlog.get_logger()


# -- Config (module-level cache) --------------------------------------

_config: Optional[dict] = None


def _load_config() -> dict:
    global _config
    if _config is None:
        with open(CONFIG_DIR / "agents.yml", encoding="utf-8") as f:
            _config = yaml.safe_load(f) or {}
    return _config


def _max_tool_calls() -> int:
    return _load_config().get("tools", {}).get("max_calls_per_turn", 15)


# -- Tool Executor Wrappers -------------------------------------------

_chat_tn = ToolNode(CHAT_AGENT_TOOLS)
_org_tn = ToolNode(ORGANIZER_TOOLS)
_search_tn = ToolNode(SEARCH_SUPERVISOR_TOOLS)
_report_tn = ToolNode(REPORT_GENERATOR_TOOLS)


async def chat_tools(state: AgentState) -> dict[str, Any]:
    result = await _chat_tn.ainvoke(state)
    new_count = len(result.get("messages", []))
    return {**result, "tool_call_count": state.get("tool_call_count", 0) + new_count}


async def org_tools(state: AgentState) -> dict[str, Any]:
    result = await _org_tn.ainvoke(state)
    new_count = len(result.get("messages", []))
    return {**result, "tool_call_count": state.get("tool_call_count", 0) + new_count}


async def search_tools(state: AgentState) -> dict[str, Any]:
    result = await _search_tn.ainvoke(state)
    new_count = len(result.get("messages", []))
    return {**result, "tool_call_count": state.get("tool_call_count", 0) + new_count}


async def report_tools(state: AgentState) -> dict[str, Any]:
    result = await _report_tn.ainvoke(state)
    new_count = len(result.get("messages", []))
    return {**result, "tool_call_count": state.get("tool_call_count", 0) + new_count}


# -- Input Gate --------------------------------------------------------

async def input_gate(state: AgentState) -> dict[str, Any]:
    """Classify user intent. Scrubs PII from input. Falls back to chat."""
    log = logger.bind(trace_id=state.get("trace_id"), node="input_gate")
    cfg = _load_config()
    gate_cfg = cfg.get("input_gate", {})

    if not gate_cfg.get("enabled", True):
        return {"route": "chat"}

    if state.get("pending_confirmation"):
        log.info("gate_pending_confirmation")
        return {"route": "confirm"}

    last_message = state["messages"][-1]
    user_text = last_message.content if hasattr(last_message, "content") else str(last_message)

    # PII scrub on user input
    cleaned, pii_found = scrub_pii(user_text)
    if pii_found:
        log.info("input_pii_scrubbed", types=pii_found)

    max_len = gate_cfg.get("max_query_length", 5000)
    if len(cleaned) > max_len:
        log.warning("query_too_long", length=len(cleaned), max=max_len)
        return {"route": "block", "is_valid": False, "error": "Query too long"}

    system_prompt = gate_cfg.get("system_prompt", "")
    chain = create_chain()

    # ── FIX 1+2: fuller context for the gate classifier ──────────
    # Previous: 4 messages, each truncated to 300 chars.
    # Now: 6 messages (3 exchanges), each up to 1500 chars with
    # head+tail preservation so write suggestions at the end of long
    # listing responses aren't cut off.
    context_messages = []
    all_msgs = state.get("messages", [])
    if len(all_msgs) > 1:
        recent = all_msgs[-7:-1]  # last 6 messages before current
        context_parts = []
        for m in recent:
            if isinstance(m, ToolMessage):
                continue
            if isinstance(m, HumanMessage):
                role = "User"
            elif isinstance(m, AIMessage):
                role = "Assistant"
            else:
                continue
            content = m.content if hasattr(m, "content") else ""
            if not content:
                continue
            if len(content) > 1500:
                content = content[:600] + "\n[...]\n" + content[-600:]
            context_parts.append(f"{role}: {content}")
        if context_parts:
            context_messages = [SystemMessage(
                content="RECENT CONVERSATION CONTEXT:\n" + "\n".join(context_parts)
            )]

    try:
        response = await chain.ainvoke_with_fallback([
            SystemMessage(content=system_prompt),
            *context_messages,
            HumanMessage(content=cleaned),
        ])
        parsed = json.loads(response.content.strip())
        route = parsed.get("route", "chat")
        reason = parsed.get("reason", "")

        valid_routes = {"chat", "organizer", "search", "report", "confirm", "block"}
        if route not in valid_routes:
            log.warning("invalid_route_from_gate", raw=route)
            route = "chat"

        log.info("gate_classified", route=route, reason=reason[:100])
        return {"route": route, "is_valid": route != "block"}

    except Exception as e:
        log.warning("gate_parse_failed", error=str(e)[:200])
        return {"route": "chat"}


# -- Block Node --------------------------------------------------------

async def block_node(state: AgentState) -> dict[str, Any]:
    return {
        "messages": [AIMessage(content=(
            "I'm Vicinity, a Boston housing intelligence assistant. "
            "I can help with apartment searches, neighborhood safety, "
            "crime data, commute routes, and livability analysis. "
            "Could you rephrase your question in that context?"
        ))]
    }


# -- Guardrail ---------------------------------------------------------

async def guardrail(state: AgentState) -> dict[str, Any]:
    """Production guardrail: PII → tool health → empty → length.

    Checks run in order. First failure triggers the appropriate action.
    The retry budget (empty_retries) is shared across empty and quality
    retries to prevent infinite loops.
    """
    log = logger.bind(trace_id=state.get("trace_id"), node="guardrail")
    cfg = _load_config()
    guard_cfg = cfg.get("guardrail", {})
    max_len = guard_cfg.get("max_response_length", 15000)
    max_retries = guard_cfg.get("max_retries", 2)
    retries = state.get("empty_retries", 0)

    messages = state.get("messages", [])
    if not messages:
        return {}

    last = messages[-1]
    content = last.content if hasattr(last, "content") else ""

    # -- 1. PII scrub on output --
    cleaned, pii_found = scrub_pii(content)
    if pii_found:
        log.warning("output_pii_scrubbed", types=pii_found)
        content = cleaned
        # Replace the message with scrubbed version
        return {"messages": [AIMessage(content=content)]}

    # -- 2. Tool health: all tools failed → honest fallback --
    health = check_tool_health(messages)
    if health["all_failed"] and health["total"] > 0:
        log.error("all_tools_failed", total=health["total"], errors=health["errors"])
        return {"messages": [AIMessage(content=TOOL_FAILURE_MSG)]}

    # -- 3. Empty response → retry with nudge --
    if not content.strip():
        if retries < max_retries:
            log.warning("empty_response", attempt=retries + 1, max=max_retries)
            return {"empty_retries": retries + 1}
        log.error("empty_response_exhausted", attempts=retries)
        return {"messages": [AIMessage(content=EMPTY_EXHAUSTED_MSG)]}

    # -- 4. Length truncation --
    if len(content) > max_len:
        log.info("response_truncated", original=len(content), max=max_len)
        return {"messages": [AIMessage(content=content[:max_len] + "\n\n[Response truncated]")]}

    # -- All checks passed --
    log.info(
        "guardrail_passed",
        tool_calls=health["total"],
        tool_errors=health["errors"],
        response_length=len(content),
    )
    return {}


# -- Routing Functions -------------------------------------------------

def route_after_gate(state: AgentState) -> str:
    route = state.get("route", "chat")
    mapping = {
        "block": "block",
        "organizer": "organizer_plan",
        "confirm": "organizer_confirm",
        "search": "search_react",
        "report": "report_react",
    }
    return mapping.get(route, "chat_react")


def route_after_plan(state: AgentState) -> str:
    if state.get("pending_confirmation"):
        return "organizer_confirm"
    messages = state.get("messages", [])
    if messages:
        last = messages[-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "org_tools"
    return "chat_react"


def route_after_confirm(state: AgentState) -> str:
    """Route after organizer_confirm: approve -> tools, reject -> chat, modify -> re-plan."""
    # ── FIX 3: state has sub_agent_result=None (not missing), so
    # .get("sub_agent_result", {}) returns None. Use `or {}`. ─────
    sub_result = state.get("sub_agent_result") or {}
    if sub_result.get("status") == "modified":
        return "organizer_plan"
 
    # Check if tool_calls present (approval -> execute)
    messages = state.get("messages", [])
    if messages:
        last = messages[-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "org_tools"
 
    # Rejection or no tool_calls -> back to chat
    return "chat_react"

def _patch_pending_tool_calls(state: AgentState):
    """Inject synthetic ToolMessages for any pending tool_calls on the last AIMessage.
 
    When the tool limit is hit mid-turn, the last AIMessage may contain
    tool_calls that were never executed (no corresponding ToolMessages).
    The LLM API rejects this sequence. This function appends stub
    ToolMessages so the conversation history stays valid.
 
    Mutates state["messages"] in place — safe because this runs inside
    the graph routing function before the state update is committed.
    """
    messages = state.get("messages", [])
    if not messages:
        return
 
    last = messages[-1]
    if not hasattr(last, "tool_calls") or not last.tool_calls:
        return
 
    stubs = []
    for tc in last.tool_calls:
        stubs.append(ToolMessage(
            content=json.dumps({
                "success": False,
                "error": "Tool call skipped: maximum tool calls per turn reached. "
                         "Please ask again to continue.",
            }),
            tool_call_id=tc["id"],
            name=tc["name"],
        ))
 
    if stubs:
        messages.extend(stubs)
        logger.info("tool_calls_patched",
                    patched=len(stubs),
                    tools=[tc["name"] for tc in last.tool_calls])

def _should_continue(state: AgentState, tools_node: str, done_node: str) -> str:
    """Shared ReAct loop check with tool limit enforcement.
    """
    max_calls = _max_tool_calls()
    if state.get("tool_call_count", 0) >= max_calls:
        logger.warning("tool_limit_reached", count=state.get("tool_call_count", 0))
        _patch_pending_tool_calls(state)
        return done_node
    messages = state.get("messages", [])
    if messages:
        last = messages[-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return tools_node
    return done_node


def should_continue_chat(state: AgentState) -> str:
    return _should_continue(state, "chat_tools", "guardrail")


def should_continue_search(state: AgentState) -> str:
    return _should_continue(state, "search_tools", "chat_react")


def should_continue_report(state: AgentState) -> str:
    return _should_continue(state, "report_tools", "chat_react")


def route_after_guardrail(state: AgentState) -> str:
    """Route based on guardrail outcome: retry or END."""
    cfg = _load_config()
    max_retries = cfg.get("guardrail", {}).get("max_retries", 2)
    retries = state.get("empty_retries", 0)

    messages = state.get("messages", [])
    if not messages:
        return END

    last = messages[-1]
    content = last.content if hasattr(last, "content") else ""

    # Retry if guardrail flagged empty (incremented empty_retries)
    if not content.strip() and retries > 0 and retries <= max_retries:
        return "chat_react_retry"

    return END


# -- Retry with nudge -------------------------------------------------

async def chat_react_retry(state: AgentState) -> dict[str, Any]:
    """Re-run chat_react with a simple nudge injected."""
    log = logger.bind(trace_id=state.get("trace_id"), node="chat_react_retry")
    log.info("retrying_with_nudge", attempt=state.get("empty_retries", 0))

    nudge_msg = SystemMessage(content=EMPTY_RETRY_NUDGE)

    from app.agents.chat_agent import chat_react as _react
    # Pass state as-is; the nudge is added to the returned messages
    # so the next guardrail pass sees a (hopefully non-empty) response
    result = await _react(state)

    return {
        "messages": [nudge_msg] + result.get("messages", []),
        "empty_retries": state.get("empty_retries", 0) + 1,
    }


# -- Graph Builder -----------------------------------------------------

def build_graph(checkpointer: Optional[BaseCheckpointSaver] = None) -> Any:
    """Build and compile the Vicinity agent graph.

    Args:
        checkpointer: Required for interrupt(). MemorySaver for dev,
            AsyncPostgresSaver for production.

    Returns:
        Compiled StateGraph. Invoke with:
            graph.ainvoke(state, config={
                "configurable": {"thread_id": "..."},
                "recursion_limit": 50,
            })
    """
    graph = StateGraph(AgentState)

    # -- Nodes --
    graph.add_node("input_gate", input_gate)
    graph.add_node("block", block_node)
    graph.add_node("chat_react", chat_react)
    graph.add_node("chat_tools", chat_tools)
    graph.add_node("chat_react_retry", chat_react_retry)
    graph.add_node("organizer_plan", organizer_plan)
    graph.add_node("organizer_confirm", organizer_confirm)
    graph.add_node("org_tools", org_tools)
    graph.add_node("search_react", search_node)
    graph.add_node("search_tools", search_tools)
    graph.add_node("report_react", report_node)
    graph.add_node("report_tools", report_tools)
    graph.add_node("guardrail", guardrail)

    # -- Entry --
    graph.set_entry_point("input_gate")

    # -- Routing --
    graph.add_conditional_edges("input_gate", route_after_gate, {
        "block": "block",
        "organizer_plan": "organizer_plan",
        "organizer_confirm": "organizer_confirm",
        "chat_react": "chat_react",
        "search_react": "search_react",
        "report_react": "report_react",
    })

    graph.add_edge("block", END)

    graph.add_conditional_edges("chat_react", should_continue_chat, {
        "chat_tools": "chat_tools",
        "guardrail": "guardrail",
    })
    graph.add_edge("chat_tools", "chat_react")

    graph.add_conditional_edges("guardrail", route_after_guardrail, {
        "chat_react_retry": "chat_react_retry",
        END: END,
    })
    graph.add_edge("chat_react_retry", "guardrail")

    graph.add_conditional_edges("organizer_plan", route_after_plan, {
        "organizer_confirm": "organizer_confirm",
        "org_tools": "org_tools",
        "chat_react": "chat_react",
    })
    graph.add_conditional_edges("organizer_confirm", route_after_confirm, {
        "org_tools": "org_tools",
        "chat_react": "chat_react",
        "organizer_plan": "organizer_plan",
    })
    graph.add_edge("org_tools", "chat_react")

    graph.add_conditional_edges("search_react", should_continue_search, {
        "search_tools": "search_tools",
        "chat_react": "chat_react",
    })
    graph.add_edge("search_tools", "search_react")

    graph.add_conditional_edges("report_react", should_continue_report, {
        "report_tools": "report_tools",
        "chat_react": "chat_react",
    })
    graph.add_edge("report_tools", "report_react")

    return graph.compile(checkpointer=checkpointer)