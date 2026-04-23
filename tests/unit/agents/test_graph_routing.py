"""Tests for graph routing functions and guardrail node."""

import pytest
from unittest.mock import patch
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.graph import (
    route_after_gate,
    route_after_plan,
    route_after_confirm,
    _should_continue,
    should_continue_chat,
    should_continue_search,
    should_continue_report,
    route_after_guardrail,
    block_node,
    guardrail,
)
from langgraph.graph import END
from tests.unit.agents.conftest import make_tool_call


# -- route_after_gate --------------------------------------------------

class TestRouteAfterGate:

    def test_chat(self):
        assert route_after_gate({"route": "chat"}) == "chat_react"

    def test_organizer(self):
        assert route_after_gate({"route": "organizer"}) == "organizer_plan"

    def test_confirm(self):
        assert route_after_gate({"route": "confirm"}) == "organizer_confirm"

    def test_search(self):
        assert route_after_gate({"route": "search"}) == "search_react"

    def test_report(self):
        assert route_after_gate({"route": "report"}) == "report_react"

    def test_block(self):
        assert route_after_gate({"route": "block"}) == "block"

    def test_unknown_defaults_to_chat(self):
        assert route_after_gate({"route": "xyz"}) == "chat_react"

    def test_empty_defaults_to_chat(self):
        assert route_after_gate({}) == "chat_react"


# -- route_after_plan --------------------------------------------------

class TestRouteAfterPlan:

    def test_pending_routes_to_confirm(self):
        state = {"pending_confirmation": {"tool": "x", "summary": "y", "params": {}, "tool_calls": []}}
        assert route_after_plan(state) == "organizer_confirm"

    def test_tool_calls_route_to_org_tools(self):
        tc = [make_tool_call("manage_conversations")]
        state = {"pending_confirmation": None, "messages": [AIMessage(content="", tool_calls=tc)]}
        assert route_after_plan(state) == "org_tools"

    def test_no_tools_routes_to_chat(self):
        state = {"pending_confirmation": None, "messages": [AIMessage(content="Clarification")]}
        assert route_after_plan(state) == "chat_react"

    def test_empty_messages_routes_to_chat(self):
        state = {"pending_confirmation": None, "messages": []}
        assert route_after_plan(state) == "chat_react"


# -- route_after_confirm -----------------------------------------------

class TestRouteAfterConfirm:

    def test_approved_with_tool_calls(self):
        tc = [make_tool_call("manage_bookmarks")]
        state = {"messages": [AIMessage(content="", tool_calls=tc)]}
        assert route_after_confirm(state) == "org_tools"

    def test_rejected_no_tools(self):
        state = {"messages": [AIMessage(content="Cancelled.")]}
        assert route_after_confirm(state) == "chat_react"

    def test_empty_messages(self):
        assert route_after_confirm({"messages": []}) == "chat_react"


# -- _should_continue --------------------------------------------------

class TestShouldContinue:

    @patch("app.agents.graph._max_tool_calls", return_value=15)
    def test_under_limit_with_tools(self, _):
        tc = [make_tool_call("query_listings")]
        state = {"tool_call_count": 3, "messages": [AIMessage(content="", tool_calls=tc)]}
        assert _should_continue(state, "tools", "done") == "tools"

    @patch("app.agents.graph._max_tool_calls", return_value=15)
    def test_at_limit_stops(self, _):
        tc = [make_tool_call("query_listings")]
        state = {"tool_call_count": 15, "messages": [AIMessage(content="", tool_calls=tc)]}
        assert _should_continue(state, "tools", "done") == "done"

    @patch("app.agents.graph._max_tool_calls", return_value=15)
    def test_over_limit_stops(self, _):
        state = {"tool_call_count": 20, "messages": [AIMessage(content="", tool_calls=[make_tool_call("x")])]}
        assert _should_continue(state, "tools", "done") == "done"

    @patch("app.agents.graph._max_tool_calls", return_value=15)
    def test_no_tools_goes_to_done(self, _):
        state = {"tool_call_count": 0, "messages": [AIMessage(content="Done")]}
        assert _should_continue(state, "tools", "done") == "done"

    @patch("app.agents.graph._max_tool_calls", return_value=15)
    def test_empty_messages_goes_to_done(self, _):
        state = {"tool_call_count": 0, "messages": []}
        assert _should_continue(state, "tools", "done") == "done"


class TestShouldContinueVariants:

    @patch("app.agents.graph._max_tool_calls", return_value=15)
    def test_chat_done_node(self, _):
        state = {"tool_call_count": 0, "messages": [AIMessage(content="done")]}
        assert should_continue_chat(state) == "guardrail"

    @patch("app.agents.graph._max_tool_calls", return_value=15)
    def test_search_done_node(self, _):
        state = {"tool_call_count": 0, "messages": [AIMessage(content="done")]}
        assert should_continue_search(state) == "chat_react"

    @patch("app.agents.graph._max_tool_calls", return_value=15)
    def test_report_done_node(self, _):
        state = {"tool_call_count": 0, "messages": [AIMessage(content="done")]}
        assert should_continue_report(state) == "chat_react"

    @patch("app.agents.graph._max_tool_calls", return_value=15)
    def test_chat_limit_goes_to_guardrail(self, _):
        tc = [make_tool_call("query_safety")]
        state = {"tool_call_count": 15, "messages": [AIMessage(content="", tool_calls=tc)]}
        assert should_continue_chat(state) == "guardrail"


# -- route_after_guardrail ---------------------------------------------

class TestRouteAfterGuardrail:

    def test_content_present_goes_to_end(self, mock_config):
        state = {"messages": [AIMessage(content="Answer.")], "empty_retries": 0}
        assert route_after_guardrail(state) == END

    def test_empty_with_retries_routes_to_retry(self, mock_config):
        state = {"messages": [AIMessage(content="")], "empty_retries": 1}
        assert route_after_guardrail(state) == "chat_react_retry"

    def test_whitespace_only_retries(self, mock_config):
        state = {"messages": [AIMessage(content="   \n  ")], "empty_retries": 1}
        assert route_after_guardrail(state) == "chat_react_retry"

    def test_exhausted_retries_goes_to_end(self, mock_config):
        # max_retries=2 in TEST_CONFIG, retries=3 → exhausted
        state = {"messages": [AIMessage(content="")], "empty_retries": 3}
        assert route_after_guardrail(state) == END

    def test_no_messages_goes_to_end(self, mock_config):
        state = {"messages": []}
        assert route_after_guardrail(state) == END


# -- block_node --------------------------------------------------------

class TestBlockNode:

    @pytest.mark.asyncio
    async def test_returns_polite_message(self):
        result = await block_node({})
        content = result["messages"][0].content
        assert "Vicinity" in content
        assert "housing" in content.lower()


# -- guardrail node ----------------------------------------------------

class TestGuardrail:

    @pytest.mark.asyncio
    async def test_passes_normal_response(self, mock_config):
        state = {"messages": [AIMessage(content="Short answer.")], "empty_retries": 0}
        result = await guardrail(state)
        assert result == {}

    @pytest.mark.asyncio
    async def test_truncates_long_response(self, mock_config):
        long_content = "x" * 20000
        state = {"messages": [AIMessage(content=long_content)], "empty_retries": 0}
        result = await guardrail(state)
        assert "[Response truncated]" in result["messages"][0].content

    @pytest.mark.asyncio
    async def test_empty_response_increments_retries(self, mock_config):
        state = {"messages": [AIMessage(content="")], "empty_retries": 0}
        result = await guardrail(state)
        assert result["empty_retries"] == 1

    @pytest.mark.asyncio
    async def test_all_tools_failed_returns_fallback(self, mock_config):
        msgs = [
            ToolMessage(content='{"success": false, "error": "timeout"}', tool_call_id="c1"),
            ToolMessage(content='{"success": false, "error": "timeout"}', tool_call_id="c2"),
            AIMessage(content="Let me tell you about that listing..."),
        ]
        state = {"messages": msgs, "empty_retries": 0}
        result = await guardrail(state)
        assert "retrieve the data" in result["messages"][0].content.lower()

    @pytest.mark.asyncio
    async def test_scrubs_pii_from_output(self, mock_config):
        state = {"messages": [AIMessage(content="Call 617-555-1234 for details")], "empty_retries": 0}
        result = await guardrail(state)
        assert "[REDACTED]" in result["messages"][0].content
        assert "617-555-1234" not in result["messages"][0].content

    @pytest.mark.asyncio
    async def test_handles_empty_messages(self, mock_config):
        result = await guardrail({"messages": []})
        assert result == {}