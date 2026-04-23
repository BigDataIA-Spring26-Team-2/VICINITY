"""Tests for the input_gate node in app.agents.graph."""

import pytest
from unittest.mock import patch, AsyncMock
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.graph import input_gate


@pytest.fixture
def gate_state(sample_state):
    """State builder for gate tests."""
    return sample_state


class TestInputGateRouting:

    @pytest.mark.asyncio
    async def test_routes_chat(self, gate_state, mock_config, mock_chain):
        chain = mock_chain(content='{"route": "chat", "reason": "listing question"}')
        with patch("app.agents.graph.create_chain", return_value=chain):
            result = await input_gate(gate_state("What is the safety score for listing abc?"))
        assert result["route"] == "chat"
        assert result["is_valid"] is True

    @pytest.mark.asyncio
    async def test_routes_organizer(self, gate_state, mock_config, mock_chain):
        chain = mock_chain(content='{"route": "organizer", "reason": "bookmark request"}')
        with patch("app.agents.graph.create_chain", return_value=chain):
            result = await input_gate(gate_state("Bookmark listing abc123"))
        assert result["route"] == "organizer"

    @pytest.mark.asyncio
    async def test_routes_search(self, gate_state, mock_config, mock_chain):
        chain = mock_chain(content='{"route": "search", "reason": "apartment search"}')
        with patch("app.agents.graph.create_chain", return_value=chain):
            result = await input_gate(gate_state("Find me a 2BR under $2500 in Allston"))
        assert result["route"] == "search"

    @pytest.mark.asyncio
    async def test_routes_report(self, gate_state, mock_config, mock_chain):
        chain = mock_chain(content='{"route": "report", "reason": "comparison request"}')
        with patch("app.agents.graph.create_chain", return_value=chain):
            result = await input_gate(gate_state("Give me a comparison report"))
        assert result["route"] == "report"

    @pytest.mark.asyncio
    async def test_routes_block(self, gate_state, mock_config, mock_chain):
        chain = mock_chain(content='{"route": "block", "reason": "off topic"}')
        with patch("app.agents.graph.create_chain", return_value=chain):
            result = await input_gate(gate_state("What's the weather in Paris?"))
        assert result["route"] == "block"
        assert result["is_valid"] is False


class TestInputGatePendingConfirmation:

    @pytest.mark.asyncio
    async def test_pending_confirmation_routes_confirm(self, gate_state, mock_config):
        pending = {
            "tool": "manage_bookmarks",
            "summary": "Bookmark listing abc",
            "params": {"action": "add"},
            "tool_calls": [],
        }
        state = gate_state("yes", pending_confirmation=pending)
        result = await input_gate(state)
        assert result["route"] == "confirm"


class TestInputGateEdgeCases:

    @pytest.mark.asyncio
    async def test_query_too_long(self, gate_state, mock_config):
        long_msg = "x" * 6000
        state = gate_state(long_msg)
        result = await input_gate(state)
        assert result["route"] == "block"
        assert result["is_valid"] is False
        assert "too long" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_llm_parse_failure_falls_back_to_chat(self, gate_state, mock_config, mock_chain):
        chain = mock_chain(content="not valid json at all")
        with patch("app.agents.graph.create_chain", return_value=chain):
            result = await input_gate(gate_state("Hello"))
        assert result["route"] == "chat"

    @pytest.mark.asyncio
    async def test_invalid_route_falls_back_to_chat(self, gate_state, mock_config, mock_chain):
        chain = mock_chain(content='{"route": "unknown_route", "reason": "test"}')
        with patch("app.agents.graph.create_chain", return_value=chain):
            result = await input_gate(gate_state("Hello"))
        assert result["route"] == "chat"

    @pytest.mark.asyncio
    async def test_gate_disabled_returns_chat(self, sample_state):
        disabled_config = {"input_gate": {"enabled": False}}
        with patch("app.agents.graph._load_config", return_value=disabled_config):
            result = await input_gate(sample_state("Hello"))
        assert result["route"] == "chat"