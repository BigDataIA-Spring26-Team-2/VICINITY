"""Tests for app.routers.auth — /auth/register, /auth/login, /auth/me."""

import pytest
from unittest.mock import patch

from tests.unit.routers.conftest import (
    TEST_USER_ID, TEST_EMAIL, TEST_PASSWORD, TEST_DISPLAY_NAME,
    TEST_PASSWORD_HASH,
    AUTH_COLS, USER_COLS_NO_PW, PROFILE_COLS, SUMMARY_COLS, BOOKMARK_COLS,
    make_auth_row, make_user_row_no_password,
)


# =====================================================================
# POST /auth/register
# =====================================================================

class TestRegister:

    def test_success(self, client, cursor):
        # create_user: INSERT
        cursor.queue_results([], [])
        # load_user_session: get_user_by_id → profile → bookmarks → summaries
        cursor.queue_results(USER_COLS_NO_PW, [make_user_row_no_password()])
        cursor.queue_results(PROFILE_COLS, [])
        cursor.queue_results(BOOKMARK_COLS, [])
        cursor.queue_results(SUMMARY_COLS, [])

        resp = client.post("/auth/register", json={
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD,
            "display_name": TEST_DISPLAY_NAME,
        })

        assert resp.status_code == 201
        data = resp.json()
        assert "token" in data
        assert data["email"] == TEST_EMAIL.lower()
        assert data["user_id"]
        assert "user_context" in data

    def test_duplicate_email(self, client, cursor):
        cursor.set_error(Exception("duplicate key value violates unique constraint"))

        resp = client.post("/auth/register", json={
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD,
        })

        assert resp.status_code == 409

    def test_short_password(self, client, cursor):
        resp = client.post("/auth/register", json={
            "email": TEST_EMAIL,
            "password": "short",
        })
        assert resp.status_code == 422

    def test_invalid_email(self, client, cursor):
        resp = client.post("/auth/register", json={
            "email": "not-an-email",
            "password": TEST_PASSWORD,
        })
        assert resp.status_code == 422

    def test_missing_email(self, client, cursor):
        resp = client.post("/auth/register", json={"password": TEST_PASSWORD})
        assert resp.status_code == 422

    def test_missing_password(self, client, cursor):
        resp = client.post("/auth/register", json={"email": TEST_EMAIL})
        assert resp.status_code == 422

    def test_display_name_optional(self, client, cursor):
        cursor.queue_results([], [])
        cursor.queue_results(USER_COLS_NO_PW, [
            make_user_row_no_password(display_name=None),
        ])
        cursor.queue_results(PROFILE_COLS, [])
        cursor.queue_results(BOOKMARK_COLS, [])
        cursor.queue_results(SUMMARY_COLS, [])

        resp = client.post("/auth/register", json={
            "email": "nodisplay@vicinity.app",
            "password": TEST_PASSWORD,
        })
        assert resp.status_code == 201

    def test_email_normalized_to_lowercase(self, client, cursor):
        cursor.queue_results([], [])
        cursor.queue_results(USER_COLS_NO_PW, [
            make_user_row_no_password(email="upper@vicinity.app"),
        ])
        cursor.queue_results(PROFILE_COLS, [])
        cursor.queue_results(BOOKMARK_COLS, [])
        cursor.queue_results(SUMMARY_COLS, [])

        resp = client.post("/auth/register", json={
            "email": "UPPER@vicinity.app",
            "password": TEST_PASSWORD,
        })
        assert resp.status_code == 201


# =====================================================================
# POST /auth/login
# =====================================================================

class TestLogin:

    def test_success(self, client, cursor):
        # authenticate_user: SELECT id, email, display_name, password_hash (4 cols)
        cursor.queue_results(AUTH_COLS, [make_auth_row()])
        # load_user_session: get_user_by_id → profile → bookmarks → summaries
        cursor.queue_results(USER_COLS_NO_PW, [make_user_row_no_password()])
        cursor.queue_results(PROFILE_COLS, [])
        cursor.queue_results(BOOKMARK_COLS, [])
        cursor.queue_results(SUMMARY_COLS, [])

        resp = client.post("/auth/login", json={
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD,
        })

        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data
        assert data["user_id"] == TEST_USER_ID
        assert "user_context" in data

    def test_wrong_password(self, client, cursor):
        # Returns the real user row — password verification will fail
        cursor.set_results(AUTH_COLS, [make_auth_row()])

        resp = client.post("/auth/login", json={
            "email": TEST_EMAIL,
            "password": "wrongpassword123",
        })

        assert resp.status_code == 401
        assert "invalid" in resp.json()["detail"].lower()

    def test_nonexistent_email(self, client, cursor):
        cursor.set_results(AUTH_COLS, [])  # No rows

        resp = client.post("/auth/login", json={
            "email": "nobody@vicinity.app",
            "password": TEST_PASSWORD,
        })

        assert resp.status_code == 401

    def test_user_without_password_hash(self, client, cursor):
        # Pre-migration user: password_hash is None
        row = (TEST_USER_ID, TEST_EMAIL, TEST_DISPLAY_NAME, None)
        cursor.set_results(AUTH_COLS, [row])

        resp = client.post("/auth/login", json={
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD,
        })

        assert resp.status_code == 401

    def test_missing_email(self, client, cursor):
        resp = client.post("/auth/login", json={"password": TEST_PASSWORD})
        assert resp.status_code == 422

    def test_missing_password(self, client, cursor):
        resp = client.post("/auth/login", json={"email": TEST_EMAIL})
        assert resp.status_code == 422

    def test_empty_password(self, client, cursor):
        resp = client.post("/auth/login", json={
            "email": TEST_EMAIL,
            "password": "",
        })
        assert resp.status_code == 422


# =====================================================================
# GET /auth/me
# =====================================================================

class TestMe:

    def test_valid_token(self, client, cursor, auth_headers):
        # get_user_by_id (in /me endpoint) + load_user_session chain
        cursor.queue_results(USER_COLS_NO_PW, [make_user_row_no_password()])
        cursor.queue_results(USER_COLS_NO_PW, [make_user_row_no_password()])
        cursor.queue_results(PROFILE_COLS, [])
        cursor.queue_results(BOOKMARK_COLS, [])
        cursor.queue_results(SUMMARY_COLS, [])

        resp = client.get("/auth/me", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == TEST_USER_ID
        assert data["email"] == TEST_EMAIL
        assert "user_context" in data

    def test_no_token(self, client, cursor):
        resp = client.get("/auth/me")
        assert resp.status_code == 401

    def test_expired_token(self, client, cursor, expired_token):
        resp = client.get("/auth/me", headers={
            "Authorization": f"Bearer {expired_token}",
        })
        assert resp.status_code == 401

    def test_invalid_token(self, client, cursor):
        resp = client.get("/auth/me", headers={
            "Authorization": "Bearer not.a.real.token",
        })
        assert resp.status_code == 401

    def test_deleted_user(self, client, cursor, auth_headers):
        cursor.set_results(USER_COLS_NO_PW, [])  # User not found

        resp = client.get("/auth/me", headers=auth_headers)
        assert resp.status_code == 404