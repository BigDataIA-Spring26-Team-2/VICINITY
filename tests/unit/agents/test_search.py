"""Tests for app.agents.search_supervisor — context, tool routing."""

import pytest
from unittest.mock import patch
from langchain_core.messages import AIMessage

from app.agents.search_supervisor import search_node, _build_context
from tests.unit.agents.conftest import MockLLM, make_tool_call


class TestBuildContext:

    def test_includes_budget(self, sample_state):
        ctx = _build_context(sample_state())
        assert "$1500" in ctx
        assert "$3000" in ctx

    def test_includes_preferences(self, sample_state):
        ctx = _build_context(sample_state())
        assert "korean_food" in ctx

    def test_includes_work_location(self, sample_state):
        ctx = _build_context(sample_state())
        assert "77 Massachusetts Ave" in ctx
        assert "42.36" in ctx

    def test_includes_commute(self, sample_state):
        ctx = _build_context(sample_state())
        assert "30 min" in ctx

    def test_empty_context(self):
        state = {"messages": [], "user_context": {}}
        assert _build_context(state) == ""


class TestSearchNode:

    @pytest.mark.asyncio
    async def test_returns_tool_calls(self, sample_state, mock_config):
        tc = [make_tool_call("search_and_filter", {"min_price": 1500, "max_price": 3000})]
        mock = MockLLM(AIMessage(content="", tool_calls=tc))
        with patch("app.agents.search_supervisor.create_llm", return_value=mock):
            result = await search_node(sample_state("Find apartments"))
        assert result["messages"][0].tool_calls == tc
        assert "sub_agent_result" not in result

    @pytest.mark.asyncio
    async def test_sets_sub_agent_result_when_done(self, sample_state, mock_config):
        mock = MockLLM(AIMessage(content="Here are the top 3 listings ranked by safety."))
        with patch("app.agents.search_supervisor.create_llm", return_value=mock):
            result = await search_node(sample_state("Find apartments"))
        assert result["sub_agent_result"]["status"] == "complete"
        assert result["sub_agent_result"]["agent"] == "search_supervisor"
        assert "top 3" in result["sub_agent_result"]["content"]