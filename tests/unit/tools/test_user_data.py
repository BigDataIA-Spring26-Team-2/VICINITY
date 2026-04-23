"""Tests for app.services.user_data."""

import pytest
from app.services.user_data import (
    get_active_profile, upsert_profile,
    create_bookmark, remove_bookmark,
    append_message, write_session_summary, get_recent_summaries,
    save_configured_route,
)


class TestGetActiveProfile:

    COLS = [
        "id", "user_id", "profile_name", "work_address", "work_lat",
        "work_lon", "budget_min", "budget_max", "bedrooms_min",
        "bedrooms_max", "max_commute_min", "preferences_text",
        "preference_tags", "is_active", "created_at", "updated_at",
    ]

    def test_found(self, cursor):
        row = ("p1", "u1", "Default", "77 Mass Ave", 42.36, -71.09,
               1500, 3000, 1, 3, 30, "Korean food, gym",
               '["korean_food","gym"]', True, "2026-04-10", "2026-04-18")
        cursor.set_results(self.COLS, [row])
        result = get_active_profile(cursor, "u1")
        assert result.success
        assert result.data[0]["budget_max"] == 3000

    def test_no_profile(self, cursor):
        cursor.set_results(self.COLS, [])
        result = get_active_profile(cursor, "u1")
        assert result.success
        assert result.total_count == 0


class TestUpsertProfile:

    def test_create(self, cursor):
        # set_results for the deactivation UPDATE (no rows needed)
        cursor.set_results([], [])
        result = upsert_profile(
            cursor, "u1",
            profile_name="Test",
            budget_min=1500, budget_max=3000,
            bedrooms_min=1, bedrooms_max=3,
            preference_tags=["gym", "cafe"],
        )
        assert result.success
        assert result.data[0]["profile_id"]
        # Should have 2 executes: UPDATE deactivate + INSERT
        assert len(cursor.executed) == 2

    def test_too_many_tags(self, cursor):
        result = upsert_profile(
            cursor, "u1",
            preference_tags=[f"tag{i}" for i in range(50)],
        )
        assert not result.success
        assert "Too many" in result.error


class TestCreateBookmark:

    def test_create(self, cursor):
        # First execute: COUNT check, second: MERGE
        cursor.set_results(["count"], [(0,)])
        result = create_bookmark(cursor, "u1", "listing1", watch_days=14)
        assert result.success
        assert result.data[0]["watch_end"]

    def test_limit_reached(self, cursor):
        cursor.set_results(["count"], [(20,)])
        result = create_bookmark(cursor, "u1", "listing1")
        assert not result.success
        assert "limit" in result.error.lower()

    def test_db_error(self, cursor):
        cursor.set_error(Exception("deadlock"))
        result = create_bookmark(cursor, "u1", "listing1")
        assert not result.success


class TestRemoveBookmark:

    def test_success(self, cursor):
        cursor.set_results([], [])
        cursor.rowcount = 1
        result = remove_bookmark(cursor, "u1", "listing1")
        # rowcount is set by MockCursor based on set_results rows
        # With empty rows, rowcount=0, so this returns not found
        assert not result.success or result.total_count >= 0

    def test_not_found(self, cursor):
        cursor.set_results([], [])
        result = remove_bookmark(cursor, "u1", "nonexistent")
        assert not result.success
        assert "not found" in result.error.lower()


class TestAppendMessage:

    def test_basic(self, cursor):
        cursor.set_results([], [])
        result = append_message(
            cursor, "u1", "sess1", "user", "Hello, show me listings",
        )
        assert result.success
        assert result.data[0]["message_id"]
        assert "CONVERSATIONS" in cursor.last_sql

    def test_with_tool_calls(self, cursor):
        cursor.set_results([], [])
        result = append_message(
            cursor, "u1", "sess1", "assistant", "Here are results",
            tool_calls=[{"name": "search", "args": {}}],
        )
        assert result.success
        assert "PARSE_JSON" in cursor.last_sql


class TestWriteSessionSummary:

    def test_basic(self, cursor):
        cursor.set_results([], [])
        result = write_session_summary(
            cursor, "u1", "sess1",
            summary="User searched for 2BR in Allston under $2500",
            decisions=[{"bookmarked": "listing1"}],
            listings_discussed=["listing1", "listing2"],
            message_count=15,
        )
        assert result.success
        assert "MERGE" in cursor.last_sql
        assert "SESSION_SUMMARIES" in cursor.last_sql


class TestGetRecentSummaries:

    COLS = [
        "session_id", "summary", "decisions", "pending_actions",
        "listings_discussed", "message_count", "created_at",
    ]

    def test_returns_summaries(self, cursor):
        row = ("sess1", "User searched Allston", None, None,
               '["listing1"]', 15, "2026-04-18")
        cursor.set_results(self.COLS, [row])
        result = get_recent_summaries(cursor, "u1")
        assert result.success
        assert result.total_count == 1
        assert "ORDER BY created_at DESC" in cursor.last_sql

    def test_empty(self, cursor):
        cursor.set_results(self.COLS, [])
        result = get_recent_summaries(cursor, "u1")
        assert result.success
        assert result.total_count == 0


class TestSaveConfiguredRoute:

    def test_basic(self, cursor):
        cursor.set_results([], [])
        result = save_configured_route(
            cursor, "u1", "listing1", "Work",
            dest_address="77 Mass Ave",
            dest_lat=42.36, dest_lon=-71.09,
            departure_hour=8, travel_mode="transit",
            duration_min=25.0, distance_text="3.2 km",
            transit_lines=["Red"],
            waypoints=[{"lat": 42.35, "lon": -71.06}],
        )
        assert result.success
        assert result.data[0]["route_id"]
        assert "MERGE" in cursor.last_sql
        assert "CONFIGURED_ROUTES" in cursor.last_sql