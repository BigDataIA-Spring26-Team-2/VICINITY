"""Tests for mcp_vicinity.auth — session management and authentication.

Patch targets: auth.py uses deferred imports (from app.X import Y inside
function bodies), so we patch at the source module, not mcp_vicinity.auth.
"""

import pytest
from unittest.mock import patch, MagicMock

from mcp_vicinity.auth import (
    MCPSession,
    authenticate_by_email,
    authenticate_by_credentials,
)
from tests.unit.mcp import TEST_USER_ID, TEST_EMAIL, TEST_PASSWORD


class TestMCPSession:

    def test_unauthenticated_by_default(self):
        session = MCPSession()
        assert not session.authenticated
        assert session.user_id is None
        assert session.user_context == {}

    def test_authenticated_with_user_id(self):
        session = MCPSession(user_id="u-123", email="a@b.com")
        assert session.authenticated


class TestAuthenticateByEmail:

    def test_success(self):
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (TEST_USER_ID,)

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch("app.core.database._connect", return_value=mock_conn), \
             patch("app.services.user_data.load_user_session", return_value={
                 "session_id": "s-001",
                 "budget_max": 3000,
             }):
            session = authenticate_by_email(TEST_EMAIL)

        assert session.authenticated
        assert session.user_id == TEST_USER_ID
        assert session.email == TEST_EMAIL
        assert session.user_context["budget_max"] == 3000

    def test_user_not_found(self):
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch("app.core.database._connect", return_value=mock_conn):
            session = authenticate_by_email("nobody@test.com")

        assert not session.authenticated
        assert session.user_id is None

    def test_db_error(self):
        with patch("app.core.database._connect", side_effect=Exception("connection refused")):
            session = authenticate_by_email(TEST_EMAIL)

        assert not session.authenticated


class TestAuthenticateByCredentials:

    def test_success(self):
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        mock_auth_result = MagicMock()
        mock_auth_result.success = True
        mock_auth_result.data = [{"user_id": TEST_USER_ID, "email": TEST_EMAIL}]

        with patch("app.core.database._connect", return_value=mock_conn), \
             patch("app.services.user_data.authenticate_user", return_value=mock_auth_result), \
             patch("app.services.user_data.load_user_session", return_value={
                 "session_id": "s-002",
                 "preference_tags": ["safety", "gym"],
             }):
            session = authenticate_by_credentials(TEST_EMAIL, TEST_PASSWORD)

        assert session.authenticated
        assert session.user_id == TEST_USER_ID
        assert session.user_context["preference_tags"] == ["safety", "gym"]

    def test_wrong_password(self):
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        mock_auth_result = MagicMock()
        mock_auth_result.success = False

        with patch("app.core.database._connect", return_value=mock_conn), \
             patch("app.services.user_data.authenticate_user", return_value=mock_auth_result):
            session = authenticate_by_credentials(TEST_EMAIL, "wrongpass")

        assert not session.authenticated

    def test_db_error(self):
        with patch("app.core.database._connect", side_effect=Exception("timeout")):
            session = authenticate_by_credentials(TEST_EMAIL, TEST_PASSWORD)

        assert not session.authenticated