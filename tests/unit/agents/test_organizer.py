"""Tests for app.agents.organizer — plan/confirm split, HITL flow."""

import pytest
from unittest.mock import patch, MagicMock
from langchain_core.messages import AIMessage

from app.agents.organizer import (
    organizer_plan,
    organizer_confirm,
    _is_no_confirm_tool,
    _build_summary,
    _parse_response,
)
from tests.unit.agents.conftest import MockLLM, make_tool_call


# -- _is_no_confirm_tool -------------------------------------------

class TestIsNoConfirmTool:

    def test_true_for_single_conversation(self):
        calls = [make_tool_call("manage_conversations")]
        assert _is_no_confirm_tool(calls) is True

    def test_true_for_multiple_conversations(self):
        calls = [make_tool_call("manage_conversations", call_id="c1"),
                 make_tool_call("manage_conversations", call_id="c2")]
        assert _is_no_confirm_tool(calls) is True

    def test_true_for_update_pipeline_queries(self):
        calls = [make_tool_call("update_pipeline_queries")]
        assert _is_no_confirm_tool(calls) is True

    def test_true_for_mixed_no_confirm(self):
        calls = [make_tool_call("manage_conversations", call_id="c1"),
                 make_tool_call("update_pipeline_queries", call_id="c2")]
        assert _is_no_confirm_tool(calls) is True

    def test_false_for_bookmark(self):
        calls = [make_tool_call("manage_bookmarks")]
        assert _is_no_confirm_tool(calls) is False

    def test_false_for_mixed(self):
        calls = [make_tool_call("manage_conversations"),
                 make_tool_call("manage_bookmarks")]
        assert _is_no_confirm_tool(calls) is False

    def test_false_for_empty(self):
        assert _is_no_confirm_tool([]) is False


# -- _build_summary ---------------------------------------------------

class TestBuildSummary:

    def test_profile_with_budget_and_beds(self):
        s = _build_summary("manage_profile", {"budget_min": 1500, "budget_max": 3000, "bedrooms_min": 2})
        assert "budget" in s
        assert "$1500" in s
        assert "$3000" in s
        assert "2+" in s

    def test_profile_with_work(self):
        s = _build_summary("manage_profile", {"work_address": "77 Mass Ave"})
        assert "77 Mass Ave" in s

    def test_profile_empty_args(self):
        s = _build_summary("manage_profile", {})
        assert s == "Update profile"

    def test_bookmark_add(self):
        s = _build_summary("manage_bookmarks", {"action": "add", "listing_id": "abc", "watch_days": 14})
        assert "Bookmark" in s
        assert "abc" in s
        assert "14" in s

    def test_bookmark_remove(self):
        s = _build_summary("manage_bookmarks", {"action": "remove", "listing_id": "abc"})
        assert "Remove" in s
        assert "abc" in s

    def test_destinations(self):
        s = _build_summary("manage_destinations", {
            "listing_id": "abc", "dest_address": "77 Mass Ave", "travel_mode": "transit",
        })
        assert "transit" in s
        assert "abc" in s
        assert "77 Mass Ave" in s

    def test_flag_data_listing(self):
        s = _build_summary("flag_data", {"listing_id": "abc"})
        assert "Flag" in s
        assert "abc" in s

    def test_flag_data_signal(self):
        s = _build_summary("flag_data", {"signal_id": "sig-001"})
        assert "sig-001" in s

    def test_update_pipeline_queries(self):
        s = _build_summary("update_pipeline_queries", {"tag": "bharatanatyam"})
        assert "tracking" in s.lower()
        assert "bharatanatyam" in s

    def test_unknown_tool(self):
        s = _build_summary("some_unknown_tool", {})
        assert "some_unknown_tool" in s


# -- _parse_response --------------------------------------------------

class TestParseResponse:

    @pytest.mark.parametrize("value", [
        "yes", "Yes", "YES", "y", "Y",
        "confirm", "CONFIRM",
        "go ahead", "Go Ahead",
        "approved", "ok", "OK", "sure", "do it", "proceed", "yep",
        "yeah", "yes please", "go for it", "sounds good",
    ])
    def test_affirmative_strings(self, value):
        assert _parse_response(value) == "approve"

    @pytest.mark.parametrize("value", [
        "no", "No", "NO", "n",
        "cancel", "nope", "nevermind", "never mind",
        "stop", "abort", "nah", "no thanks",
        "forget it", "skip", "don't",
    ])
    def test_rejection_strings(self, value):
        assert _parse_response(value) == "reject"

    @pytest.mark.parametrize("value", [
        "change budget to 3000",
        "make it walking instead",
        "hmm let me think",
        "I want 60 days instead",
        "can you change the watch period",
        "actually make it 3 bedrooms",
    ])
    def test_modification_strings(self, value):
        assert _parse_response(value) == "modify"

    def test_empty_string_is_reject(self):
        assert _parse_response("") == "reject"

    def test_bool_true(self):
        assert _parse_response(True) == "approve"

    def test_bool_false(self):
        assert _parse_response(False) == "reject"

    def test_dict_approved_true(self):
        assert _parse_response({"approved": True}) == "approve"

    def test_dict_approved_false(self):
        assert _parse_response({"approved": False}) == "modify"

    def test_dict_rejected_true(self):
        assert _parse_response({"rejected": True}) == "reject"

    def test_dict_no_approved_key(self):
        assert _parse_response({"something": "else"}) == "modify"

    def test_none(self):
        assert _parse_response(None) == "reject"

    def test_int(self):
        assert _parse_response(42) == "reject"


# -- organizer_plan ---------------------------------------------------

class TestOrganizerPlan:

    @pytest.mark.asyncio
    async def test_no_tools_returns_clarification(self, sample_state, mock_config):
        mock = MockLLM(AIMessage(content="What listing do you want to bookmark?"))
        with patch("app.agents.organizer.create_llm", return_value=mock):
            result = await organizer_plan(sample_state("Bookmark something"))
        assert result["sub_agent_result"]["status"] == "no_action"
        assert "What listing" in result["messages"][0].content

    @pytest.mark.asyncio
    async def test_conversation_bypasses_confirmation(self, sample_state, mock_config):
        tc = [make_tool_call("manage_conversations", {"action": "message", "content": "hi"})]
        mock = MockLLM(AIMessage(content="", tool_calls=tc))
        with patch("app.agents.organizer.create_llm", return_value=mock):
            result = await organizer_plan(sample_state("Log this message"))
        assert "pending_confirmation" not in result
        assert result["messages"][0].tool_calls == tc

    @pytest.mark.asyncio
    async def test_pipeline_queries_bypasses_confirmation(self, sample_state, mock_config):
        tc = [make_tool_call("update_pipeline_queries", {
            "tag": "bharatanatyam",
            "reddit_queries": ["bharatanatyam classes Allston"],
        })]
        mock = MockLLM(AIMessage(content="", tool_calls=tc))
        with patch("app.agents.organizer.create_llm", return_value=mock):
            result = await organizer_plan(sample_state("Track bharatanatyam in Allston"))
        assert "pending_confirmation" not in result
        assert result["messages"][0].tool_calls == tc

    @pytest.mark.asyncio
    async def test_write_tool_sets_pending_confirmation(self, sample_state, mock_config):
        tc = [make_tool_call("manage_bookmarks", {"action": "add", "listing_id": "abc", "user_id": "u1"})]
        mock = MockLLM(AIMessage(content="", tool_calls=tc))
        with patch("app.agents.organizer.create_llm", return_value=mock):
            result = await organizer_plan(sample_state("Bookmark listing abc"))

        assert result["pending_confirmation"] is not None
        assert result["pending_confirmation"]["tool"] == "manage_bookmarks"
        assert result["pending_confirmation"]["tool_calls"] == tc
        # Preview message should NOT have tool_calls
        assert not result["messages"][0].tool_calls
        assert "Shall I proceed" in result["messages"][0].content

    @pytest.mark.asyncio
    async def test_pending_stores_original_tool_calls(self, sample_state, mock_config):
        tc = [make_tool_call("manage_destinations", {
            "user_id": "u1", "listing_id": "abc",
            "dest_label": "Work", "dest_address": "77 Mass Ave",
        })]
        mock = MockLLM(AIMessage(content="", tool_calls=tc))
        with patch("app.agents.organizer.create_llm", return_value=mock):
            result = await organizer_plan(sample_state("Compute route from abc to work"))
        stored_calls = result["pending_confirmation"]["tool_calls"]
        assert stored_calls[0]["name"] == "manage_destinations"
        assert stored_calls[0]["args"]["dest_address"] == "77 Mass Ave"

    @pytest.mark.asyncio
    async def test_unauthenticated_user_blocked(self, sample_state, mock_config):
        state = sample_state("Bookmark listing abc")
        state["user_context"] = {}  # no user_id
        with patch("app.agents.organizer.create_llm") as mock_llm:
            result = await organizer_plan(state)
        assert result["sub_agent_result"]["status"] == "auth_required"
        assert "sign in" in result["messages"][0].content.lower()
        mock_llm.assert_not_called()


# -- organizer_confirm ------------------------------------------------

class TestOrganizerConfirm:

    @pytest.mark.asyncio
    async def test_no_pending_returns_nothing(self, sample_state, mock_config):
        state = sample_state(pending_confirmation=None)
        result = await organizer_confirm(state)
        assert "Nothing pending" in result["messages"][0].content

    @pytest.mark.asyncio
    async def test_approved_reconstructs_tool_calls(self, sample_state, mock_config):
        original_tc = [make_tool_call("manage_bookmarks", {"action": "add", "listing_id": "abc"})]
        pending = {
            "tool": "manage_bookmarks",
            "summary": "Bookmark abc",
            "params": {"action": "add", "listing_id": "abc"},
            "tool_calls": original_tc,
        }
        state = sample_state(pending_confirmation=pending)
        with patch("app.agents.organizer.interrupt", return_value="yes"):
            result = await organizer_confirm(state)

        assert result["pending_confirmation"] is None
        ai_msg = result["messages"][0]
        assert ai_msg.tool_calls == original_tc

    @pytest.mark.asyncio
    async def test_rejected_clears_pending(self, sample_state, mock_config):
        pending = {
            "tool": "manage_bookmarks",
            "summary": "Bookmark abc",
            "params": {},
            "tool_calls": [make_tool_call("manage_bookmarks")],
        }
        state = sample_state(pending_confirmation=pending)
        with patch("app.agents.organizer.interrupt", return_value="no"):
            result = await organizer_confirm(state)

        assert result["pending_confirmation"] is None
        assert "cancelled" in result["messages"][0].content.lower()
        assert result["sub_agent_result"]["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_modification_routes_back_to_plan(self, sample_state, mock_config):
        pending = {
            "tool": "manage_profile",
            "summary": "Update profile",
            "params": {},
            "tool_calls": [make_tool_call("manage_profile")],
        }
        state = sample_state(pending_confirmation=pending)
        with patch("app.agents.organizer.interrupt", return_value="change budget to 3000"):
            result = await organizer_confirm(state)

        assert result["pending_confirmation"] is None
        assert result["sub_agent_result"]["status"] == "modified"
        # Should have 2 messages: AI acknowledgment + HumanMessage with modification
        assert len(result["messages"]) == 4
        assert "change budget to 3000" in result["messages"][3].content