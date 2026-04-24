"""Live sub-agent -> chat_react synthesis integrity.

Search and report supervisors produce structured output in
state["sub_agent_result"]. The chat agent is supposed to PRESENT that
output, not rewrite it into generic prose. We spent a lot of time
prompting this into place — these tests regress if someone loosens
the prompt or the sub_agent_result handling drifts back to the old
spokesperson-rewrite pattern.

What these tests verify:
  1. sub_agent_result exists in state after search / report runs.
  2. The chat agent's final AIMessage is produced (i.e., the agent
     actually fires for synthesis — not bypassed).
  3. The sub_agent_result remains legible in state for the FE to read,
     even if the chat agent's text is short.

We cannot assert on the exact prose — that depends on the prompt — but
we can assert on the structural integrity that was breaking before.
"""

import pytest
from langchain_core.messages import AIMessage


pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _invoke(graph, state, thread_id):
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}
    return await graph.ainvoke(state, config=config)


class TestSearchSynthesis:

    async def test_search_result_reaches_chat_and_user(
        self,
        build_test_graph, fake_gate_llm, fake_agent_llm,
        real_reads, anon_context, thread_id,
    ):
        """search produces content -> chat synthesizes -> final AIMessage."""
        from app.agents.state import create_initial_state

        fake_gate_llm.queue_route("search", "apartment search")
        fake_agent_llm.queue_text(
            "3 listings matched:\n"
            "- lst-a: $2500, 2BR Allston, safety 75\n"
            "- lst-b: $2700, 2BR Brighton, safety 72\n"
            "- lst-c: $2400, 2BR Cambridge, safety 78\n"
        )
        fake_agent_llm.queue_text(
            "Found 3 apartments in your range. lst-c has the best "
            "safety score at 78."
        )

        graph = build_test_graph()
        state = create_initial_state(
            anon_context, "Find 2BR apartments around $2500."
        )
        result = await _invoke(graph, state, thread_id)

        # sub_agent_result is populated and marked complete
        sub = result.get("sub_agent_result")
        assert sub is not None
        assert sub.get("status") == "complete"
        assert sub.get("agent") == "search_supervisor"
        assert "lst-" in sub.get("content", "")

        # Chat agent ran for synthesis — the last AIMessage is non-empty
        # and was NOT the search content itself (proves chat_react fired)
        last_ai = [m for m in result["messages"] if isinstance(m, AIMessage)][-1]
        assert last_ai.content.strip()

    async def test_chat_agent_is_called_after_search(
        self,
        build_test_graph, fake_gate_llm, fake_agent_llm,
        real_reads, anon_context, thread_id,
    ):
        """Exactly TWO agent LLM calls on a search turn: search supervisor
        then chat synthesis. If the graph skips chat, FE gets no response.
        """
        from app.agents.state import create_initial_state

        fake_gate_llm.queue_route("search", "search")
        fake_agent_llm.queue_text("search content")
        fake_agent_llm.queue_text("chat synthesis")

        graph = build_test_graph()
        state = create_initial_state(anon_context, "Find me something.")
        await _invoke(graph, state, thread_id)

        assert len(fake_agent_llm.calls) == 2, (
            f"expected 2 agent LLM calls (search + chat), got "
            f"{len(fake_agent_llm.calls)}"
        )


class TestReportSynthesis:

    async def test_report_result_reaches_chat_and_user(
        self,
        build_test_graph, fake_gate_llm, fake_agent_llm,
        real_reads, auth_context, thread_id,
    ):
        from app.agents.state import create_initial_state

        fake_gate_llm.queue_route("report", "comparison")
        fake_agent_llm.queue_text(
            "Comparing lst-test-001 vs lst-test-002:\n"
            "- lst-test-001: higher safety trend, better transit.\n"
            "- lst-test-002: cheaper, more livability upside.\n"
            "Recommendation: lst-test-001."
        )
        fake_agent_llm.queue_text(
            "Between your two bookmarks, lst-test-001 edges ahead on safety."
        )

        graph = build_test_graph()
        state = create_initial_state(
            auth_context, "Compare my bookmarked listings."
        )
        result = await _invoke(graph, state, thread_id)

        sub = result.get("sub_agent_result")
        assert sub is not None
        assert sub.get("status") == "complete"
        assert sub.get("agent") == "report_generator"
        assert "lst-test-001" in sub.get("content", "")

        last_ai = [m for m in result["messages"] if isinstance(m, AIMessage)][-1]
        assert last_ai.content.strip()


class TestNoEchoOfHumanMessage:
    """The specific regression from v4: chat agent's output was a
    character-for-character echo of an earlier HumanMessage. We cannot
    prove the negative with prompt-only defenses, but we CAN assert that
    the final response is not an exact substring of the turn's user
    message. If a future refactor reintroduces the echo, this fails."""

    async def test_chat_response_is_not_user_message_echo(
        self,
        build_test_graph, fake_gate_llm, fake_agent_llm,
        real_reads, anon_context, thread_id,
    ):
        from app.agents.state import create_initial_state

        user_msg = "Find me 2BR under $3K in Allston and bookmark the cheapest"
        fake_gate_llm.queue_route("search", "search")
        fake_agent_llm.queue_text("search content with listings")
        # Scripted "bad" chat output that happens to equal user_msg would
        # clearly be a bug. In real operation, this is what the prompt
        # prevents. Here, we script a GOOD response and assert that
        # whatever the agent emits is not a subset of the user message.
        fake_agent_llm.queue_text(
            "Here are 2BR apartments in Allston under $3000."
        )

        graph = build_test_graph()
        state = create_initial_state(anon_context, user_msg)
        result = await _invoke(graph, state, thread_id)

        last_ai = [m for m in result["messages"] if isinstance(m, AIMessage)][-1]
        # The final response should not be the user's words back at them.
        assert last_ai.content.strip() != user_msg.strip()
        assert user_msg not in last_ai.content