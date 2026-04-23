"""Shared fixtures for router unit tests.

Provides:
  - FastAPI TestClient with dependency overrides (no real Snowflake/Redis)
  - MockCursor matching the tools conftest pattern (tracks SQL, returns preset rows)
  - Auth helpers: valid JWT, expired JWT, auth headers
  - Pre-built user rows and profile rows for common test scenarios
  - Auto-reset of all service caches between tests

All router tests run in-process against the FastAPI app — no network,
no Snowflake, no Redis, no external services.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth import create_token, hash_password
from app.main import app
from app.core.database import get_cursor


# =====================================================================
# MockCursor
# =====================================================================

class MockCursor:
    """Simulates Snowflake cursor for router tests."""

    def __init__(self):
        self.executed: list[tuple[str, Optional[tuple]]] = []
        self._columns: list[str] = []
        self._rows: list[tuple] = []
        self.rowcount: int = 0
        self._error: Optional[Exception] = None
        self._result_queue: list[tuple[list[str], list[tuple]]] = []

    def set_results(self, columns: list[str], rows: list[tuple]):
        self._columns = columns
        self._rows = rows
        self.rowcount = len(rows)

    def queue_results(self, columns: list[str], rows: list[tuple]):
        self._result_queue.append((columns, rows))

    def set_error(self, error: Exception):
        self._error = error

    def execute(self, sql: str, params=None):
        if self._error:
            err = self._error
            self._error = None
            raise err
        self.executed.append((sql, params))
        if self._result_queue:
            cols, rows = self._result_queue.pop(0)
            self._columns = cols
            self._rows = rows
        self.rowcount = len(self._rows)

    def executemany(self, sql: str, params_list):
        for p in params_list:
            self.executed.append((sql, p))
        self.rowcount = len(params_list)

    def fetchall(self) -> list[tuple]:
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    @property
    def description(self):
        return [(col.upper(), None, None, None, None, None, None)
                for col in self._columns]

    def close(self):
        pass

    @property
    def last_sql(self) -> str:
        return self.executed[-1][0] if self.executed else ""

    @property
    def last_params(self):
        return self.executed[-1][1] if self.executed else None


# =====================================================================
# Test data
# =====================================================================

TEST_USER_ID = "u-test-router-001"
TEST_EMAIL = "test@vicinity.app"
TEST_PASSWORD = "securepass123"
TEST_DISPLAY_NAME = "Test User"
TEST_PASSWORD_HASH = hash_password(TEST_PASSWORD)

# Column lists matching the exact SELECT in each service function:

# authenticate_user: SELECT id, email, display_name, password_hash
AUTH_COLS = ["id", "email", "display_name", "password_hash"]

# get_user_by_id / get_user_by_email: SELECT id, email, display_name, created_at, updated_at
USER_COLS_NO_PW = ["id", "email", "display_name", "created_at", "updated_at"]

# get_active_profile: SELECT id, user_id, profile_name, ...
PROFILE_COLS = [
    "id", "user_id", "profile_name", "work_address", "work_lat",
    "work_lon", "budget_min", "budget_max", "bedrooms_min",
    "bedrooms_max", "max_commute_min", "preferences_text",
    "preference_tags", "is_active", "created_at", "updated_at",
]

# get_recent_summaries: SELECT session_id, summary, ...
SUMMARY_COLS = [
    "session_id", "summary", "decisions", "pending_actions",
    "listings_discussed", "message_count", "created_at",
]

# get_bookmarked_listings: varies by JOIN, but empty results just need any cols
BOOKMARK_COLS = ["listing_id", "street", "neighborhood", "price",
                 "beds", "safety_score", "livability_score",
                 "source_url", "is_active", "watch_end"]


def make_auth_row(
    user_id: str = TEST_USER_ID,
    email: str = TEST_EMAIL,
    display_name: str = TEST_DISPLAY_NAME,
    password_hash: str = TEST_PASSWORD_HASH,
) -> tuple:
    """Row matching AUTH_COLS (4 columns)."""
    return (user_id, email, display_name, password_hash)


def make_user_row_no_password(
    user_id: str = TEST_USER_ID,
    email: str = TEST_EMAIL,
    display_name: str = TEST_DISPLAY_NAME,
) -> tuple:
    """Row matching USER_COLS_NO_PW (5 columns)."""
    now = datetime.now(timezone.utc).isoformat()
    return (user_id, email, display_name, now, now)


# =====================================================================
# Fixtures
# =====================================================================

@pytest.fixture
def cursor():
    return MockCursor()


@pytest.fixture
def client(cursor):
    def _override_cursor():
        yield cursor

    app.dependency_overrides[get_cursor] = _override_cursor
    yield TestClient(app, raise_server_exceptions=True)
    app.dependency_overrides.clear()


@pytest.fixture
def valid_token() -> str:
    return create_token(user_id=TEST_USER_ID, email=TEST_EMAIL)


@pytest.fixture
def auth_headers(valid_token) -> dict:
    return {"Authorization": f"Bearer {valid_token}"}


@pytest.fixture
def expired_token() -> str:
    import jwt as pyjwt
    from app.config import get_settings
    payload = {
        "sub": TEST_USER_ID,
        "email": TEST_EMAIL,
        "iat": datetime(2020, 1, 1, tzinfo=timezone.utc),
        "exp": datetime(2020, 1, 2, tzinfo=timezone.utc),
    }
    return pyjwt.encode(payload, get_settings().jwt_secret, algorithm="HS256")


@pytest.fixture(autouse=True)
def _reset_caches():
    yield
    try:
        from app.services import (
            listing_queries, crime_queries, complaint_queries,
            user_data, url_health, sql_freeform,
        )
        for mod in (listing_queries, crime_queries, complaint_queries,
                    user_data, url_health, sql_freeform):
            if hasattr(mod, "reload_config"):
                mod.reload_config()
    except ImportError:
        pass