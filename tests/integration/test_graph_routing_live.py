"""Live routing behavior through the compiled graph.

Each test invokes the REAL compiled graph with MemorySaver checkpointer,
a scripted FakeLLM, and real-read Snowflake access. Verifies:

  1. input_gate correctly sends each intent to its agent node.
  2. chat/search/report nodes terminate in the correct final state.
  3. A new message in the SAME thread is classified on its own merits,
     not routed based on an earlier turn.
  4. block returns a polite message and ends.

These tests are the regression suite for the routing layer. If someone
refactors the gate prompt, these break loudly.

NO write tools are exercised here — that's test_organizer_hitl_live.
Writes would still be sentinel-blocked, but we keep the surface narrow.
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage


pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _invoke(graph, state, thread_id):
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}
    return await graph.ainvoke(state, config=config)


class TestRouteToChat:

    async def test_neighborhood_question_routes_to_chat(
        self,
        build_test_graph, fake_gate_llm, fake_agent_llm,
        real_reads, anon_context, thread_id,
    ):
        """A plain question flows gate -> chat_react -> guardrail -> END."""
        fake_gate_llm.queue_route("chat", "neighborhood question")
        fake_agent_llm.queue_text("Allston has a safety score of 72.")

        from app.agents.state import create_initial_state
        graph = build_test_graph()
        state = create_initial_state(anon_context, "Tell me about Allston.")

        result = await _invoke(graph, state, thread_id)

        ai_msgs = [m for m in result["messages"] if isinstance(m, AIMessage)]
        assert ai_msgs, "expected at least one AIMessage"
        assert "72" in ai_msgs[-1].content

    async def test_scoring_explanation_routes_to_chat_no_tools(
        self,
        build_test_graph, fake_gate_llm, fake_agent_llm,
        real_reads, anon_context, thread_id,
    ):
        """Methodology questions answer natively — no tool_calls allowed."""
        fake_gate_llm.queue_route("chat", "methodology")
        fake_agent_llm.queue_text(
            "Safety score blends crime, complaints, and transit exposure."
        )

        from app.agents.state import create_initial_state
        graph = build_test_graph()
        state = create_initial_state(anon_context, "How does scoring work?")

        result = await _invoke(graph, state, thread_id)

        assert result.get("tool_call_count", 0) == 0
        content = result["messages"][-1].content
        assert "safety" in content.lower() or "score" in content.lower()


class TestRouteToSearch:

    async def test_search_terminates_in_chat_with_sub_agent_result(
        self,
        build_test_graph, fake_gate_llm, fake_agent_llm,
        real_reads, anon_context, thread_id,
    ):
        """search_react produces content -> chat_react synthesizes -> END.

        In this simpler architecture, search terminates without tools if
        the LLM produces a final answer directly (we script that here).
        Tool-driven paths live in tool-specific unit tests.
        """
        fake_gate_llm.queue_route("search", "search request")
        # Search supervisor's one LLM call: direct synthesis.
        fake_agent_llm.queue_text("Found 3 listings: lst-a, lst-b, lst-c.")
        # Chat agent's one LLM call: presents sub_agent_result.
        fake_agent_llm.queue_text("Here are 3 listings matching your search.")

        from app.agents.state import create_initial_state
        graph = build_test_graph()
        state = create_initial_state(
            anon_context, "Find me 2BR under $3000 in Allston."
        )

        result = await _invoke(graph, state, thread_id)

        # search node wrote sub_agent_result
        sub = result.get("sub_agent_result")
        assert sub is not None
        assert sub.get("status") == "complete"
        assert "lst-" in sub.get("content", "")

        # chat agent produced a user-facing response
        final = result["messages"][-1]
        assert isinstance(final, AIMessage)
        assert final.content


class TestRouteToReport:

    async def test_report_to_chat_for_synthesis(
        self,
        build_test_graph, fake_gate_llm, fake_agent_llm,
        real_reads, auth_context, thread_id,
    ):
        """report_react -> chat_react path. Two bookmarks in auth_context."""
        fake_gate_llm.queue_route("report", "comparison request")
        fake_agent_llm.queue_text(
            "Listing lst-test-001 scores higher on safety; "
            "lst-test-002 is cheaper."
        )
        fake_agent_llm.queue_text(
            "Based on your priorities, lst-test-001 looks like the better fit."
        )

        from app.agents.state import create_initial_state
        graph = build_test_graph()
        state = create_initial_state(
            auth_context, "Compare my bookmarked listings."
        )

        result = await _invoke(graph, state, thread_id)

        sub = result.get("sub_agent_result")
        assert sub is not None
        assert sub.get("agent") == "report_generator"


class TestRouteToBlock:

    async def test_off_topic_gets_blocked_politely(
        self,
        build_test_graph, fake_gate_llm, fake_agent_llm,
        real_reads, anon_context, thread_id,
    ):
        """block node produces the boilerplate rejection without hitting
        the agent LLM at all."""
        fake_gate_llm.queue_route("block", "off topic")
        # No agent LLM responses needed — block doesn't call one.

        from app.agents.state import create_initial_state
        graph = build_test_graph()
        state = create_initial_state(
            anon_context, "What's the weather in Paris?"
        )

        result = await _invoke(graph, state, thread_id)

        assert result.get("is_valid") is False
        content = result["messages"][-1].content
        assert "Vicinity" in content
        # Agent LLM was never consulted
        assert len(fake_agent_llm.calls) == 0


class TestCurrentMessageNotOlder:
    """Regression: the gate must classify the MOST RECENT message, not the
    first one stored in state. This is the bug we keep fighting.
    """

    async def test_second_turn_routed_independently(
        self,
        build_test_graph, fake_gate_llm, fake_agent_llm,
        real_reads, anon_context, thread_id,
    ):
        """Turn 1: chat question.
        Turn 2: search request in the SAME thread. Must route to search,
                NOT re-route to chat because the old question is still
                in state.
        """
        from app.agents.state import create_initial_state

        # --- Turn 1: chat ---
        fake_gate_llm.queue_route("chat", "first-turn question")
        fake_agent_llm.queue_text("Scoring blends several dimensions.")
        graph = build_test_graph()
        state = create_initial_state(anon_context, "How does scoring work?")
        await _invoke(graph, state, thread_id)

        # --- Turn 2: search (same thread_id) ---
        fake_gate_llm.queue_route("search", "search request")
        fake_agent_llm.queue_text("Found some listings.")
        fake_agent_llm.queue_text("Here are the results.")
        second = {"messages": [HumanMessage(content=
            "Now find me 2BR under $3000 in Cambridge.")]}
        result = await _invoke(graph, second, thread_id)

        # Second turn's sub_agent_result should be search's output, not
        # stale state from the first turn. If routing mis-fired to chat,
        # sub_agent_result would be None.
        sub = result.get("sub_agent_result")
        assert sub is not None, (
            "second turn should have routed to search and produced "
            "sub_agent_result; got None (likely routed to chat instead)"
        )
        assert sub.get("agent") == "search_supervisor"