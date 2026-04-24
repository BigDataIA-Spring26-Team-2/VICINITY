"""Live HITL organizer flow — approve / reject / modify paths.

These are the most complex paths in the graph. Every write tool is
intercepted by the write_sentinel fixture — no row ever lands in
USER_DATA.* during these tests, but the graph behaves as if they did.

Covers:
  - organizer_plan -> interrupt with ConfirmationPayload.
  - Resume with "yes" -> org_tools executes -> chat_react synthesizes.
  - Resume with "no" -> chat_react acknowledges cancellation, NO write.
  - Resume with modification -> organizer_plan re-plans, NO orphan
    tool_calls poisoning state for the next turn.
  - manage_conversations / update_pipeline_queries skip confirmation
    (no-confirm tools).

Catches the "modified leak into approve" bug, the orphan-tool_calls
contract violation, and the triple-bubble case if someone reintroduces
a spokesperson rewrite.
"""

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command


pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _invoke(graph, state_or_cmd, thread_id):
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}
    return await graph.ainvoke(state_or_cmd, config=config)


class TestOrganizerApprove:

    async def test_bookmark_approve_flow(
        self,
        build_test_graph, fake_gate_llm, fake_agent_llm,
        real_reads, write_sentinel, auth_context, thread_id,
    ):
        """User says bookmark, gets preview, approves, write is recorded."""
        from app.agents.state import create_initial_state

        # --- Turn 1: plan + interrupt ---
        fake_gate_llm.queue_route("organizer", "bookmark request")
        fake_agent_llm.queue_tool_call(
            "manage_bookmarks",
            {"action": "add", "listing_id": "lst-test-001",
             "user_id": auth_context["user_id"], "watch_days": 14},
        )

        graph = build_test_graph()
        state = create_initial_state(
            auth_context, "Bookmark listing lst-test-001."
        )
        await _invoke(graph, state, thread_id)

        # Graph should have paused with pending_confirmation
        gs = await graph.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )
        assert gs.next, "expected graph to be paused at organizer_confirm"
        pending = gs.values.get("pending_confirmation")
        assert pending is not None
        assert pending["tool"] == "manage_bookmarks"

        # --- Turn 2: approve ---
        # Chat agent's post-write synthesis LLM call.
        fake_agent_llm.queue_text("Done — bookmarked.")
        result = await _invoke(graph, Command(resume="yes"), thread_id)

        # Write sentinel captured exactly one attempt
        assert len(write_sentinel.attempts) == 1
        attempt = write_sentinel.attempts[0]
        assert attempt.tool_name == "manage_bookmarks"
        assert attempt.args["listing_id"] == "lst-test-001"
        assert attempt.args.get("action") == "add"

        # pending_confirmation cleared after approve
        assert result.get("pending_confirmation") in (None,)

    async def test_no_write_until_approved(
        self,
        build_test_graph, fake_gate_llm, fake_agent_llm,
        real_reads, write_sentinel, auth_context, thread_id,
    ):
        """The preview turn alone must NEVER invoke a write tool."""
        from app.agents.state import create_initial_state

        fake_gate_llm.queue_route("organizer", "profile update")
        fake_agent_llm.queue_tool_call(
            "manage_profile",
            {"user_id": auth_context["user_id"], "budget_max": 3500},
        )

        graph = build_test_graph()
        state = create_initial_state(
            auth_context, "Update my budget max to 3500."
        )
        await _invoke(graph, state, thread_id)

        # Preview only — no write tool may have fired.
        assert len(write_sentinel.attempts) == 0, (
            "write tool invoked before user approval — "
            f"{write_sentinel.attempts}"
        )


class TestOrganizerReject:

    async def test_reject_does_not_write(
        self,
        build_test_graph, fake_gate_llm, fake_agent_llm,
        real_reads, write_sentinel, auth_context, thread_id,
    ):
        from app.agents.state import create_initial_state

        fake_gate_llm.queue_route("organizer", "bookmark")
        fake_agent_llm.queue_tool_call(
            "manage_bookmarks",
            {"action": "add", "listing_id": "lst-test-001",
             "user_id": auth_context["user_id"]},
        )

        graph = build_test_graph()
        state = create_initial_state(auth_context, "Bookmark lst-test-001.")
        await _invoke(graph, state, thread_id)

        # --- Reject ---
        # chat_react runs after rejection for acknowledgement.
        fake_agent_llm.queue_text("Cancelled. Anything else?")
        result = await _invoke(graph, Command(resume="no"), thread_id)

        assert len(write_sentinel.attempts) == 0
        # sub_agent_result should reflect rejection
        sub = result.get("sub_agent_result") or {}
        assert sub.get("status") == "rejected"


class TestOrganizerModify:
    """Regression suite for the modify flow — the single trickiest path
    in the whole graph. Historically it has leaked state or poisoned
    the message list with orphan tool_calls."""

    async def test_modify_reroutes_to_plan_no_write(
        self,
        build_test_graph, fake_gate_llm, fake_agent_llm,
        real_reads, write_sentinel, auth_context, thread_id,
    ):
        from app.agents.state import create_initial_state

        # --- Turn 1: plan (proposes 14 days) ---
        fake_gate_llm.queue_route("organizer", "bookmark")
        fake_agent_llm.queue_tool_call(
            "manage_bookmarks",
            {"action": "add", "listing_id": "lst-test-001",
             "user_id": auth_context["user_id"], "watch_days": 14},
        )

        graph = build_test_graph()
        state = create_initial_state(auth_context, "Bookmark lst-test-001.")
        await _invoke(graph, state, thread_id)

        # --- Turn 2: user modifies ("30 days") ---
        # After modification, the graph re-enters organizer_plan, which
        # produces a NEW proposal. Then it pauses at organizer_confirm
        # again.
        fake_agent_llm.queue_tool_call(
            "manage_bookmarks",
            {"action": "add", "listing_id": "lst-test-001",
             "user_id": auth_context["user_id"], "watch_days": 30},
        )
        await _invoke(graph, Command(resume="make it 30 days"), thread_id)

        # Still no write.
        assert len(write_sentinel.attempts) == 0

        # New pending_confirmation reflects the modification.
        gs = await graph.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )
        pending = gs.values.get("pending_confirmation")
        assert pending is not None
        assert pending["tool"] == "manage_bookmarks"
        assert pending["params"].get("watch_days") == 30

    async def test_approve_after_modify_does_not_leak_status(
        self,
        build_test_graph, fake_gate_llm, fake_agent_llm,
        real_reads, write_sentinel, auth_context, thread_id,
    ):
        """The bug: after modify, sub_agent_result had status="modified".
        On approve, routing consulted that stale status and misrouted
        back to organizer_plan instead of executing the write."""
        from app.agents.state import create_initial_state

        fake_gate_llm.queue_route("organizer", "bookmark")
        fake_agent_llm.queue_tool_call(
            "manage_bookmarks",
            {"action": "add", "listing_id": "lst-test-001",
             "user_id": auth_context["user_id"], "watch_days": 14},
        )
        graph = build_test_graph()
        state = create_initial_state(auth_context, "Bookmark lst-test-001.")
        await _invoke(graph, state, thread_id)

        # Modify
        fake_agent_llm.queue_tool_call(
            "manage_bookmarks",
            {"action": "add", "listing_id": "lst-test-001",
             "user_id": auth_context["user_id"], "watch_days": 30},
        )
        await _invoke(graph, Command(resume="30 days"), thread_id)

        # Approve the modified version
        fake_agent_llm.queue_text("Done.")
        await _invoke(graph, Command(resume="yes"), thread_id)

        # Write DID happen now, and with the modified watch_days.
        assert len(write_sentinel.attempts) == 1
        attempt = write_sentinel.attempts[0]
        assert attempt.args["watch_days"] == 30


class TestOrganizerMessageIntegrity:
    """After any HITL path, the graph's message list must be valid:
    every AIMessage with tool_calls has matching ToolMessages, or the
    tool_calls are stripped. Otherwise the NEXT LLM call will reject
    the conversation with the 'tool_calls must be followed by tool
    messages' error.
    """

    async def test_reject_leaves_no_orphan_tool_calls_in_state(
        self,
        build_test_graph, fake_gate_llm, fake_agent_llm,
        real_reads, write_sentinel, auth_context, thread_id,
    ):
        from app.agents.state import create_initial_state

        fake_gate_llm.queue_route("organizer", "bookmark")
        fake_agent_llm.queue_tool_call(
            "manage_bookmarks",
            {"action": "add", "listing_id": "lst-test-001",
             "user_id": auth_context["user_id"]},
        )
        graph = build_test_graph()
        state = create_initial_state(auth_context, "Bookmark lst-test-001.")
        await _invoke(graph, state, thread_id)

        fake_agent_llm.queue_text("Cancelled.")
        await _invoke(graph, Command(resume="no"), thread_id)

        # After rejection, walk the message list and verify every
        # AIMessage-with-tool_calls has a matching ToolMessage.
        gs = await graph.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )
        messages = gs.values.get("messages", [])

        declared = set()
        for m in messages:
            if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
                for tc in m.tool_calls:
                    declared.add(tc.get("id"))

        answered = {
            m.tool_call_id for m in messages
            if isinstance(m, ToolMessage) and getattr(m, "tool_call_id", None)
        }

        missing = declared - answered
        assert not missing, (
            f"Orphaned tool_calls after rejection: {missing}. "
            "The next LLM call will reject this conversation. "
            "sanitize_messages should cover it, but this is the signal "
            "that either the organizer injects bad state or sanitize was "
            "removed from an agent node."
        )


class TestNoConfirmTools:
    """manage_conversations and update_pipeline_queries skip the
    confirmation flow entirely."""

    async def test_update_pipeline_queries_bypasses_confirm(
        self,
        build_test_graph, fake_gate_llm, fake_agent_llm,
        real_reads, write_sentinel, auth_context, thread_id,
    ):
        from app.agents.state import create_initial_state

        fake_gate_llm.queue_route("organizer", "track new topic")
        fake_agent_llm.queue_tool_call(
            "update_pipeline_queries",
            {"tag": "pottery_classes",
             "reddit_queries": ["pottery classes Boston"]},
        )
        fake_agent_llm.queue_text("Tracking set up.")

        graph = build_test_graph()
        state = create_initial_state(
            auth_context, "Start tracking pottery classes."
        )
        result = await _invoke(graph, state, thread_id)

        # No-confirm tool went straight through — sentinel saw the call.
        assert len(write_sentinel.attempts) == 1
        assert write_sentinel.attempts[0].tool_name == "update_pipeline_queries"

        # Graph is NOT paused.
        gs = await graph.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )
        assert not gs.next, "update_pipeline_queries should not interrupt"