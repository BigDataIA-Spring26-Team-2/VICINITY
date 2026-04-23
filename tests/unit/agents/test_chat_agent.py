"""Tests for app.agents.chat_agent — context building, LLM invocation."""

import pytest
from unittest.mock import patch
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agents.chat_agent import chat_react, _build_context
from tests.unit.agents.conftest import MockLLM, make_tool_call


class TestBuildContext:

    def test_includes_budget(self, sample_state):
        state = sample_state()
        ctx = _build_context(state)
        assert "$1500" in ctx
        assert "$3000" in ctx

    def test_includes_bedrooms(self, sample_state):
        state = sample_state()
        ctx = _build_context(state)
        assert "2+" in ctx

    def test_includes_work_address(self, sample_state):
        state = sample_state()
        ctx = _build_context(state)
        assert "77 Massachusetts Ave" in ctx

    def test_includes_preferences(self, sample_state):
        state = sample_state()
        ctx = _build_context(state)
        assert "korean_food" in ctx
        assert "gym" in ctx

    def test_includes_bookmarks(self, sample_state):
        state = sample_state()
        ctx = _build_context(state)
        assert "lst-abc" in ctx
        assert "lst-def" in ctx

    def test_includes_session_summary(self, sample_state):
        state = sample_state()
        ctx = _build_context(state)
        assert "Allston" in ctx

    def test_includes_sub_agent_result(self, sample_state):
        state = sample_state(sub_agent_result={"status": "complete", "data": [1, 2, 3]})
        ctx = _build_context(state)
        assert "sub_agent_result" in ctx.lower() or "complete" in ctx

    def test_empty_context(self):
        state = {"messages": [HumanMessage(content="Hi")], "user_context": {}}
        ctx = _build_context(state)
        assert ctx == ""

    def test_display_name_shown(self, sample_state):
        state = sample_state()
        ctx = _build_context(state)
        assert "Test User" in ctx


class TestChatReact:

    @pytest.mark.asyncio
    async def test_returns_content_response(self, sample_state, mock_config):
        mock = MockLLM(AIMessage(content="Here are your listings."))
        with patch("app.agents.chat_agent.create_llm", return_value=mock):
            result = await chat_react(sample_state())
        assert len(result["messages"]) == 1
        assert result["messages"][0].content == "Here are your listings."

    @pytest.mark.asyncio
    async def test_returns_tool_calls(self, sample_state, mock_config):
        tc = [make_tool_call("query_listings", {"action": "detail", "listing_id": "abc"})]
        mock = MockLLM(AIMessage(content="", tool_calls=tc))
        with patch("app.agents.chat_agent.create_llm", return_value=mock):
            result = await chat_react(sample_state())
        assert result["messages"][0].tool_calls == tc

    @pytest.mark.asyncio
    async def test_passes_system_prompt(self, sample_state, mock_config):
        mock = MockLLM(AIMessage(content="ok"))
        with patch("app.agents.chat_agent.create_llm", return_value=mock):
            await chat_react(sample_state())
        # First message should be the system prompt
        first_msg = mock.last_messages[0]
        assert isinstance(first_msg, SystemMessage)
        assert "test chat agent" in first_msg.content.lower()

    @pytest.mark.asyncio
    async def test_passes_user_context(self, sample_state, mock_config):
        mock = MockLLM(AIMessage(content="ok"))
        with patch("app.agents.chat_agent.create_llm", return_value=mock):
            await chat_react(sample_state())
        # Second message should be context
        messages = mock.last_messages
        context_msgs = [m for m in messages if isinstance(m, SystemMessage) and "USER CONTEXT" in m.content]
        assert len(context_msgs) == 1
        assert "$1500" in context_msgs[0].content