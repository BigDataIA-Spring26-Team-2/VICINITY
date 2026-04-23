"""Tests for app.agents.state — schema, factory, field validation."""

import uuid

import pytest
from langchain_core.messages import HumanMessage

from app.agents.state import (
    AgentState,
    ConfirmationPayload,
    ToolCallRecord,
    UserContext,
    create_initial_state,
)


class TestCreateInitialState:

    def test_sets_human_message(self):
        state = create_initial_state({"user_id": "u1"}, "Hello")
        assert len(state["messages"]) == 1
        assert isinstance(state["messages"][0], HumanMessage)
        assert state["messages"][0].content == "Hello"

    def test_sets_user_context(self):
        uc = {"user_id": "u1", "budget_min": 1500}
        state = create_initial_state(uc, "Hi")
        assert state["user_context"]["user_id"] == "u1"
        assert state["user_context"]["budget_min"] == 1500

    def test_generates_trace_id(self):
        state = create_initial_state({}, "Hi")
        assert state["trace_id"]
        uuid.UUID(state["trace_id"])  # validates format

    def test_unique_trace_ids(self):
        s1 = create_initial_state({}, "A")
        s2 = create_initial_state({}, "B")
        assert s1["trace_id"] != s2["trace_id"]

    def test_counters_zeroed(self):
        state = create_initial_state({}, "Hi")
        assert state["tool_call_count"] == 0
        assert state["tool_call_ledger"] == []
        assert state["query_cost_usd"] == 0.0
        assert state["empty_retries"] == 0

    def test_control_fields_default(self):
        state = create_initial_state({}, "Hi")
        assert state["is_valid"] is True
        assert state["error"] is None
        assert state["route"] == ""

    def test_sub_agent_result_none(self):
        state = create_initial_state({}, "Hi")
        assert state["sub_agent_result"] is None

    def test_pending_confirmation_none(self):
        state = create_initial_state({}, "Hi")
        assert state["pending_confirmation"] is None


class TestConfirmationPayload:

    def test_has_tool_calls_field(self):
        payload: ConfirmationPayload = {
            "tool": "manage_bookmarks",
            "summary": "Bookmark listing abc",
            "params": {"action": "add", "listing_id": "abc"},
            "tool_calls": [{"name": "manage_bookmarks", "args": {}, "id": "c1", "type": "tool_call"}],
        }
        assert payload["tool_calls"][0]["name"] == "manage_bookmarks"
        assert len(payload["tool_calls"]) == 1

    def test_all_fields_present(self):
        payload: ConfirmationPayload = {
            "tool": "manage_profile",
            "summary": "Update profile",
            "params": {"budget_min": 1500},
            "tool_calls": [],
        }
        assert set(payload.keys()) == {"tool", "summary", "params", "tool_calls"}


class TestToolCallRecord:

    def test_all_fields(self):
        record: ToolCallRecord = {
            "tool_name": "query_listings",
            "args_summary": "action=detail, listing_id=abc",
            "latency_ms": 150,
            "result_size": 1024,
            "success": True,
        }
        assert record["tool_name"] == "query_listings"
        assert record["success"] is True
        assert record["latency_ms"] == 150