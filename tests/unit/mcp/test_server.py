"""Tests for mcp_vicinity.server — login, send_message, status."""

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from mcp_vicinity.auth import MCPSession
from mcp_vicinity import server as srv


@pytest.fixture(autouse=True)
def _reset_session():
    srv._session = MCPSession()
    yield
    srv._session = MCPSession()


class TestLogin:

    def test_success(self):
        mock_session = MCPSession(
            user_id="u-001", email="test@test.com",
            user_context={
                "budget_min": 1500, "budget_max": 3000,
                "bedrooms_min": 2, "work_address": "MIT",
                "preference_tags": ["safety", "gym"],
                "active_bookmarks": [{"id": "b1"}, {"id": "b2"}],
            },
        )
        with patch("mcp_vicinity.server.authenticate_by_credentials",
                   return_value=mock_session):
            result = json.loads(srv.login("test@test.com", "pass123"))

        assert result["success"] is True
        assert result["user_id"] == "u-001"
        assert result["profile"]["budget_max"] == 3000
        assert result["profile"]["bookmarks"] == 2
        assert srv._session.authenticated

    def test_failure(self):
        with patch("mcp_vicinity.server.authenticate_by_credentials",
                   return_value=MCPSession()):
            result = json.loads(srv.login("bad@test.com", "wrong"))

        assert result["success"] is False
        assert not srv._session.authenticated

    def test_resets_pipeline(self):
        srv._session.pipeline = MagicMock()

        mock_session = MCPSession(user_id="u-002", email="new@test.com")
        with patch("mcp_vicinity.server.authenticate_by_credentials",
                   return_value=mock_session):
            srv.login("new@test.com", "pass")

        assert srv._session.pipeline is None


class TestSendMessage:

    @pytest.mark.asyncio
    async def test_returns_response(self):
        mock_pipeline = AsyncMock()

        async def _fake_send(msg):
            yield {"type": "token", "data": {"content": "Found "}}
            yield {"type": "token", "data": {"content": "3 listings."}}

        mock_pipeline.send = _fake_send

        with patch.object(srv, "_ensure_pipeline", return_value=mock_pipeline):
            result = await srv.send_message("find apartments")

        assert result == "Found 3 listings."

    @pytest.mark.asyncio
    async def test_handles_interrupt(self):
        mock_pipeline = AsyncMock()

        async def _fake_send(msg):
            yield {"type": "token", "data": {"content": "I'd like to bookmark."}}
            yield {"type": "interrupt", "data": {"summary": "Bookmark listing X"}}

        mock_pipeline.send = _fake_send

        with patch.object(srv, "_ensure_pipeline", return_value=mock_pipeline):
            result = await srv.send_message("bookmark this")

        assert "Pending confirmation" in result
        assert "Bookmark listing X" in result

    @pytest.mark.asyncio
    async def test_handles_error_event(self):
        mock_pipeline = AsyncMock()

        async def _fake_send(msg):
            yield {"type": "error", "data": {"error": "DB connection lost"}}

        mock_pipeline.send = _fake_send

        with patch.object(srv, "_ensure_pipeline", return_value=mock_pipeline):
            result = await srv.send_message("search")

        assert "Error" in result
        assert "DB connection lost" in result

    @pytest.mark.asyncio
    async def test_handles_pipeline_init_failure(self):
        """_ensure_pipeline is now inside try block — init failures return clean error."""
        async def _fail():
            raise Exception("init failed")

        with patch.object(srv, "_ensure_pipeline", side_effect=Exception("init failed")):
            result = await srv.send_message("hello")

        assert "Error" in result
        assert "init failed" in result

    @pytest.mark.asyncio
    async def test_empty_response(self):
        mock_pipeline = AsyncMock()

        async def _fake_send(msg):
            return
            yield

        mock_pipeline.send = _fake_send

        with patch.object(srv, "_ensure_pipeline", return_value=mock_pipeline):
            result = await srv.send_message("test")

        assert "No response" in result or "rephrasing" in result

    @pytest.mark.asyncio
    async def test_node_end_content(self):
        mock_pipeline = AsyncMock()

        async def _fake_send(msg):
            yield {"type": "node_end", "data": {"content": "Blocked: off-topic."}}

        mock_pipeline.send = _fake_send

        with patch.object(srv, "_ensure_pipeline", return_value=mock_pipeline):
            result = await srv.send_message("weather in tokyo")

        assert "Blocked" in result


class TestStatus:

    def test_unauthenticated(self):
        result = json.loads(srv.get_status())
        assert result["authenticated"] is False
        assert result["user_id"] is None

    def test_authenticated(self):
        srv._session = MCPSession(
            user_id="u-001", email="test@test.com", session_id="s-001",
        )
        result = json.loads(srv.get_status())
        assert result["authenticated"] is True
        assert result["user_id"] == "u-001"
        assert result["session_id"] == "s-001"