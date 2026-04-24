"""Organizer Agent -- async, split into plan + confirm for HITL correctness.

Two nodes:
  organizer_plan    -- LLM decides the write operation, writes
                      pending_confirmation to state, returns normally
                      (state commits). No interrupt here.
  organizer_confirm -- Reads pending_confirmation from state, calls
                      interrupt() with the payload. Cheap to rerun
                      on resume. Handles approve/reject/modify.

Three-way confirmation:
  approve  -- "yes", "confirm", "go ahead" etc.
              Reconstructs AIMessage with tool_calls, routes to org_tools.
  reject   -- "no", "cancel", "nevermind" etc.
              Clears pending_confirmation, routes to chat_react.
  modify   -- anything else ("change watch period to 60", "make it 3 beds")
              Clears pending_confirmation, injects user's text as HumanMessage,
              routes BACK to organizer_plan so the LLM re-plans with the
              modification. No wasted LLM calls on the confirm node.

Bounce mechanism (added):
  If the input_gate misroutes a read/explanation to organizer (e.g.
  "why would I bookmark this?" classified as organizer because
  "bookmark" appeared), the LLM emits "BOUNCE: <reason>" per its
  system prompt instructions. organizer_plan detects this and sets
  sub_agent_result.status="bounce" so the graph reroutes to input_gate
  for reclassification. Consistent with search/report bounce pattern.

Graph wiring:
  input_gate -> (route=organizer) -> organizer_plan
    -> (status=bounce)             -> record_bounce -> input_gate
    -> (pending_confirmation set)  -> organizer_confirm -> interrupt() -> PAUSE
    -> (no-confirm tool_calls)     -> org_tools -> chat_react
    -> (no tools)                  -> chat_react

  Command(resume=...) -> organizer_confirm resumes
    -> (approved)  -> org_tools -> chat_react -> guardrail -> END
    -> (rejected)  -> chat_react -> guardrail -> END
    -> (modified)  -> organizer_plan (re-plan with new instruction) -> ...
"""

from __future__ import annotations

from typing import Any

import structlog
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import interrupt
from app.agents.llm import create_llm
from app.agents.state import AgentState, ConfirmationPayload
from app.agents.tools.write_tools import ORGANIZER_TOOLS
from app.core.config_loader import CONFIG_DIR

logger = structlog.get_logger()

# -- Config (cached at module level) ----------------------------------

_system_prompt: str | None = None


def _get_system_prompt() -> str:
    global _system_prompt
    if _system_prompt is None:
        import yaml
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

    gr = state.get("gate_reasoning")
    if gr:
        parts.append(f"\nGate reasoning (why you were invoked): {gr}")

    return "\n".join(parts) if parts else ""


def _is_no_confirm_tool(tool_calls: list[dict]) -> bool:
    """True if ALL tool calls are tools that skip confirmation.

    manage_conversations and update_pipeline_queries execute immediately
    without user approval.
    """
    no_confirm = {"manage_conversations", "update_pipeline_queries"}
    return bool(tool_calls) and all(
        tc.get("name") in no_confirm for tc in tool_calls
    )


def _is_bounce_response(content: str) -> tuple[bool, str]:
    """True if the LLM output begins with 'BOUNCE:' (per system prompt).

    Same pattern as search_supervisor and report_generator for
    consistency across all three sub-agents.
    """
    if not isinstance(content, str):
        return False, ""
    stripped = content.lstrip()
    if stripped.upper().startswith("BOUNCE:"):
        reason = stripped[7:].strip()
        return True, reason
    return False, ""


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

    return f"Execute {tool_name}"


# -- Node 1: Plan (runs LLM, commits pending_confirmation) -----------

async def organizer_plan(state: AgentState) -> dict[str, Any]:
    """LLM decides the write operation. No interrupt here.

    Six outcomes:
      0. No user_id -> authentication required, return signup prompt.
      1. LLM returns BOUNCE: -> misrouted here, bounce to input_gate.
      2. No tool calls -> LLM wants clarification, return message.
      3. No-confirm tools (manage_conversations, update_pipeline_queries)
         -> execute immediately (return with tool_calls).
      4. Any other write tool -> build ConfirmationPayload, write to state,
         return user-facing preview message WITHOUT tool_calls.
      5. Re-plan after modification -> same as above but with user's
         modification already in the message history.
    """
    log = logger.bind(trace_id=state.get("trace_id"), node="organizer_plan")

    # -- Outcome 0: authentication guard --
    uc = state.get("user_context", {})
    if not uc.get("user_id"):
        log.info("blocked_unauthenticated_write")
        return {
            "messages": [AIMessage(content=(
                "Sign in at the top right and I can save that for you."
            ))],
            "sub_agent_result": {
                "status": "auth_required",
                "message": "User must authenticate before write operations.",
            },
        }

    # -- LLM invocation --
    system_prompt = _get_system_prompt()
    context = _build_context(state)

    messages = []
    messages.append(SystemMessage(content=system_prompt))
    if context:
        messages.append(SystemMessage(content=f"USER CONTEXT:\n{context}"))
    messages.extend(state["messages"])

    llm = create_llm(tools=ORGANIZER_TOOLS)
    response = await llm.ainvoke(messages)

    # -- Outcome 1: bounce (misrouted read / explanation) --
    # Check BEFORE tool_calls so the LLM can cleanly bounce even if
    # it accidentally generates tool_calls alongside BOUNCE text.
    if not response.tool_calls:
        is_bounce, bounce_reason = _is_bounce_response(response.content)
        if is_bounce:
            log.info("organizer_bounced", reason=bounce_reason[:200])
            return {
                "sub_agent_result": {
                    "status": "bounce",
                    "agent": "organizer",
                    "reason": bounce_reason,
                    "content": "",
                },
            }

    # Outcome 2: no tool calls (clarification)
    if not response.tool_calls:
        log.info("plan_no_tools")
        return {
            "messages": [response],
            "sub_agent_result": {"status": "no_action", "message": response.content},
        }

    # Outcome 3: no-confirm tools bypass confirmation
    if _is_no_confirm_tool(response.tool_calls):
        log.info("plan_no_confirm_bypass", calls=len(response.tool_calls))
        return {"messages": [response]}

    # Outcome 4: write tool -- build confirmation, commit to state
    tc = response.tool_calls[0]
    payload: ConfirmationPayload = {
        "tool": tc["name"],
        "summary": _build_summary(tc["name"], tc["args"]),
        "params": tc["args"],
        "tool_calls": response.tool_calls,
    }

    log.info("plan_confirmation_built", tool=tc["name"], summary=payload["summary"])

    return {
        "messages": [AIMessage(
            content=f"I'd like to: **{payload['summary']}**. Shall I proceed?"
        )],
        "pending_confirmation": payload,
    }


# -- Node 2: Confirm (interrupt, three-way: approve/reject/modify) ----

async def organizer_confirm(state: AgentState) -> dict[str, Any]:
    """Pause for user approval via interrupt(). Cheap to rerun.

    Three outcomes:
      - Approved: reconstruct AIMessage with original tool_calls -> org_tools.
      - Rejected: clear pending_confirmation -> chat_react.
      - Modified: clear pending_confirmation, inject user's text as
        HumanMessage -> organizer_plan re-plans with the modification.
    """
    log = logger.bind(trace_id=state.get("trace_id"), node="organizer_confirm")

    pending = state.get("pending_confirmation")
    if not pending:
        log.warning("confirm_no_pending")
        return {
            "messages": [AIMessage(content="Nothing pending to confirm.")],
        }

    user_response = interrupt(pending)

    decision = _parse_response(user_response)

    if decision == "approve":
        log.info("confirm_approved", tool=pending["tool"])
        ai_msg = AIMessage(content="", tool_calls=pending["tool_calls"])
        return {
            "messages": [ai_msg],
            "pending_confirmation": None,
        }

    if decision == "reject":
        log.info("confirm_rejected", tool=pending["tool"])
        return {
            "messages": [AIMessage(content="Got it, I've cancelled that.")],
            "pending_confirmation": None,
            "sub_agent_result": {"status": "rejected", "tool": pending["tool"]},
        }

    # decision == "modify"
    cancelled_tool_msgs = []
    original_tc = pending.get("tool_calls", [])
    if original_tc:
        original_ai = AIMessage(content="", tool_calls=original_tc)
        cancelled_tool_msgs.append(original_ai)
        for tc in original_tc:
            cancelled_tool_msgs.append(ToolMessage(
                content=f"Cancelled: user requested modification.",
                tool_call_id=tc["id"],
            ))

    log.info("confirm_modified", tool=pending["tool"],
             modification=str(user_response)[:100])
    return {
        "messages": [
            *cancelled_tool_msgs,
            AIMessage(content=(
                f"Understood, let me adjust. You said: \"{user_response}\""
            )),
            HumanMessage(content=str(user_response)),
        ],
        "pending_confirmation": None,
        "sub_agent_result": {"status": "modified", "tool": pending["tool"]},
    }


def _parse_response(response: Any) -> str:
    """Parse the user's interrupt response into approve/reject/modify.

    Returns:
        "approve"  -- explicit approval
        "reject"   -- explicit rejection
        "modify"   -- anything else (modification request)
    """
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

        if lower in (
            "yes", "y", "confirm", "go ahead", "approved",
            "ok", "sure", "do it", "proceed", "yep", "yeah",
            "yes please", "go for it", "sounds good",
        ):
            return "approve"

        if not lower or lower in (
            "no", "n", "cancel", "nevermind", "never mind",
            "stop", "abort", "don't", "nope", "nah",
            "forget it", "skip", "no thanks",
        ):
            return "reject"

        return "modify"

    return "reject"