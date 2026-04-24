"""Shared state schema for the Vicinity agent graph.

This TypedDict is the single contract between all four agents
(Chat, Organizer, Search Supervisor, Report Generator) and the
graph routing logic. Every node reads from and writes to this state.

Design decisions:
  - messages uses add_messages reducer: LangGraph's built-in message
    accumulator that handles HumanMessage, AIMessage, ToolMessage
    deduplication and ordering.
  - sub_agent_result is a plain dict, NOT appended to messages.
    Sub-agent internal tool calls stay inside the sub-agent. Only
    the final result dict propagates to the Chat Agent for synthesis.
    This prevents context window blowup from parallel scoring results.
  - user_context is loaded once at session start (profile, bookmarks,
    summaries) and passed through unchanged. Agents read it but only
    the Organizer writes back to Snowflake.
  - pending_confirmation holds the Organizer's write preview payload.
    Written by organizer_plan (state commits on normal return).
    Read by organizer_confirm (calls interrupt() with this payload).
    Includes the original tool_calls list so the exact operation the
    user approved can be reconstructed after interrupt() resumes.
  - All config-driven limits (max tool calls, timeouts) are read from
    config/agents.yml at graph construction time, not stored in state.

Reroute / bounce architecture (added fields):

  reroute_count: int
    How many times the graph has returned to input_gate during a single
    user turn due to a sub-agent bounce or a chat_agent [ROUTE: ...]
    marker. Capped at MAX_REROUTES in graph.py; the third attempt
    forces route=chat as the universal fallback.

  reroute_history: list[RerouteRecord] (operator.add reducer)
    Append-only log of route attempts this turn. The input_gate reads
    this on re-entry to avoid re-picking the same failed route, and
    to understand WHY previous attempts failed so its next pick is
    informed, not just different.

  gate_reasoning: str
    The most recent input_gate's "reason" field. Propagated so every
    downstream agent can see why it was invoked — a sub-agent that
    knows the gate said "user is asking for a 2BR search" will bounce
    or proceed with more confidence than one that only sees the raw
    user message.

Sub-agent result status vocabulary (CANONICAL — keep in sync with
chat_agent system prompt's SUB-AGENT RESULT HANDLING section):

  complete       — Real content in .content. Chat synthesizes it.
  empty          — Tool ran but found nothing actionable.
  error          — Tool or LLM error; chat explains gracefully.
  bounce         — Wrong agent for this query. Graph routes back to
                   input_gate, which re-classifies with the history hint.
  auth_required  — User must sign in (Organizer only).
  rejected       — User rejected the Organizer proposal.
  modified       — User modified the Organizer proposal.
  no_action      — Agent had nothing to do (Organizer clarification
                   question with no tool calls).
"""

from __future__ import annotations

import operator
import uuid
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph.message import add_messages


class UserContext(TypedDict, total=False):
    """Pre-loaded user identity, profile, bookmarks, session history.

    Loaded from Snowflake before graph invocation.
    Passed through state unchanged — agents read, only Organizer writes to DB.

    USER IDENTITY INVARIANT:
        If user_id is present and non-empty, the user IS signed in.
        There is no separate "authenticated" flag. All four agents
        treat user_id truthiness as proof of authentication. This
        simplifies reasoning: anyone with user_id has full access;
        anyone without it is anonymous (read-only via Chat Agent).

        Any bookmarks, preferences, routes, or session_summaries
        that accompany a user_id belong to THAT SAME USER. A previous
        session loaded into context means the same human is continuing
        their journey across sessions — never cross-user contamination.
    """
    user_id: str
    session_id: str
    email: str
    display_name: str

    # From SEARCH_PROFILES (latest active)
    profile_id: str
    work_address: str
    work_lat: float
    work_lon: float
    budget_min: int
    budget_max: int
    bedrooms_min: int
    bedrooms_max: int
    max_commute_min: int
    preferences_text: str
    preference_tags: list[str]

    # From SESSION_SUMMARIES (N most recent)
    recent_summaries: list[dict]

    # From BOOKMARKED_LISTINGS (active bookmarks)
    active_bookmarks: list[dict]


class ToolCallRecord(TypedDict):
    """Single tool invocation record for the execution ledger."""
    tool_name: str
    args_summary: str
    latency_ms: int
    result_size: int
    success: bool


class ConfirmationPayload(TypedDict):
    """Write preview the Organizer presents before interrupt().

    Written by organizer_plan (committed to state on normal return).
    Read by organizer_confirm (calls interrupt with this as the value).

    tool_calls stores the original AIMessage.tool_calls so the exact
    operation can be reconstructed in an AIMessage after the user
    approves. This avoids re-invoking the LLM on resume.
    """
    tool: str
    summary: str
    params: dict
    tool_calls: list[dict]


class RerouteRecord(TypedDict, total=False):
    """One entry in reroute_history.

    Written each time the graph re-enters input_gate after a
    sub-agent bounce or a chat_agent [ROUTE: ...] marker. The gate
    reads history entries to avoid re-picking the same failed route
    and to understand why each attempt failed.
    """
    from_agent: str          # "search" | "report" | "organizer" | "chat"
    reason: str              # Human-readable why the reroute happened
    original_route: str      # The route that was attempted
    trigger: str             # "bounce" | "marker"
    gate_reasoning: str      # The gate's reason for the failed pick


class AgentState(TypedDict, total=False):
    """Shared state flowing through the entire Vicinity agent graph.

    Fields (existing):
        messages:             LangGraph message list with add_messages reducer.
        route:                Intent from input_gate: chat|organizer|search|report|confirm|block.
        user_context:         Pre-loaded user profile. Read-only during graph execution.
        sub_agent_result:     Compact dict from sub-agents for Chat Agent synthesis.
        pending_confirmation: Organizer write preview awaiting user approval.
        tool_call_count:      Running total of tool invocations this turn.
        tool_call_ledger:     Append-only log of every tool call (operator.add reducer).
        query_cost_usd:       Accumulated LLM token cost for this invocation.
        trace_id:             Unique ID for this graph invocation.
        is_valid:             False if input_gate blocked the query.
        empty_retries:        Guardrail empty-response retry counter.
        error:                Set on unrecoverable failure. Graph routes to END.

    Fields (reroute architecture):
        reroute_count:        Times the graph has re-entered input_gate this turn.
                              Capped at MAX_REROUTES in graph.py; third attempt
                              forces route=chat.
        reroute_history:      Append-only log of route attempts (RerouteRecord list).
                              Gate reads it on re-entry for informed reclassification.
        gate_reasoning:       Latest gate's reason, propagated so sub-agents know
                              WHY they were selected.
    """
    messages: Annotated[list, add_messages]
    route: str
    user_context: UserContext
    sub_agent_result: Optional[dict]
    pending_confirmation: Optional[ConfirmationPayload]
    tool_call_count: int
    tool_call_ledger: Annotated[list[ToolCallRecord], operator.add]
    query_cost_usd: float
    trace_id: str
    is_valid: bool
    empty_retries: int
    error: Optional[str]

    # Reroute-bounce architecture
    reroute_count: int
    reroute_history: Annotated[list[RerouteRecord], operator.add]
    gate_reasoning: str


def create_initial_state(
    user_context: UserContext,
    user_message: str,
) -> AgentState:
    """Build initial state for a new graph invocation.

    Called by MCP server / FastAPI endpoint before graph.ainvoke().
    Generates a fresh trace_id, initializes all counters to zero.
    """
    from langchain_core.messages import HumanMessage

    return {
        "messages": [HumanMessage(content=user_message)],
        "route": "",
        "user_context": user_context,
        "sub_agent_result": None,
        "pending_confirmation": None,
        "tool_call_count": 0,
        "tool_call_ledger": [],
        "query_cost_usd": 0.0,
        "trace_id": str(uuid.uuid4()),
        "is_valid": True,
        "empty_retries": 0,
        "error": None,
        # Reroute-bounce fields — start empty, only populated on
        # a sub-agent bounce or chat_agent [ROUTE: ...] marker.
        "reroute_count": 0,
        "reroute_history": [],
        "gate_reasoning": "",
    }