"""Vicinity agent graph — async StateGraph with production guardrails.

Graph topology:

  Entry -> input_gate -> conditional routing:

    chat ->
      chat_react <-> chat_tools (ReAct loop, counted, limited)
      -> should_continue_chat:
           tool_calls pending      -> chat_tools
           [ROUTE: X] marker found -> strip_reroute_marker -> input_gate
           otherwise               -> guardrail -> (retry | END)

    search ->
      search_react <-> search_tools
      -> route_after_search:
           tool_calls pending              -> search_tools
           sub_agent_result.status=bounce  -> record_bounce -> input_gate
           otherwise                       -> chat_react

    report ->
      report_react <-> report_tools
      -> route_after_report:
           tool_calls pending              -> report_tools
           sub_agent_result.status=bounce  -> record_bounce -> input_gate
           otherwise                       -> chat_react

    organizer ->
      organizer_plan
      -> route_after_plan:
           sub_agent_result.status=bounce -> record_bounce -> input_gate
           pending_confirmation set       -> organizer_confirm -> interrupt() -> PAUSE
           tool_calls (no-confirm tools)  -> org_tools -> chat_react
           otherwise                      -> chat_react

      organizer_confirm (resumed)
      -> (approved)  -> org_tools -> chat_react
      -> (rejected)  -> chat_react
      -> (modified)  -> organizer_plan (re-plan)

    block -> END

Bounce + reroute architecture:

  All three sub-agents (search, report, organizer) can set
  sub_agent_result.status="bounce" when the user query is outside
  their domain. The graph's post-sub-agent routers detect the bounce
  and route to record_bounce -> input_gate.

  Chat agent's reroute path uses a textual marker ("[ROUTE: organizer]")
  because chat's output IS the user-facing response. strip_reroute_marker
  removes the marker before streaming continues.

  All reroutes increment reroute_count and append to reroute_history.
  The gate reads history on re-entry and picks a different route.

  Cap: MAX_REROUTES = 2. Third attempt forces route=chat as the
  universal fallback to prevent infinite loops.

Observability:

  Every reroute emits a structured log event. Frontend ToolLog consumes
  these for developer transparency. The user never sees reroute
  internals — the final response streams silently after any reroutes.

Guardrail checks (run after chat_react):
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
import re
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


# Maximum times the graph returns to input_gate during one user turn.
# Third attempt forces route=chat as universal fallback. Prevents
# infinite ping-pong between agents that all bounce.
MAX_REROUTES = 2


# Regex for the chat_agent reroute marker. Must be on its own line
# near the end. Case-insensitive so "[route: organizer]" also works.
_ROUTE_MARKER = re.compile(
    r"\[ROUTE:\s*(organizer|search|report)\]\s*$",
    re.IGNORECASE | re.MULTILINE,
)


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

def _build_reroute_hint(state: AgentState) -> Optional[str]:
    """Build a rich SystemMessage hint for the gate on re-entry.

    Includes: previous routes tried, WHY each failed, and the gate's
    OWN previous reasoning. This lets the LLM see its own classification
    mistake rather than just being told to pick differently.
    """
    history = state.get("reroute_history") or []
    if not history:
        return None

    lines = ["=== PREVIOUS ROUTE ATTEMPTS THIS TURN (DO NOT REPEAT) ==="]
    for i, rec in enumerate(history, 1):
        lines.append(
            f"{i}. You classified this message as route='{rec.get('original_route', '?')}' "
            f"(your reasoning: \"{rec.get('gate_reasoning', '?')}\")."
        )
        lines.append(
            f"   That was rejected by {rec.get('from_agent', '?')} "
            f"(trigger: {rec.get('trigger', '?')})."
        )
        lines.append(f"   Reason: {rec.get('reason', '')[:300]}")

    lines.append("")
    lines.append(
        "Your previous classification was WRONG. Read the rejection "
        "reason carefully — it reveals what the query is actually asking "
        "for. Pick a DIFFERENT route that matches. If no specialized "
        "route fits, pick 'chat' — Chat Agent is the universal fallback."
    )
    return "\n".join(lines)


async def input_gate(state: AgentState) -> dict[str, Any]:
    """Classify user intent. Scrubs PII from input. Falls back to chat.

    Supports re-entry: if reroute_history is non-empty, injects a
    rich hint explaining previous failures. Capped at MAX_REROUTES.
    """
    log = logger.bind(trace_id=state.get("trace_id"), node="input_gate")
    cfg = _load_config()
    gate_cfg = cfg.get("input_gate", {})

    if not gate_cfg.get("enabled", True):
        return {"route": "chat", "gate_reasoning": "gate_disabled"}

    if state.get("pending_confirmation"):
        log.info("gate_pending_confirmation")
        return {"route": "confirm", "gate_reasoning": "pending_confirmation_present"}

    # Reroute safety cap
    reroute_count = state.get("reroute_count", 0)
    if reroute_count >= MAX_REROUTES:
        log.warning("reroute_cap_hit", count=reroute_count)
        return {
            "route": "chat",
            "gate_reasoning": f"reroute_cap_exceeded_after_{reroute_count}_attempts",
        }

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

    # Recent conversation context
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

    # User state snapshot (auth + bookmarks) — gate uses these for
    # report bookmark-count rule and auth-aware routing.
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

    # Reroute hint — only present on re-entry
    reroute_hint = _build_reroute_hint(state)
    if reroute_hint:
        context_messages.append(SystemMessage(content=reroute_hint))

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

        # Belt-and-suspenders: report needs ≥2 bookmarks. The prompt
        # tells the gate this, but enforce it here too. report_generator
        # also checks this as a pre-LLM bounce — triple defense.
        if route == "report" and bookmark_count < 2:
            log.info(
                "report_downgraded_to_chat",
                bookmarks=bookmark_count, original_reason=reason[:100],
            )
            route = "chat"
            reason = f"report_requires_2_bookmarks_user_has_{bookmark_count}"

        log.info(
            "gate_classified",
            route=route,
            reason=reason[:100],
            reroute_count=reroute_count,
        )
        return {
            "route": route,
            "is_valid": route != "block",
            "gate_reasoning": reason[:500],
        }

    except Exception as e:
        log.warning("gate_parse_failed", error=str(e)[:200])
        return {"route": "chat", "gate_reasoning": f"parse_failed: {str(e)[:200]}"}


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
    """Production guardrail: PII -> tool health -> empty -> length.

    Checks run in order. First failure triggers the appropriate action.
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
    """Route after organizer_plan.

    Order:
      1. sub_agent_result.status=="bounce"  -> record_bounce -> input_gate
      2. pending_confirmation set           -> organizer_confirm
      3. tool_calls pending                 -> org_tools
      4. fallthrough                        -> chat_react
    """
    sub_result = state.get("sub_agent_result") or {}
    if sub_result.get("status") == "bounce" and state.get("reroute_count", 0) < MAX_REROUTES:
        return "record_bounce"

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
    sub_result = state.get("sub_agent_result") or {}
    if sub_result.get("status") == "modified":
        return "organizer_plan"

    messages = state.get("messages", [])
    if messages:
        last = messages[-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "org_tools"

    return "chat_react"


def _patch_pending_tool_calls(state: AgentState):
    """Inject synthetic ToolMessages for any pending tool_calls on the last AIMessage."""
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


# -- Reroute marker detection (chat_agent [ROUTE: X]) ------------------

def _detect_reroute_marker(state: AgentState) -> Optional[str]:
    """Returns target route lowercased if last AIMessage ends with [ROUTE: X]."""
    messages = state.get("messages", [])
    if not messages:
        return None
    last = messages[-1]
    if not isinstance(last, AIMessage):
        return None
    content = last.content if hasattr(last, "content") else ""
    if not isinstance(content, str):
        return None
    m = _ROUTE_MARKER.search(content)
    if not m:
        return None
    return m.group(1).lower()


async def strip_reroute_marker(state: AgentState) -> dict[str, Any]:
    """Strip [ROUTE: ...] marker from last AIMessage, record, reroute.

    Runs before re-entering input_gate. Ensures the marker never leaks
    to the user, records the reroute for the gate's history hint, and
    clears sub_agent_result so the next pass starts fresh.
    """
    log = logger.bind(trace_id=state.get("trace_id"), node="strip_reroute_marker")

    messages = state.get("messages", [])
    last = messages[-1] if messages else None
    if not isinstance(last, AIMessage):
        return {}

    content = last.content or ""
    marker_match = _ROUTE_MARKER.search(content)
    target_route = marker_match.group(1).lower() if marker_match else "chat"

    # Strip marker + trailing whitespace
    stripped = _ROUTE_MARKER.sub("", content).rstrip()

    # Replace message in-place. If the original has an id, preserve it
    # so add_messages replaces rather than appends.
    if getattr(last, "id", None):
        new_msg = AIMessage(content=stripped, id=last.id)
    else:
        new_msg = AIMessage(content=stripped)

    reroute_record = {
        "from_agent": "chat",
        "reason": f"chat_agent requested reroute to {target_route}",
        "original_route": "chat",
        "trigger": "marker",
        "gate_reasoning": state.get("gate_reasoning", ""),
    }

    log.info(
        "chat_reroute_marker_stripped",
        target_route=target_route,
        stripped_chars=len(content) - len(stripped),
    )

    return {
        "messages": [new_msg],
        "reroute_count": state.get("reroute_count", 0) + 1,
        "reroute_history": [reroute_record],
        "sub_agent_result": None,
    }


async def record_bounce(state: AgentState) -> dict[str, Any]:
    """Append a bounce record and clear sub_agent_result.

    Runs between a bouncing sub-agent and input_gate. Bumps
    reroute_count, writes a RerouteRecord with the bounce details
    including the gate's previous reasoning so the gate can see its
    own classification mistake on re-entry.
    """
    log = logger.bind(trace_id=state.get("trace_id"), node="record_bounce")

    sub_result = state.get("sub_agent_result") or {}
    from_agent = sub_result.get("agent", "unknown")
    reason = sub_result.get("reason") or (sub_result.get("content") or "")[:200] or "no reason given"

    # Map agent name back to the route that was attempted
    agent_to_route = {
        "search_supervisor": "search",
        "report_generator": "report",
        "organizer": "organizer",
    }
    original_route = agent_to_route.get(from_agent, from_agent)

    reroute_record = {
        "from_agent": from_agent,
        "reason": reason[:500],
        "original_route": original_route,
        "trigger": "bounce",
        "gate_reasoning": state.get("gate_reasoning", ""),
    }

    log.info(
        "bounce_recorded",
        from_agent=from_agent,
        original_route=original_route,
        reason=reason[:100],
    )

    return {
        "reroute_count": state.get("reroute_count", 0) + 1,
        "reroute_history": [reroute_record],
        "sub_agent_result": None,
    }


# -- Chat ReAct loop routing ------------------------------------------

def should_continue_chat(state: AgentState) -> str:
    """Chat ReAct loop router with reroute-marker detection.

    Priority:
      1. Tool limit hit       -> patch + guardrail
      2. Tool calls pending   -> chat_tools
      3. [ROUTE: X] marker    -> strip_reroute_marker
      4. Otherwise            -> guardrail
    """
    max_calls = _max_tool_calls()
    if state.get("tool_call_count", 0) >= max_calls:
        logger.warning("tool_limit_reached", count=state.get("tool_call_count", 0))
        _patch_pending_tool_calls(state)
        return "guardrail"

    messages = state.get("messages", [])
    if messages:
        last = messages[-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "chat_tools"

    # Reroute marker check — only honored under the cap
    if _detect_reroute_marker(state) is not None:
        if state.get("reroute_count", 0) < MAX_REROUTES:
            return "strip_reroute_marker"
        logger.warning(
            "reroute_marker_ignored_over_cap",
            reroute_count=state.get("reroute_count", 0),
        )

    return "guardrail"


# -- Sub-agent routers with bounce detection --------------------------

def route_after_search(state: AgentState) -> str:
    """Post-search-node routing with bounce and tool-loop detection."""
    max_calls = _max_tool_calls()
    if state.get("tool_call_count", 0) >= max_calls:
        logger.warning("tool_limit_reached", count=state.get("tool_call_count", 0))
        _patch_pending_tool_calls(state)
        return "chat_react"

    messages = state.get("messages", [])
    if messages:
        last = messages[-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "search_tools"

    sub_result = state.get("sub_agent_result") or {}
    if sub_result.get("status") == "bounce" and state.get("reroute_count", 0) < MAX_REROUTES:
        return "record_bounce"

    return "chat_react"


def route_after_report(state: AgentState) -> str:
    """Post-report-node routing — same pattern as search."""
    max_calls = _max_tool_calls()
    if state.get("tool_call_count", 0) >= max_calls:
        logger.warning("tool_limit_reached", count=state.get("tool_call_count", 0))
        _patch_pending_tool_calls(state)
        return "chat_react"

    messages = state.get("messages", [])
    if messages:
        last = messages[-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "report_tools"

    sub_result = state.get("sub_agent_result") or {}
    if sub_result.get("status") == "bounce" and state.get("reroute_count", 0) < MAX_REROUTES:
        return "record_bounce"

    return "chat_react"


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
    result = await _react(state)

    return {
        "messages": [nudge_msg] + result.get("messages", []),
        "empty_retries": state.get("empty_retries", 0) + 1,
    }


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
    graph.add_node("strip_reroute_marker", strip_reroute_marker)
    graph.add_node("record_bounce", record_bounce)
    graph.add_node("organizer_plan", organizer_plan)
    graph.add_node("organizer_confirm", organizer_confirm)
    graph.add_node("org_tools", org_tools)
    graph.add_node("search_react", search_node)
    graph.add_node("search_tools", search_tools)
    graph.add_node("report_react", report_node)
    graph.add_node("report_tools", report_tools)
    graph.add_node("guardrail", guardrail)

    # Entry
    graph.set_entry_point("input_gate")

    # Gate routing
    graph.add_conditional_edges("input_gate", route_after_gate, {
        "block": "block",
        "organizer_plan": "organizer_plan",
        "organizer_confirm": "organizer_confirm",
        "chat_react": "chat_react",
        "search_react": "search_react",
        "report_react": "report_react",
    })

    graph.add_edge("block", END)

    # Chat loop + reroute marker handling
    graph.add_conditional_edges("chat_react", should_continue_chat, {
        "chat_tools": "chat_tools",
        "strip_reroute_marker": "strip_reroute_marker",
        "guardrail": "guardrail",
    })
    graph.add_edge("chat_tools", "chat_react")
    graph.add_edge("strip_reroute_marker", "input_gate")

    # Guardrail
    graph.add_conditional_edges("guardrail", route_after_guardrail, {
        "chat_react_retry": "chat_react_retry",
        END: END,
    })
    graph.add_edge("chat_react_retry", "guardrail")

    # Organizer flow (with bounce detection in route_after_plan)
    graph.add_conditional_edges("organizer_plan", route_after_plan, {
        "organizer_confirm": "organizer_confirm",
        "org_tools": "org_tools",
        "chat_react": "chat_react",
        "record_bounce": "record_bounce",
    })
    graph.add_conditional_edges("organizer_confirm", route_after_confirm, {
        "org_tools": "org_tools",
        "chat_react": "chat_react",
        "organizer_plan": "organizer_plan",
    })
    graph.add_edge("org_tools", "chat_react")

    # Search flow with bounce
    graph.add_conditional_edges("search_react", route_after_search, {
        "search_tools": "search_tools",
        "chat_react": "chat_react",
        "record_bounce": "record_bounce",
    })
    graph.add_edge("search_tools", "search_react")

    # Report flow with bounce
    graph.add_conditional_edges("report_react", route_after_report, {
        "report_tools": "report_tools",
        "chat_react": "chat_react",
        "record_bounce": "record_bounce",
    })
    graph.add_edge("report_tools", "report_react")

    # Bounce flows back into gate for reclassification
    graph.add_edge("record_bounce", "input_gate")

    return graph.compile(checkpointer=checkpointer)