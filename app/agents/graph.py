"""Vicinity agent graph — async StateGraph with production guardrails.

Topology:

  Entry -> input_gate -> conditional routing:

    chat      -> chat_react <-> chat_tools  -> guardrail -> END
    search    -> search_react <-> search_tools  -> chat_react -> guardrail -> END
    report    -> report_react <-> report_tools  -> chat_react -> guardrail -> END
    organizer -> organizer_plan
                   |-> organizer_confirm -> interrupt() -> PAUSE
                   |   (resumed) -> approve: org_tools -> chat_react
                   |              -> reject: chat_react
                   |              -> modify: organizer_plan (re-plan)
                   |-> org_tools (no-confirm tools) -> chat_react
                   |-> chat_react (no tools)
    block     -> END

Tool-limit handling:
  When tool_call_count >= max_calls_per_turn, should_continue_* routes
  directly to the done node (guardrail for chat, chat_react for sub-agents),
  even if pending tool_calls are still attached to the last AIMessage.
  sanitize_messages strips those orphan tool_calls on the next LLM input,
  so no separate patching step is needed.

Message sanitization:
  Every agent node (chat_react, search_node, report_node, organizer_plan)
  calls sanitize_messages on state["messages"] before building the LLM
  input. Orphaned tool_calls from any source (tool-limit cutoff, crashes,
  partial state writes, Snowflake checkpointer loads, HITL rejection)
  can never reach an LLM.

Guardrail checks (run after chat_react finishes):
  1. PII scrub     — remove SSNs, credit cards, phone numbers from output.
  2. Tool health   — all tools failed → honest fallback message.
  3. Empty         — retry with nudge (up to max_retries).
  4. Length        — truncate if over limit.
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

from app.agents.chat_agent import chat_react
from app.agents.guardrails import (
    EMPTY_EXHAUSTED_MSG,
    EMPTY_RETRY_NUDGE,
    TOOL_FAILURE_MSG,
    check_tool_health,
    scrub_pii,
)
from app.agents.llm import create_chain
from app.agents.organizer import organizer_confirm, organizer_plan
from app.agents.report_generator import report_node
from app.agents.search_supervisor import search_node
from app.agents.state import AgentState
from app.agents.tools.read_tools import CHAT_AGENT_TOOLS
from app.agents.tools.search_tools import (
    REPORT_GENERATOR_TOOLS,
    SEARCH_SUPERVISOR_TOOLS,
)
from app.agents.tools.write_tools import ORGANIZER_TOOLS
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

    cleaned, pii_found = scrub_pii(user_text)
    if pii_found:
        log.info("input_pii_scrubbed", types=pii_found)

    max_len = gate_cfg.get("max_query_length", 5000)
    if len(cleaned) > max_len:
        log.warning("query_too_long", length=len(cleaned), max=max_len)
        return {"route": "block", "is_valid": False, "error": "Query too long"}

    system_prompt = gate_cfg.get("system_prompt", "")
    chain = create_chain()

    # Recent conversation context for pronoun resolution
    context_messages = []
    all_msgs = state.get("messages", [])
    if len(all_msgs) > 1:
        recent = all_msgs[-7:-1]
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
            context_messages.append(SystemMessage(
                content="RECENT CONVERSATION CONTEXT:\n" + "\n".join(context_parts)
            ))

    # User state snapshot
    uc = state.get("user_context", {}) or {}
    signed_in = bool(uc.get("user_id"))
    bookmark_count = len(uc.get("active_bookmarks") or [])
    user_state_parts = [
        f"Signed in: {'yes' if signed_in else 'no'}",
        f"Active bookmarks: {bookmark_count}",
    ]
    if uc.get("work_address"):
        user_state_parts.append("Work address saved: yes")
    if uc.get("budget_min") or uc.get("budget_max"):
        user_state_parts.append("Budget saved: yes")
    context_messages.append(SystemMessage(
        content="USER STATE:\n" + "\n".join(user_state_parts)
    ))

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
            reason = f"invalid_route_fallback (raw={parsed.get('route')})"

        # Belt-and-suspenders: report needs >= 2 bookmarks
        if route == "report" and bookmark_count < 2:
            log.info("report_downgraded_to_chat", bookmarks=bookmark_count)
            route = "chat"
            reason = f"report_requires_2_bookmarks_user_has_{bookmark_count}"

        log.info("gate_classified", route=route, reason=reason[:100])
        return {"route": route, "is_valid": route != "block"}

    except Exception as e:
        log.warning("gate_parse_failed", error=str(e)[:200])
        return {"route": "chat"}


# -- Block --------------------------------------------------------------

async def block_node(state: AgentState) -> dict[str, Any]:
    return {
        "messages": [AIMessage(content=(
            "I'm Vicinity, a Boston housing intelligence assistant. "
            "I can help with apartment searches, neighborhood safety, "
            "crime data, commute routes, and livability analysis. "
            "Could you rephrase your question in that context?"
        ))]
    }


# -- Tool-limit patch node --------------------------------------------

# -- Guardrail ---------------------------------------------------------

async def guardrail(state: AgentState) -> dict[str, Any]:
    """PII -> tool health -> empty -> length."""
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

    # 1. PII scrub on output
    cleaned, pii_found = scrub_pii(content)
    if pii_found:
        log.warning("output_pii_scrubbed", types=pii_found)
        return {"messages": [AIMessage(content=cleaned)]}

    # 2. Tool health
    health = check_tool_health(messages)
    if health["all_failed"] and health["total"] > 0:
        log.error("all_tools_failed", total=health["total"], errors=health["errors"])
        return {"messages": [AIMessage(content=TOOL_FAILURE_MSG)]}

    # 3. Empty response
    if not content.strip():
        if retries < max_retries:
            log.warning("empty_response", attempt=retries + 1, max=max_retries)
            return {"empty_retries": retries + 1}
        log.error("empty_response_exhausted", attempts=retries)
        return {"messages": [AIMessage(content=EMPTY_EXHAUSTED_MSG)]}

    # 4. Length truncation
    if len(content) > max_len:
        log.info("response_truncated", original=len(content), max=max_len)
        return {"messages": [AIMessage(content=content[:max_len] + "\n\n[Response truncated]")]}

    log.info("guardrail_passed",
             tool_calls=health["total"], tool_errors=health["errors"],
             response_length=len(content))
    return {}


async def chat_react_retry(state: AgentState) -> dict[str, Any]:
    """Append the empty-response nudge as a SystemMessage and loop back to chat_react."""
    log = logger.bind(trace_id=state.get("trace_id"), node="chat_react_retry")
    log.info("retrying_with_nudge", attempt=state.get("empty_retries", 0))
    return {
        "messages": [SystemMessage(content=EMPTY_RETRY_NUDGE)],
        "empty_retries": state.get("empty_retries", 0) + 1,
    }


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
    """Route after organizer_plan.

    Order:
      1. pending_confirmation set  -> organizer_confirm
      2. tool_calls pending        -> org_tools (no-confirm tools)
      3. fallthrough               -> chat_react
    """
    if state.get("pending_confirmation"):
        return "organizer_confirm"

    messages = state.get("messages", [])
    if messages:
        last = messages[-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "org_tools"
    return "chat_react"


def route_after_confirm(state: AgentState) -> str:
    """After organizer_confirm: approve -> tools, reject -> chat, modify -> re-plan."""
    sub_result = state.get("sub_agent_result") or {}
    if sub_result.get("status") == "modified":
        return "organizer_plan"

    messages = state.get("messages", [])
    if messages:
        last = messages[-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "org_tools"

    return "chat_react"


def _has_pending_tool_calls(state: AgentState) -> bool:
    messages = state.get("messages") or []
    if not messages:
        return False
    last = messages[-1]
    return bool(getattr(last, "tool_calls", None))


def _should_continue(state: AgentState, tools_node: str, done_node: str) -> str:
    """Shared ReAct-loop router.

    Used by chat, search, and report supervisors. Decides whether the
    next step is another tool execution or the exit node (done_node).

    Priority:
      1. Tool limit reached -> done_node (orphan tool_calls, if any, are
         stripped by sanitize_messages on the next LLM input, so no
         patching step is needed).
      2. Pending tool_calls on the last AIMessage -> tools_node.
      3. Otherwise (LLM produced a final answer) -> done_node.
    """
    if state.get("tool_call_count", 0) >= _max_tool_calls():
        if _has_pending_tool_calls(state):
            logger.warning(
                "tool_limit_reached",
                count=state.get("tool_call_count", 0),
                tools_node=tools_node,
            )
        return done_node

    if _has_pending_tool_calls(state):
        return tools_node

    return done_node


def should_continue_chat(state: AgentState) -> str:
    """Chat ReAct router: tools -> chat_tools, done -> guardrail."""
    return _should_continue(state, "chat_tools", "guardrail")


def should_continue_search(state: AgentState) -> str:
    """Search ReAct router: tools -> search_tools, done -> chat_react."""
    return _should_continue(state, "search_tools", "chat_react")


def should_continue_report(state: AgentState) -> str:
    """Report ReAct router: tools -> report_tools, done -> chat_react."""
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

    if not content.strip() and 0 < retries <= max_retries:
        return "chat_react_retry"

    return END


# -- Graph Builder -----------------------------------------------------

def build_graph(checkpointer: Optional[BaseCheckpointSaver] = None) -> Any:
    """Build and compile the Vicinity agent graph."""
    graph = StateGraph(AgentState)

    # Nodes
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

    graph.set_entry_point("input_gate")

    # Gate dispatch
    graph.add_conditional_edges("input_gate", route_after_gate, {
        "block": "block",
        "organizer_plan": "organizer_plan",
        "organizer_confirm": "organizer_confirm",
        "chat_react": "chat_react",
        "search_react": "search_react",
        "report_react": "report_react",
    })

    graph.add_edge("block", END)

    # Chat loop
    graph.add_conditional_edges("chat_react", should_continue_chat, {
        "chat_tools": "chat_tools",
        "guardrail": "guardrail",
    })
    graph.add_edge("chat_tools", "chat_react")

    # Guardrail
    graph.add_conditional_edges("guardrail", route_after_guardrail, {
        "chat_react_retry": "chat_react_retry",
        END: END,
    })
    graph.add_edge("chat_react_retry", "chat_react")

    # Organizer flow
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

    # Search flow
    graph.add_conditional_edges("search_react", should_continue_search, {
        "search_tools": "search_tools",
        "chat_react": "chat_react",
    })
    graph.add_edge("search_tools", "search_react")

    # Report flow
    graph.add_conditional_edges("report_react", should_continue_report, {
        "report_tools": "report_tools",
        "chat_react": "chat_react",
    })
    graph.add_edge("report_tools", "report_react")

    return graph.compile(checkpointer=checkpointer)