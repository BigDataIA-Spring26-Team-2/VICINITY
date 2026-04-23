"""Tests for app.routers.chat — /chat/send and /chat/resume.

Note: TestClient handles async endpoints synchronously — test functions
must be regular `def`, NOT `async def`.
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.unit.routers.conftest import TEST_USER_ID, TEST_EMAIL


# =====================================================================
# Helpers
# =====================================================================

def parse_sse_events(response) -> list[dict]:
    """Parse SSE text/event-stream response into event dicts."""
    events = []
    for line in response.text.split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return events


class MockPipeline:
    """Mock ChatPipeline. No LLM, no Snowflake."""

    def __init__(self, events=None):
        self.session_id = "sess-mock-001"
        self.user_id = None
        self.message_count = 0
        self.listings_discussed = set()
        self._user_context = {}
        self._initialized = False
        self._has_interrupt = False
        self._events = events or [
            {"type": "route", "data": {"route": "chat", "is_valid": True}},
            {"type": "token", "data": {"content": "Hello! "}},
            {"type": "token", "data": {"content": "How can I help?"}},
            {"type": "done", "data": {"elapsed_ms": 100, "tool_calls": 0,
                                       "tool_errors": 0, "message_length": 24}},
        ]

    async def initialize(self):
        self._initialized = True

    async def send(self, message):
        for event in self._events:
            yield event

    async def close(self):
        pass


# =====================================================================
# POST /chat/send
# =====================================================================

class TestChatSend:

    @patch("app.routers.chat._get_or_create_pipeline")
    def test_anonymous_send(self, mock_get_pipeline, client):
        pipeline = MockPipeline()
        mock_get_pipeline.return_value = (pipeline, "sess-001", True)

        resp = client.post("/chat/send", json={
            "message": "What neighborhoods are safest?",
        })

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        events = parse_sse_events(resp)
        types = [e["type"] for e in events]
        assert "route" in types
        assert "token" in types
        assert "done" in types

    @patch("app.routers.chat._get_or_create_pipeline")
    def test_authenticated_send(self, mock_get_pipeline, client, auth_headers):
        pipeline = MockPipeline()
        pipeline.user_id = TEST_USER_ID
        mock_get_pipeline.return_value = (pipeline, "sess-002", True)

        resp = client.post(
            "/chat/send",
            json={"message": "Show my bookmarks"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        events = parse_sse_events(resp)
        assert any(e["type"] == "done" for e in events)

    @patch("app.routers.chat._get_or_create_pipeline")
    def test_session_id_returned_in_header(self, mock_get_pipeline, client):
        pipeline = MockPipeline()
        mock_get_pipeline.return_value = (pipeline, "sess-xyz", True)

        resp = client.post("/chat/send", json={"message": "hello"})

        assert resp.headers.get("x-session-id") == "sess-xyz"

    @patch("app.routers.chat._get_or_create_pipeline")
    def test_resume_existing_session(self, mock_get_pipeline, client):
        pipeline = MockPipeline()
        mock_get_pipeline.return_value = (pipeline, "sess-existing", False)

        resp = client.post("/chat/send", json={
            "message": "continue our conversation",
            "session_id": "sess-existing",
        })

        assert resp.status_code == 200

    def test_empty_message_rejected(self, client):
        resp = client.post("/chat/send", json={"message": ""})
        assert resp.status_code == 422

    def test_missing_message_rejected(self, client):
        resp = client.post("/chat/send", json={})
        assert resp.status_code == 422

    @patch("app.routers.chat._get_or_create_pipeline")
    def test_tool_events_in_stream(self, mock_get_pipeline, client):
        events = [
            {"type": "route", "data": {"route": "chat", "is_valid": True}},
            {"type": "tool_start", "data": {"tool": "query_listings", "args": "action=search"}},
            {"type": "tool_end", "data": {"tool": "query_listings", "size": 250, "error": False}},
            {"type": "token", "data": {"content": "Found 3 listings."}},
            {"type": "done", "data": {"elapsed_ms": 500, "tool_calls": 1,
                                       "tool_errors": 0, "message_length": 17}},
        ]
        pipeline = MockPipeline(events=events)
        mock_get_pipeline.return_value = (pipeline, "sess-tools", True)

        resp = client.post("/chat/send", json={"message": "Find apartments"})
        parsed = parse_sse_events(resp)

        tool_starts = [e for e in parsed if e["type"] == "tool_start"]
        tool_ends = [e for e in parsed if e["type"] == "tool_end"]
        assert len(tool_starts) == 1
        assert tool_starts[0]["data"]["tool"] == "query_listings"
        assert len(tool_ends) == 1
        assert tool_ends[0]["data"]["error"] is False

    @patch("app.routers.chat._get_or_create_pipeline")
    def test_error_event_in_stream(self, mock_get_pipeline, client):
        events = [
            {"type": "error", "data": {"error": "LLM provider timeout"}},
        ]
        pipeline = MockPipeline(events=events)
        mock_get_pipeline.return_value = (pipeline, "sess-err", True)

        resp = client.post("/chat/send", json={"message": "hello"})
        parsed = parse_sse_events(resp)

        errors = [e for e in parsed if e["type"] == "error"]
        assert len(errors) == 1
        assert "timeout" in errors[0]["data"]["error"].lower()

    @patch("app.routers.chat._get_or_create_pipeline")
    def test_interrupt_event_in_stream(self, mock_get_pipeline, client, auth_headers):
        events = [
            {"type": "route", "data": {"route": "organizer", "is_valid": True}},
            {"type": "interrupt", "data": {
                "tool": "manage_bookmarks",
                "summary": "Bookmark listing lst-001 with 14-day watch",
            }},
        ]
        pipeline = MockPipeline(events=events)
        mock_get_pipeline.return_value = (pipeline, "sess-int", True)

        resp = client.post(
            "/chat/send",
            json={"message": "Bookmark listing lst-001"},
            headers=auth_headers,
        )
        parsed = parse_sse_events(resp)

        interrupts = [e for e in parsed if e["type"] == "interrupt"]
        assert len(interrupts) == 1
        assert "bookmark" in interrupts[0]["data"]["summary"].lower()


# =====================================================================
# POST /chat/resume
# =====================================================================

class TestChatResume:

    def test_session_not_found(self, client):
        resp = client.post("/chat/resume", json={
            "session_id": "nonexistent",
            "thread_id": "t-001",
            "response": "yes",
        })
        assert resp.status_code == 404

    def test_missing_session_id(self, client):
        resp = client.post("/chat/resume", json={
            "thread_id": "t-001",
            "response": "yes",
        })
        assert resp.status_code == 422

    def test_missing_response(self, client):
        resp = client.post("/chat/resume", json={
            "session_id": "sess-001",
            "thread_id": "t-001",
        })
        assert resp.status_code == 422