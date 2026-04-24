"""Vicinity agent graph — async StateGraph with production guardrails.

Single-voice architecture:

  Only chat_react produces user-visible content. All other agent nodes
  (organizer_plan, organizer_confirm, search_node, report_node) write
  structured signals to state (organizer_event, sub_agent_result,
  pending_confirmation) that chat_react reads and verbalizes. The user
  sees one coherent assistant, not a committee.

  The one exception is report_node: the Report Generator produces a
  fully-structured document that IS the final user-facing artifact, so
  it goes directly to guardrail. Chat re-synthesis would strip the
  template structure.

Topology:

  Entry -> input_gate -> conditional routing:

    chat      -> chat_react <-> chat_tools                    -> guardrail -> END

    search    -> search_react <-> search_tools                -> chat_react -> guardrail -> END
                                                              \\-> handoff_to_organizer -> organizer_plan (compound)

    report    -> report_react <-> report_tools                -> guardrail -> END
                 (Report IS the final output; no chat re-synthesis.)

    organizer -> organizer_plan
                   |-> no-confirm tool          -> org_tools -> chat_react -> guardrail -> END
                   |-> preview/clarification/   -> chat_react (verbalize)
                       auth_required                |
                                                    |-- if preview -> organizer_confirm (interrupt)
                                                    |                   |
                                                    |                   |-- approve -> org_tools -> chat_react -> guardrail -> END
                                                    |                   |-- reject  -> chat_react -> guardrail -> END
                                                    |                   \\-- modify  -> organizer_plan (re-plan)
                                                    \\-- else (clarification/auth) -> guardrail -> END
    block     -> END

State signals chat_react reads:
  organizer_event.kind: preview | completed | rejected | clarification | auth_required
    - preview         -> ask the user for confirmation in natural language
    - completed       -> acknowledge the write briefly
    - rejected        -> acknowledge cancellation, offer next step
    - clarification   -> pass the organizer's question through naturally
    - auth_required   -> exact sign-in prompt
  sub_agent_result.status: complete | empty | error | no_action | modified
    - complete        -> present search results (organizer reports never flow here)
    - empty/error     -> acknowledge, suggest alternate
    - no_action       -> pass through
    - modified        -> internal routing only, not user-facing

Sequential routing (compound intents):
  The gate can emit pending_handoff when the user asks for two things
  in one turn (e.g. "find X and bookmark the cheapest"). After search
  finishes, handoff_to_organizer injects a HumanMessage containing the
  primary's results and the secondary intent, then routes to
  organizer_plan. Only search -> organizer is supported.

Tool-limit handling:
  When tool_call_count >= max_calls_per_turn and a tool_call is still
  pending on the last AIMessage, should_continue_* routes to
  patch_tool_calls. That node returns stub ToolMessages via the normal
  state-update mechanism. add_messages reducer commits reliably.

  tool_call_count is incremented BEFORE the ToolNode runs, based on the
  number of tool_calls on the last AIMessage. Accurate source of truth
  even when a ToolNode returns 0 messages.

Message sanitization:
  Every agent node (chat_react, search_node, report_node, organizer_plan)
  calls sanitize_messages on state["messages"] before building the LLM
  input. Orphaned tool_calls from crashes or partial writes never reach
  an LLM.

Guardrail checks (run after chat_react or report_react finishes):
  1. PII scrub     — remove SSNs, credit cards, phone numbers from output.
  2. Tool health   — all tools failed → honest fallback message.
  3. Empty         — retry with nudge (up to max_retries). Guardrail is
                     the SOLE owner of the empty_retries counter; the
                     retry node only injects the nudge message.
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
#
# tool_call_count is incremented based on the number of tool_calls on the
# preceding AIMessage — the authoritative source for "how many calls were
# just dispatched." We do NOT count ToolNode output messages, because a
# ToolNode may return non-1:1 output shapes in future versions and because
# errors swallowed into single ToolMessages would under-count.

_chat_tn = ToolNode(CHAT_AGENT_TOOLS)
_org_tn = ToolNode(ORGANIZER_TOOLS)
_search_tn = ToolNode(SEARCH_SUPERVISOR_TOOLS)
_report_tn = ToolNode(REPORT_GENERATOR_TOOLS)


def _pending_tool_call_count(state: AgentState) -> int:
    """Number of tool_calls on the last AIMessage, or 0."""
    messages = state.get("messages") or []
    if not messages:
        return 0
    last = messages[-1]
    tcs = getattr(last, "tool_calls", None) or []
    return len(tcs)


async def chat_tools(state: AgentState) -> dict[str, Any]:
    dispatched = _pending_tool_call_count(state)
    result = await _chat_tn.ainvoke(state)
    return {**result, "tool_call_count": state.get("tool_call_count", 0) + dispatched}


async def org_tools(state: AgentState) -> dict[str, Any]:
    """Run the organizer's ToolNode and attach a completion event.

    After the write tool executes (successfully or not), we look up the
    tool_call that triggered it and package a summary for chat_react to
    verbalize. organizer_event.kind is set to "completed" so chat_react
    knows to acknowledge the outcome in natural language — the user
    never sees raw JSON tool output.

    If the tool failed (ToolMessage contains '"success": false'), we
    still set kind="completed" but attach the failure detail; chat_react
    is instructed to acknowledge the failure gracefully without
    exposing internals.
    """
    dispatched = _pending_tool_call_count(state)

    # Pull the calling AIMessage BEFORE running tools — we need its
    # tool_calls to build the completion summary.
    source_tool_calls: list[dict] = []
    msgs_before = state.get("messages") or []
    if msgs_before:
        last = msgs_before[-1]
        tcs = getattr(last, "tool_calls", None) or []
        source_tool_calls = list(tcs)

    result = await _org_tn.ainvoke(state)
    tool_msgs = result.get("messages", []) or []

    # Match each tool_call to its corresponding ToolMessage (if any).
    # For organizer writes we typically have ONE tool per turn; but we
    # handle N defensively.
    completed_details = []
    for tc in source_tool_calls:
        tc_id = tc.get("id") if isinstance(tc, dict) else None
        tc_name = tc.get("name", "?")
        tc_args = tc.get("args", {}) or {}

        matching_result = None
        for tm in tool_msgs:
            if (
                isinstance(tm, ToolMessage)
                and getattr(tm, "tool_call_id", None) == tc_id
            ):
                matching_result = tm
                break

        content = (getattr(matching_result, "content", "") or "") if matching_result else ""
        success = True
        if isinstance(content, str) and (
            '"success": false' in content.lower()
            or '"success":false' in content.lower()
        ):
            success = False

        completed_details.append({
            "tool": tc_name,
            "args": tc_args,
            "success": success,
            "raw_result": content[:800] if isinstance(content, str) else "",
        })

    # Build organizer_event. For the typical single-tool case we pick
    # the first (and only) detail; we still keep the raw details list
    # for chat_react to see if it needs more context.
    if completed_details:
        primary = completed_details[0]
        summary = _summarize_completed_write(primary["tool"], primary["args"])
        organizer_event = {
            "kind": "completed",
            "tool": primary["tool"],
            "summary": summary,
            "success": primary["success"],
            "result": {"details": completed_details},
        }
    else:
        organizer_event = None

    out = {
        **result,
        "tool_call_count": state.get("tool_call_count", 0) + dispatched,
    }
    if organizer_event:
        out["organizer_event"] = organizer_event
    return out


def _summarize_completed_write(tool_name: str, args: dict) -> str:
    """Human-readable post-write summary for organizer_event.

    Mirrors the _build_summary style in organizer.py but in past tense.
    Kept here (not imported) to avoid a circular-import risk with
    organizer.py and to keep the user-facing phrasing in the file that
    owns user-facing transformations.
    """
    if tool_name == "manage_profile":
        parts = []
        if args.get("budget_min") or args.get("budget_max"):
            parts.append(
                f"budget ${args.get('budget_min', '?')}-${args.get('budget_max', '?')}"
            )
        if args.get("bedrooms_min"):
            parts.append(f"{args['bedrooms_min']}+ beds")
        if args.get("work_address"):
            parts.append(f"work at {args['work_address']}")
        return f"profile updated ({', '.join(parts)})" if parts else "profile updated"

    if tool_name == "manage_bookmarks":
        action = args.get("action", "add")
        lid = args.get("listing_id", "?")
        if action == "add":
            days = args.get("watch_days", 14)
            return f"bookmarked listing {lid} with a {days}-day watch"
        return f"removed bookmark for listing {lid}"

    if tool_name == "manage_destinations":
        lid = args.get("listing_id", "?")
        dest = args.get("dest_address", "?")
        mode = args.get("travel_mode", "transit")
        return f"saved a {mode} route from listing {lid} to {dest}"

    if tool_name == "flag_data":
        rid = args.get("listing_id") or args.get("signal_id", "?")
        return f"flagged URL for record {rid}"

    if tool_name == "update_pipeline_queries":
        tag = args.get("tag", "?")
        return f"set up tracking for '{tag}'"

    if tool_name == "manage_conversations":
        action = args.get("action", "?")
        return f"conversation {action} recorded"

    return f"completed {tool_name}"


async def search_tools(state: AgentState) -> dict[str, Any]:
    dispatched = _pending_tool_call_count(state)
    result = await _search_tn.ainvoke(state)
    return {**result, "tool_call_count": state.get("tool_call_count", 0) + dispatched}


async def report_tools(state: AgentState) -> dict[str, Any]:
    dispatched = _pending_tool_call_count(state)
    result = await _report_tn.ainvoke(state)
    return {**result, "tool_call_count": state.get("tool_call_count", 0) + dispatched}


# -- Input Gate --------------------------------------------------------

async def input_gate(state: AgentState) -> dict[str, Any]:
    """Classify user intent. Scrubs PII from input. Falls back to chat.

    Also resets per-turn counters. Continued turns on a checkpointed
    session inherit state from the previous turn — including tool_call_count
    and empty_retries. Without this reset, a heavy turn that used 12 tool
    calls would leave turn N+1 with only 3 remaining tool calls, and any
    turn that hit a retry would start N+1 with a depleted retry budget.
    These counters are per-turn semantics, not per-session, so we zero
    them on every entry to the gate.
    """
    log = logger.bind(trace_id=state.get("trace_id"), node="input_gate")
    cfg = _load_config()
    gate_cfg = cfg.get("input_gate", {})

    # Per-turn counter reset. This runs on BOTH new and continued turns.
    per_turn_reset = {
        "tool_call_count": 0,
        "empty_retries": 0,
        "sub_agent_result": None,
        "pending_handoff": None,
        "organizer_event": None,
    }

    if not gate_cfg.get("enabled", True):
        return {**per_turn_reset, "route": "chat"}

    if state.get("pending_confirmation"):
        log.info("gate_pending_confirmation")
        return {**per_turn_reset, "route": "confirm"}

    last_message = state["messages"][-1]
    user_text = last_message.content if hasattr(last_message, "content") else str(last_message)

    cleaned, pii_found = scrub_pii(user_text)
    if pii_found:
        log.info("input_pii_scrubbed", types=pii_found)

    max_len = gate_cfg.get("max_query_length", 5000)
    if len(cleaned) > max_len:
        log.warning("query_too_long", length=len(cleaned), max=max_len)
        return {**per_turn_reset, "route": "block", "is_valid": False, "error": "Query too long"}

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
        parsed = _parse_gate_json(response.content)
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

        # Parse optional handoff for compound intents.
        # Only search -> organizer is supported. Everything else collapses
        # to single-route (chat already runs last on search/organizer).
        handoff = None
        next_block = parsed.get("next") or {}
        if (
            isinstance(next_block, dict)
            and route == "search"
            and next_block.get("route") == "organizer"
            and (next_block.get("intent") or "").strip()
        ):
            # Also require signed-in — organizer auth-guards anonymous.
            if signed_in:
                handoff = {
                    "route": "organizer",
                    "intent": next_block["intent"].strip()[:500],
                }
                log.info(
                    "gate_compound_intent",
                    primary=route,
                    handoff_route=handoff["route"],
                    handoff_intent=handoff["intent"][:80],
                )
            else:
                log.info(
                    "gate_handoff_dropped_anonymous",
                    reason="organizer_requires_auth",
                )

        log.info("gate_classified", route=route, reason=reason[:100])
        return {
            **per_turn_reset,
            "route": route,
            "is_valid": route != "block",
            "pending_handoff": handoff,
        }

    except Exception as e:
        log.warning("gate_parse_failed", error=str(e)[:200])
        return {**per_turn_reset, "route": "chat"}


def _parse_gate_json(raw: str) -> dict:
    """Parse the gate's JSON response, tolerating markdown fences.

    The gate prompt explicitly forbids fences, but LLMs occasionally emit
    them anyway. Rather than silently degrading to chat-route on every
    fenced response, strip common fence patterns before json.loads.
    """
    text = (raw or "").strip()
    if not text:
        return {}

    # Strip ```json ... ``` or ``` ... ``` fences
    if text.startswith("```"):
        # Drop leading fence line
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        # Drop trailing fence
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    # Some models emit a prose preamble. Find the first {...} block.
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]

    return json.loads(text)


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

async def patch_tool_calls(state: AgentState) -> dict[str, Any]:
    """Inject stub ToolMessages for any pending tool_calls at the tail.

    Runs when tool_call_count hits the per-turn cap. A proper node, so
    the add_messages reducer commits the stubs reliably.
    """
    log = logger.bind(trace_id=state.get("trace_id"), node="patch_tool_calls")
    messages = state.get("messages") or []
    if not messages:
        return {}

    last = messages[-1]
    tool_calls = getattr(last, "tool_calls", None)
    if not tool_calls:
        return {}

    stubs = [
        ToolMessage(
            content=json.dumps({
                "success": False,
                "error": "Tool call skipped: maximum tool calls per turn reached.",
            }),
            tool_call_id=tc["id"],
            name=tc["name"],
        )
        for tc in tool_calls
    ]

    log.warning("tool_calls_patched", count=len(stubs),
                tools=[tc["name"] for tc in tool_calls])
    return {"messages": stubs}


# -- Guardrail ---------------------------------------------------------

async def guardrail(state: AgentState) -> dict[str, Any]:
    """PII -> tool health -> empty -> length.

    Sole owner of the empty_retries counter. When an empty response is
    detected and retries remain, increments empty_retries. When retries
    are exhausted, writes the exhausted marker message and stops. The
    retry node (chat_react_retry) does NOT touch the counter — it only
    injects the nudge SystemMessage.
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
        # Emit the exhausted marker AIMessage. route_after_guardrail
        # treats "retries exhausted" as a terminal state regardless of
        # content, so even if this message were empty we would still end.
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
    """Append the empty-response nudge as a SystemMessage and loop back to chat_react.

    Does NOT increment empty_retries — guardrail is the sole writer. This
    node is idempotent with respect to the counter, which means guardrail
    and this node can never double-increment.

    The nudge SystemMessage uses a stable id so add_messages reducer
    dedupes it across retries — the LLM sees at most one nudge in
    history, not a growing stack of identical reminders.
    """
    log = logger.bind(trace_id=state.get("trace_id"), node="chat_react_retry")
    log.info("retrying_with_nudge", attempt=state.get("empty_retries", 0))
    return {
        "messages": [SystemMessage(
            content=EMPTY_RETRY_NUDGE,
            id="vicinity-empty-retry-nudge",
        )],
    }


# -- Handoff dispatch (compound intent continuation) ------------------

async def handoff_to_organizer(state: AgentState) -> dict[str, Any]:
    """Sequential-routing bridge: search -> organizer.

    Runs only when the input gate set pending_handoff with route=organizer
    AND the primary sub-agent (search) has finished.

    Work performed:
      1. Read sub_agent_result from the primary agent.
      2. Inject a HumanMessage that gives the organizer everything it
         needs to act: the user's original request, the secondary intent
         the gate extracted, and the primary's result summary.
      3. Clear pending_handoff so the next turn starts clean.
      4. Return — the next graph edge routes to organizer_plan.

    Why a HumanMessage (not SystemMessage):
      organizer_plan's prompt instructs the LLM to read the user's
      request from the latest HumanMessage and act on it. A SystemMessage
      would not trigger the same tool-selection behavior. We synthesize
      a HumanMessage that expresses what the user asked for in the
      second half of their compound intent, enriched with the primary
      agent's findings so the organizer can resolve selectors like
      "the cheapest" against concrete listing_ids.
    """
    log = logger.bind(trace_id=state.get("trace_id"), node="handoff_to_organizer")
    handoff = state.get("pending_handoff") or {}
    intent = handoff.get("intent", "").strip()
    sub = state.get("sub_agent_result") or {}

    # Extract the primary agent's content. For search, this is the
    # ranked listing summary the Search Supervisor produced.
    primary_content = (sub.get("content") or "").strip()
    primary_agent = sub.get("agent", "search_supervisor")

    if not intent:
        log.warning("handoff_missing_intent")
        return {"pending_handoff": None}

    # Build the synthesized HumanMessage. The format is explicit and
    # structured so the organizer's LLM reliably parses it.
    parts = [
        f"[Continuing compound request — previous step: {primary_agent}]",
        "",
        f"Follow-up action requested: {intent}",
    ]
    if primary_content:
        # Truncate if the primary output is unreasonably large. 4000 chars
        # is enough for ~15 listing cards with metadata.
        if len(primary_content) > 4000:
            primary_content = primary_content[:4000] + "\n[...truncated...]"
        parts += [
            "",
            f"Results from {primary_agent} (use these to resolve any selectors "
            "like 'the cheapest', 'the first one', 'the safest'):",
            "",
            primary_content,
        ]
    else:
        parts += [
            "",
            f"The {primary_agent} step returned no content. If the follow-up "
            "action requires a specific record (listing_id, route_id, etc.) "
            "and you don't have one in this context, respond in plain text "
            "asking the user to specify instead of calling a tool.",
        ]

    handoff_msg = "\n".join(parts)

    log.info(
        "handoff_injected",
        intent=intent[:80],
        primary_agent=primary_agent,
        primary_content_chars=len(primary_content),
    )

    return {
        "messages": [HumanMessage(content=handoff_msg)],
        "pending_handoff": None,
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

    Priority (in order):
      1. tool_calls pending (no-confirm tools)    -> org_tools
      2. Otherwise (organizer_event was set, or
         edge case with neither)                  -> chat_react

    chat_react is the universal spokesperson: it reads organizer_event
    and produces the user-facing message for preview / clarification /
    auth_required / rejected / completed cases. If organizer_event is
    set to "preview", chat_react's output is followed by
    organizer_confirm (which triggers interrupt). See should_continue_chat.
    """
    # No-confirm tool path: organizer_plan emitted a tool_call AIMessage
    # directly. Route to org_tools to execute it. After org_tools,
    # chat_react will verbalize the completed event.
    messages = state.get("messages", [])
    if messages:
        last = messages[-1]
        if getattr(last, "tool_calls", None):
            return "org_tools"

    # All other paths (preview, clarification, auth_required) flow
    # through chat_react for user-facing verbalization.
    return "chat_react"


def route_after_confirm(state: AgentState) -> str:
    """After organizer_confirm, dispatch based on the user's decision.

    Decisions are expressed through three distinguishable state shapes
    set by organizer_confirm:

      approve:  last AIMessage has tool_calls   -> org_tools
      reject:   organizer_event.kind="rejected" -> chat_react
      modify:   sub_agent_result.status="modified" -> organizer_plan (re-plan)

    Order matters: modify is checked first because the modify path
    injects a HumanMessage AND sets sub_agent_result; if we let tool
    calls check fire first we'd misroute on a re-plan cycle. The
    modified sub_agent_result is cleared by organizer_confirm on
    subsequent approve/reject, so it cannot leak across decisions.
    """
    sub_result = state.get("sub_agent_result") or {}
    if sub_result.get("status") == "modified":
        return "organizer_plan"

    messages = state.get("messages", [])
    if messages:
        last = messages[-1]
        if getattr(last, "tool_calls", None):
            return "org_tools"

    # reject / defensive: chat_react will verbalize from organizer_event
    return "chat_react"


def _has_pending_tool_calls(state: AgentState) -> bool:
    messages = state.get("messages") or []
    if not messages:
        return False
    last = messages[-1]
    return bool(getattr(last, "tool_calls", None))


def should_continue_chat(state: AgentState) -> str:
    """Chat ReAct router.

    Priority:
      1. Tool limit hit AND pending tool_calls -> patch_tool_calls -> guardrail
      2. Tool limit hit, no pending calls      -> guardrail
      3. Tool calls pending                    -> chat_tools
      4. Organizer preview pending             -> organizer_confirm (interrupt)
      5. Otherwise                             -> guardrail

    The organizer_confirm hand-off (priority 4) is what makes chat_react
    the single-voice spokesperson for the HITL flow: chat_react first
    verbalizes the preview in natural language, THEN the graph pauses
    for the user's approve/reject/modify via organizer_confirm's
    interrupt(). chat_react's streamed tokens become the confirmation
    bubble; organizer_confirm's interrupt payload drives the UI's
    approve/reject buttons.
    """
    if state.get("tool_call_count", 0) >= _max_tool_calls():
        if _has_pending_tool_calls(state):
            logger.warning("chat_tool_limit_reached",
                           count=state.get("tool_call_count", 0))
            return "patch_tool_calls"
        return "guardrail"

    if _has_pending_tool_calls(state):
        return "chat_tools"

    # Organizer preview: chat_react just produced the natural-language
    # confirmation question. Hand off to organizer_confirm to interrupt.
    pending = state.get("pending_confirmation")
    event = state.get("organizer_event") or {}
    if pending and event.get("kind") == "preview":
        return "organizer_confirm"

    return "guardrail"


def should_continue_search(state: AgentState) -> str:
    """Search ReAct router.

    Priority:
      1. Tool limit hit AND pending tool_calls -> patch_tool_calls
      2. Tool limit hit, no pending calls      -> next-step dispatch
      3. Tool calls pending                    -> search_tools
      4. Otherwise                             -> next-step dispatch

    Next-step dispatch: if pending_handoff is set (compound intent
    with search -> organizer), route to handoff_to_organizer.
    Otherwise route to chat_react as the default synthesizer.
    """
    if state.get("tool_call_count", 0) >= _max_tool_calls():
        if _has_pending_tool_calls(state):
            logger.warning("search_tool_limit_reached",
                           count=state.get("tool_call_count", 0))
            return "patch_tool_calls"
        return _search_next_step(state)

    if _has_pending_tool_calls(state):
        return "search_tools"

    return _search_next_step(state)


def _search_next_step(state: AgentState) -> str:
    handoff = state.get("pending_handoff") or {}
    if handoff.get("route") == "organizer":
        return "handoff_to_organizer"
    return "chat_react"


def should_continue_report(state: AgentState) -> str:
    """Report ReAct router.

    Priority:
      1. Tool limit hit AND pending tool_calls -> patch_tool_calls -> guardrail
      2. Tool limit hit, no pending calls      -> guardrail
      3. Tool calls pending                    -> report_tools
      4. Otherwise                             -> guardrail

    Reports route DIRECTLY to guardrail when done — the Report
    Generator's final AIMessage IS the user-facing artifact. No
    Chat Agent re-synthesis (which would strip the template
    structure the report_generator prompt enforces).
    """
    if state.get("tool_call_count", 0) >= _max_tool_calls():
        if _has_pending_tool_calls(state):
            logger.warning("report_tool_limit_reached",
                           count=state.get("tool_call_count", 0))
            return "patch_tool_calls"
        return "guardrail"

    if _has_pending_tool_calls(state):
        return "report_tools"

    return "guardrail"


def route_after_guardrail(state: AgentState) -> str:
    """Route based on guardrail outcome: retry or END.

    Terminal when:
      - empty_retries has reached max_retries (regardless of content),
      - content is non-empty (normal path).

    Retries when:
      - 0 < empty_retries < max_retries AND last message content is empty.

    The retries==0 case is impossible to reach here because guardrail
    only returns without writing a message when the response was good.
    """
    cfg = _load_config()
    max_retries = cfg.get("guardrail", {}).get("max_retries", 2)
    retries = state.get("empty_retries", 0)

    # Exhausted: guardrail has already emitted the exhausted marker.
    # Terminate regardless of what that marker's content looks like.
    if retries >= max_retries:
        return END

    messages = state.get("messages", [])
    if not messages:
        return END

    last = messages[-1]
    content = last.content if hasattr(last, "content") else ""

    if not content.strip() and retries > 0:
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
    graph.add_node("handoff_to_organizer", handoff_to_organizer)
    graph.add_node("patch_tool_calls", patch_tool_calls)
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
        "patch_tool_calls": "patch_tool_calls",
        "guardrail": "guardrail",
        "organizer_confirm": "organizer_confirm",
    })
    graph.add_edge("chat_tools", "chat_react")

    # Tool-limit patch drains to guardrail
    graph.add_edge("patch_tool_calls", "guardrail")

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

    # Search flow — when done, either hand off to organizer (compound
    # intent) or fall through to chat_react for synthesis.
    graph.add_conditional_edges("search_react", should_continue_search, {
        "search_tools": "search_tools",
        "chat_react": "chat_react",
        "handoff_to_organizer": "handoff_to_organizer",
        "patch_tool_calls": "patch_tool_calls",
    })
    graph.add_edge("search_tools", "search_react")

    # Handoff bridge -> organizer. The organizer will plan against the
    # handoff HumanMessage just like any user request.
    graph.add_edge("handoff_to_organizer", "organizer_plan")

    # Report flow — terminal at guardrail (report IS the user-facing
    # output, no Chat Agent re-synthesis).
    graph.add_conditional_edges("report_react", should_continue_report, {
        "report_tools": "report_tools",
        "guardrail": "guardrail",
        "patch_tool_calls": "patch_tool_calls",
    })
    graph.add_edge("report_tools", "report_react")

    return graph.compile(checkpointer=checkpointer)