"""Live verification that sanitize_messages protects the LLM contract.

The OpenAI / DeepSeek message-list contract requires every AIMessage
with tool_calls to be followed by matching ToolMessages. When that
breaks — HITL rejection, crash mid-ReAct, partial checkpointer load —
the NEXT LLM call rejects the conversation.

sanitize_messages strips orphaned tool_calls on every agent node's
LLM input. These tests construct a poisoned message list and verify
the next turn runs without the LLM seeing the orphan.

If someone removes the sanitize_messages call from an agent node,
these tests fail, and the failure message points to which node is
missing the protection.
"""

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage


# Module-level: only integration (applies to all). asyncio is class-level
# on the async classes below — TestSanitizeRemovesOrphans has sync tests.
pytestmark = [pytest.mark.integration]


def _has_orphan_tool_calls(messages: list[BaseMessage]) -> bool:
    """True if ANY AIMessage with tool_calls lacks matching ToolMessages."""
    answered = {
        m.tool_call_id for m in messages
        if isinstance(m, ToolMessage) and getattr(m, "tool_call_id", None)
    }
    for m in messages:
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                tc_id = tc.get("id") if isinstance(tc, dict) else None
                if tc_id and tc_id not in answered:
                    return True
    return False


async def _invoke(graph, state_or_cmd, thread_id):
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}
    return await graph.ainvoke(state_or_cmd, config=config)


class TestSanitizeRemovesOrphans:
    """Direct unit-ish test of sanitize_messages with real message objects."""

    def test_orphan_ai_message_tool_calls_stripped(self):
        from app.agents.message_utils import sanitize_messages

        poisoned = [
            HumanMessage(content="Search something"),
            AIMessage(
                content="",
                tool_calls=[{"name": "search_and_filter", "args": {},
                             "id": "call_orphan", "type": "tool_call"}],
            ),
            # No ToolMessage for call_orphan - poisoned.
        ]
        cleaned = sanitize_messages(poisoned)
        assert not _has_orphan_tool_calls(cleaned)

    def test_matched_tool_calls_preserved(self):
        from app.agents.message_utils import sanitize_messages

        healthy = [
            HumanMessage(content="Search"),
            AIMessage(
                content="",
                tool_calls=[{"name": "search_and_filter", "args": {},
                             "id": "call_ok", "type": "tool_call"}],
            ),
            ToolMessage(content='{"results": []}', tool_call_id="call_ok"),
            AIMessage(content="Found nothing."),
        ]
        cleaned = sanitize_messages(healthy)
        assert len(cleaned) == 4  # nothing dropped
        assert not _has_orphan_tool_calls(cleaned)


@pytest.mark.asyncio
class TestChatAgentSurvivesPoisonedState:

    async def test_chat_agent_invoked_with_orphan_tool_calls_in_history(
        self,
        build_test_graph, fake_gate_llm, fake_agent_llm,
        real_reads, anon_context, thread_id,
    ):
        """Seed the checkpointer with a poisoned message list. Send a new
        message. The chat agent's LLM call must receive a sanitized list,
        not the raw orphan.
        """
        from app.agents.state import create_initial_state

        graph = build_test_graph()
        config = {"configurable": {"thread_id": thread_id}}

        # Seed state via the checkpointer: pretend an earlier turn left
        # an orphan tool_call in the history.
        poisoned = create_initial_state(anon_context, "earlier question")
        poisoned["messages"].append(AIMessage(
            content="",
            tool_calls=[{"name": "query_listings", "args": {"action": "search"},
                         "id": "call_poisoned", "type": "tool_call"}],
        ))
        # NO ToolMessage for call_poisoned — this is the orphan.

        # Run the graph to put it in a checkpointable state first,
        # then inject the orphan via update_state.
        fake_gate_llm.queue_route("chat", "seed")
        fake_agent_llm.queue_text("seeded.")
        await _invoke(
            graph,
            create_initial_state(anon_context, "seed"),
            thread_id,
        )
        await graph.aupdate_state(
            config,
            {"messages": [AIMessage(
                content="",
                tool_calls=[{"name": "query_listings",
                             "args": {"action": "search"},
                             "id": "call_poisoned", "type": "tool_call"}],
            )]},
        )

        # Now send a fresh question. chat_react should run, and the LLM
        # input it builds should NOT contain the orphan.
        fake_gate_llm.queue_route("chat", "follow-up")
        fake_agent_llm.queue_text("Here you go.")
        new_turn = {"messages": [HumanMessage(content="What about Allston?")]}
        await _invoke(graph, new_turn, thread_id)

        # Inspect what the chat agent LLM actually received on its call.
        # calls[-1] is (messages_in, response_out). If sanitize worked,
        # the messages_in has no orphan tool_calls.
        assert fake_agent_llm.calls, "chat agent was never called"
        msgs_sent_to_llm, _ = fake_agent_llm.calls[-1]
        assert not _has_orphan_tool_calls(msgs_sent_to_llm), (
            "chat_react sent orphan tool_calls to the LLM. "
            "sanitize_messages was either removed from chat_agent.py "
            "or stopped working. Check app/agents/chat_agent.py for "
            "the sanitize_messages call before the LLM ainvoke."
        )


@pytest.mark.asyncio
class TestSearchAgentSurvivesPoisonedState:

    async def test_search_node_sanitizes(
        self,
        build_test_graph, fake_gate_llm, fake_agent_llm,
        real_reads, anon_context, thread_id,
    ):
        """Same orphan-state scenario, but force routing to search so
        the search_supervisor node is the one that must sanitize."""
        from app.agents.state import create_initial_state

        graph = build_test_graph()
        config = {"configurable": {"thread_id": thread_id}}

        # Seed a valid state first
        fake_gate_llm.queue_route("chat", "seed")
        fake_agent_llm.queue_text("ok.")
        await _invoke(
            graph,
            create_initial_state(anon_context, "seed"),
            thread_id,
        )
        # Inject orphan
        await graph.aupdate_state(
            config,
            {"messages": [AIMessage(
                content="",
                tool_calls=[{"name": "search_and_filter", "args": {},
                             "id": "orphan2", "type": "tool_call"}],
            )]},
        )

        # Force route to search on the next turn
        fake_gate_llm.queue_route("search", "search")
        fake_agent_llm.queue_text("found listings")
        fake_agent_llm.queue_text("here are your listings")
        await _invoke(
            graph,
            {"messages": [HumanMessage(content="Find 2BR in Allston.")]},
            thread_id,
        )

        # First agent LLM call was search_node — verify its input was clean
        assert fake_agent_llm.calls, "search node never called LLM"
        first_call_msgs, _ = fake_agent_llm.calls[0]
        assert not _has_orphan_tool_calls(first_call_msgs), (
            "search_node sent orphan tool_calls to the LLM. "
            "sanitize_messages missing from app/agents/search_supervisor.py"
        )


@pytest.mark.asyncio
class TestOrganizerSanitizes:

    async def test_organizer_plan_sanitizes(
        self,
        build_test_graph, fake_gate_llm, fake_agent_llm,
        real_reads, write_sentinel, auth_context, thread_id,
    ):
        from app.agents.state import create_initial_state

        graph = build_test_graph()
        config = {"configurable": {"thread_id": thread_id}}

        # Seed valid state
        fake_gate_llm.queue_route("chat", "seed")
        fake_agent_llm.queue_text("ok.")
        await _invoke(
            graph,
            create_initial_state(auth_context, "seed"),
            thread_id,
        )
        # Inject orphan
        await graph.aupdate_state(
            config,
            {"messages": [AIMessage(
                content="",
                tool_calls=[{"name": "manage_bookmarks", "args": {},
                             "id": "orphan3", "type": "tool_call"}],
            )]},
        )

        # Route to organizer
        fake_gate_llm.queue_route("organizer", "bookmark")
        fake_agent_llm.queue_tool_call(
            "manage_bookmarks",
            {"action": "add", "listing_id": "lst-test-001",
             "user_id": auth_context["user_id"]},
        )
        await _invoke(
            graph,
            {"messages": [HumanMessage(content="Bookmark lst-test-001.")]},
            thread_id,
        )

        # First agent LLM call after seed was organizer_plan
        assert fake_agent_llm.calls
        # The last call is organizer's plan call
        msgs_sent, _ = fake_agent_llm.calls[-1]
        assert not _has_orphan_tool_calls(msgs_sent), (
            "organizer_plan sent orphan tool_calls to the LLM. "
            "sanitize_messages missing from app/agents/organizer.py"
        )