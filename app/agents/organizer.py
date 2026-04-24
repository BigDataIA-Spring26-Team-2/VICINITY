"""Organizer Agent — async, split into plan + confirm for HITL correctness.

Two nodes:
  organizer_plan    — LLM decides the write operation. Writes
                      pending_confirmation to state and returns normally
                      (state commits). No interrupt here.
  organizer_confirm — Reads pending_confirmation, calls interrupt()
                      with the payload. Cheap to rerun on resume.
                      Handles approve / reject / modify.

Three-way confirmation:
  approve  — "yes", "confirm", etc.  Reconstructs AIMessage with the
             original tool_calls → org_tools executes.
  reject   — "no", "cancel", etc.    Clears pending_confirmation → chat_react.
  modify   — anything else.          Clears pending_confirmation, injects
             user's text as a HumanMessage → organizer_plan re-plans.
             No ghost tool_call / ToolMessage pairs — the previous
             "Shall I proceed?" AIMessage had no tool_calls, so it stays
             in history harmlessly.

Hallucinated tool rejection:
  LLMs sometimes emit tool_calls with names not in their bindings
  (e.g. calling score_listing from the Organizer). If ANY tool_call
  name is outside ORGANIZER_TOOL_NAMES, we reject the whole response
  and ask the user to clarify. Prevents confirmation bubbles like
  "Execute score_listing" from ever reaching the user.

Graph wiring:
  input_gate → (route=organizer) → organizer_plan
    → (pending_confirmation set) → organizer_confirm → interrupt() → PAUSE
    → (no-confirm tools)         → org_tools → chat_react
    → (no tools)                 → chat_react

  Command(resume=...) → organizer_confirm
    → approved  → org_tools → chat_react → guardrail → END
    → rejected  → chat_react → guardrail → END
    → modified  → organizer_plan (re-plan) → ...
"""

from __future__ import annotations

from typing import Any

import structlog
import yaml
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.types import interrupt

from app.agents.llm import create_llm
from app.agents.message_utils import sanitize_messages
from app.agents.state import AgentState, ConfirmationPayload
from app.agents.tools.write_tools import ORGANIZER_TOOLS
from app.core.config_loader import CONFIG_DIR

logger = structlog.get_logger()


# Whitelist of tool names the Organizer is allowed to call.
# Derived at import time from ORGANIZER_TOOLS itself so we never drift.
ORGANIZER_TOOL_NAMES: set[str] = {t.name for t in ORGANIZER_TOOLS}


# -- Config (cached at module level) ----------------------------------

_system_prompt: str | None = None


def _get_system_prompt() -> str:
    global _system_prompt
    if _system_prompt is None:
        with open(CONFIG_DIR / "agents.yml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        _system_prompt = cfg.get("organizer", {}).get("system_prompt", "")
    return _system_prompt


# -- Helpers -----------------------------------------------------------

def _build_context(state: AgentState) -> str:
    """Build context string with user_id, profile, and bookmark count."""
    parts = []
    uc = state.get("user_context", {})
    if uc.get("user_id"):
        parts.append(f"user_id: {uc['user_id']}")
    if uc.get("session_id"):
        parts.append(f"session_id: {uc['session_id']}")
    if uc.get("display_name"):
        parts.append(f"User name: {uc['display_name']}")
    if uc.get("work_address"):
        parts.append(f"Work address: {uc['work_address']}")
    if uc.get("budget_min") or uc.get("budget_max"):
        parts.append(f"Budget: ${uc.get('budget_min', '?')}-${uc.get('budget_max', '?')}")
    if uc.get("active_bookmarks"):
        parts.append(f"Active bookmarks: {len(uc['active_bookmarks'])}")
    return "\n".join(parts) if parts else ""


def _is_no_confirm_tool(tool_calls: list[dict]) -> bool:
    """True if ALL tool calls are tools that skip confirmation."""
    no_confirm = {"manage_conversations", "update_pipeline_queries"}
    return bool(tool_calls) and all(
        tc.get("name") in no_confirm for tc in tool_calls
    )


def _unknown_tool_names(tool_calls: list[dict]) -> list[str]:
    """Return names of tool_calls NOT in the Organizer whitelist."""
    return [
        tc.get("name", "")
        for tc in tool_calls
        if tc.get("name") not in ORGANIZER_TOOL_NAMES
    ]


def _build_summary(tool_name: str, args: dict) -> str:
    """Human-readable one-liner from tool name and args."""
    if tool_name == "manage_profile":
        parts = []
        if args.get("budget_min") or args.get("budget_max"):
            parts.append(f"budget ${args.get('budget_min', '?')}-${args.get('budget_max', '?')}")
        if args.get("bedrooms_min"):
            parts.append(f"{args['bedrooms_min']}+ beds")
        if args.get("work_address"):
            parts.append(f"work at {args['work_address']}")
        return f"Update profile: {', '.join(parts)}" if parts else "Update profile"

    if tool_name == "manage_bookmarks":
        action = args.get("action", "add")
        lid = args.get("listing_id", "?")
        if action == "add":
            days = args.get("watch_days", 14)
            return f"Bookmark listing {lid} with {days}-day watch"
        return f"Remove bookmark for listing {lid}"

    if tool_name == "manage_destinations":
        lid = args.get("listing_id", "?")
        dest = args.get("dest_address", "?")
        mode = args.get("travel_mode", "transit")
        return f"Compute {mode} route from listing {lid} to {dest}"

    if tool_name == "flag_data":
        rid = args.get("listing_id") or args.get("signal_id", "?")
        return f"Flag URL for record {rid}"

    if tool_name == "update_pipeline_queries":
        tag = args.get("tag", "?")
        return f"Set up tracking for '{tag}'"

    # Unreachable because we whitelist upstream, but defensive.
    return f"Execute {tool_name}"


# -- Node 1: Plan (runs LLM, writes to structured state) --------------

async def organizer_plan(state: AgentState) -> dict[str, Any]:
    """LLM decides the write operation. No user-visible AIMessage.

    The organizer is an INTERNAL agent — the user must only ever see
    one coherent voice (chat_react). Instead of emitting AIMessages
    with conversational content, organizer_plan writes structured
    outcomes to state:

      - organizer_event          (for chat_react to verbalize)
      - pending_confirmation     (for organizer_confirm to interrupt on)

    Outcomes:
      0. No user_id              -> organizer_event.kind = auth_required
      1. Hallucinated tool names -> organizer_event.kind = clarification
      2. No tool calls           -> organizer_event.kind = clarification
                                    (LLM asked a clarification question)
      3. No-confirm tools        -> emit tool_call AIMessage, ToolNode
                                    runs, then chat_react summarizes
                                    via organizer_event.kind = completed
                                    (which org_tools post-wrapper sets)
      4. Write tool              -> build ConfirmationPayload + set
                                    organizer_event.kind = preview
    """
    log = logger.bind(trace_id=state.get("trace_id"), node="organizer_plan")

    # -- Outcome 0: authentication guard --
    uc = state.get("user_context", {})
    if not uc.get("user_id"):
        log.info("blocked_unauthenticated_write")
        return {
            "organizer_event": {
                "kind": "auth_required",
            },
            "sub_agent_result": {
                "status": "auth_required",
                "message": "User must authenticate before write operations.",
            },
        }

    # -- Build LLM input, sanitized --
    messages = [SystemMessage(content=_get_system_prompt())]

    context = _build_context(state)
    if context:
        messages.append(SystemMessage(content=f"USER CONTEXT:\n{context}"))

    messages.extend(sanitize_messages(state["messages"]))

    llm = create_llm(tools=ORGANIZER_TOOLS)
    response = await llm.ainvoke(messages)

    tool_calls = response.tool_calls or []

    # -- Outcome 1: hallucinated tool names --
    unknown = _unknown_tool_names(tool_calls)
    if unknown:
        log.warning("organizer_rejected_hallucinated_tools", unknown=unknown)
        return {
            "organizer_event": {
                "kind": "clarification",
                "message": (
                    "Could you tell me more specifically what you'd like me "
                    "to save? I can update a profile, bookmark a listing, "
                    "set up a commute route, flag a broken link, or track "
                    "a new interest."
                ),
            },
            "sub_agent_result": {
                "status": "no_action",
                "message": f"Rejected hallucinated tool names: {unknown}",
            },
        }

    # -- Outcome 2: no tool calls (LLM asked for clarification) --
    if not tool_calls:
        log.info("plan_no_tools")
        clarification_text = (response.content or "").strip() or (
            "Could you tell me a bit more about what you'd like me to do?"
        )
        return {
            "organizer_event": {
                "kind": "clarification",
                "message": clarification_text,
            },
            "sub_agent_result": {
                "status": "no_action",
                "message": clarification_text,
            },
        }

    # -- Outcome 3: no-confirm tools bypass confirmation --
    # These emit the tool_call AIMessage directly so ToolNode fires
    # on the next edge. No user-visible content. chat_react will
    # summarize after org_tools_with_event wraps the outcome.
    if _is_no_confirm_tool(tool_calls):
        log.info("plan_no_confirm_bypass", calls=len(tool_calls))
        return {"messages": [response]}

    # -- Outcome 4: write tool -> build confirmation payload + preview event --
    tc = tool_calls[0]
    summary = _build_summary(tc["name"], tc["args"])
    payload: ConfirmationPayload = {
        "tool": tc["name"],
        "summary": summary,
        "params": tc["args"],
        "tool_calls": tool_calls,
    }

    log.info("plan_confirmation_built", tool=tc["name"], summary=summary)

    return {
        "pending_confirmation": payload,
        "organizer_event": {
            "kind": "preview",
            "tool": tc["name"],
            "summary": summary,
            "params": tc["args"],
        },
    }


# -- Node 2: Confirm (interrupt, three-way: approve/reject/modify) ----

async def organizer_confirm(state: AgentState) -> dict[str, Any]:
    """Pause for user approval via interrupt(). Cheap to rerun on resume.

    Like organizer_plan, this node does NOT emit user-visible AIMessages.
    All outcomes are expressed through state:

      approve -> AIMessage with tool_calls (invisible content, only for
                 ToolNode to consume). Clears pending_confirmation AND
                 sub_agent_result so stale "modified" status from a
                 previous re-plan cycle cannot leak forward.

      reject  -> organizer_event.kind = "rejected" (chat_react verbalizes).
                 Clears pending_confirmation AND sub_agent_result.

      modify  -> User's modification injected as HumanMessage so
                 organizer_plan sees it as the new request.
                 sub_agent_result.status = "modified" flags re-plan
                 routing. pending_confirmation cleared (plan will
                 rebuild).
    """
    log = logger.bind(trace_id=state.get("trace_id"), node="organizer_confirm")

    pending = state.get("pending_confirmation")
    if not pending:
        # Defensive: we reached confirm without a pending payload.
        # Route back through chat_react via a clarification event.
        log.warning("confirm_no_pending")
        return {
            "organizer_event": {
                "kind": "clarification",
                "message": "I don't have anything pending to confirm right now.",
            },
            "sub_agent_result": None,
        }

    user_response = interrupt(pending)
    decision = _parse_response(user_response)

    if decision == "approve":
        log.info("confirm_approved", tool=pending["tool"])
        # Emit the tool_call AIMessage so ToolNode can fire. No content
        # (not user-visible). Clear both pending_confirmation (we're
        # done with the preview) and sub_agent_result (any prior
        # "modified" status must not leak into route_after_confirm).
        return {
            "messages": [AIMessage(content="", tool_calls=pending["tool_calls"])],
            "pending_confirmation": None,
            "sub_agent_result": None,
        }

    if decision == "reject":
        log.info("confirm_rejected", tool=pending["tool"])
        return {
            "organizer_event": {
                "kind": "rejected",
                "tool": pending["tool"],
                "summary": pending["summary"],
            },
            "pending_confirmation": None,
            "sub_agent_result": None,
        }

    # decision == "modify"
    # The user wants the operation tweaked. Inject their text as a new
    # HumanMessage so organizer_plan treats it as the latest request.
    # sub_agent_result.status = "modified" drives route_after_confirm
    # to go back to organizer_plan (see graph.py).
    log.info(
        "confirm_modified",
        tool=pending["tool"],
        modification=str(user_response)[:100],
    )
    return {
        "messages": [HumanMessage(content=str(user_response))],
        "pending_confirmation": None,
        "sub_agent_result": {"status": "modified", "tool": pending["tool"]},
    }


# -- Response parser --------------------------------------------------

_APPROVE = frozenset({
    "yes", "y", "confirm", "go ahead", "approved",
    "ok", "okay", "sure", "do it", "proceed",
    "yep", "yeah", "yes please", "go for it", "sounds good",
})

_REJECT = frozenset({
    "no", "n", "cancel", "nevermind", "never mind",
    "stop", "abort", "don't", "dont", "nope", "nah",
    "forget it", "skip", "no thanks",
})


def _parse_response(response: Any) -> str:
    """Parse user's interrupt response into approve / reject / modify."""
    if isinstance(response, bool):
        return "approve" if response else "reject"

    if isinstance(response, dict):
        if response.get("approved"):
            return "approve"
        if response.get("rejected"):
            return "reject"
        return "modify"

    if isinstance(response, str):
        lower = response.strip().lower()
        if lower in _APPROVE:
            return "approve"
        if not lower or lower in _REJECT:
            return "reject"
        return "modify"

    return "reject"