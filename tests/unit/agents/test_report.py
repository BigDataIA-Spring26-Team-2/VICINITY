"""Tests for app.agents.report_generator — context, tool routing."""

import pytest
from unittest.mock import patch
from langchain_core.messages import AIMessage

from app.agents.report_generator import report_node, _build_context
from tests.unit.agents.conftest import MockLLM, make_tool_call


class TestBuildContext:

    def test_includes_user_id(self, sample_state):
        ctx = _build_context(sample_state())
        assert "u-test-001" in ctx

    def test_includes_priorities(self, sample_state):
        ctx = _build_context(sample_state())
        assert "korean_food" in ctx
        assert "safety" in ctx

    def test_includes_preferences_text(self, sample_state):
        ctx = _build_context(sample_state())
        assert "Korean food" in ctx

    def test_includes_bookmarks(self, sample_state):
        ctx = _build_context(sample_state())
        assert "lst-abc" in ctx
        assert "Allston" in ctx
        assert "2200" in ctx

    def test_empty_context(self):
        state = {"messages": [], "user_context": {}}
        assert _build_context(state) == ""


class TestReportNode:

    @pytest.mark.asyncio
    async def test_returns_tool_calls(self, sample_state, mock_config):
        tc = [make_tool_call("compile_evidence", {"user_id": "u1", "days": 14})]
        mock = MockLLM(AIMessage(content="", tool_calls=tc))
        with patch("app.agents.report_generator.create_llm", return_value=mock):
            result = await report_node(sample_state("Give me a comparison report"))
        assert result["messages"][0].tool_calls == tc
        assert "sub_agent_result" not in result

    @pytest.mark.asyncio
    async def test_sets_sub_agent_result_when_done(self, sample_state, mock_config):
        mock = MockLLM(AIMessage(content="Recommendation: Listing abc in Allston is the best match."))
        with patch("app.agents.report_generator.create_llm", return_value=mock):
            result = await report_node(sample_state("Compare my bookmarks"))
        assert result["sub_agent_result"]["status"] == "complete"
        assert result["sub_agent_result"]["agent"] == "report_generator"
        assert "abc" in result["sub_agent_result"]["content"]