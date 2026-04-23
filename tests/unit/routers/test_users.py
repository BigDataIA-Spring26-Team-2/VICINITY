"""Tests for app.routers.users — /users/profile, /users/bookmarks, /users/routes.

All endpoints require authentication. Tests verify:
  - Auth enforcement (401 without token)
  - Empty results for new users
  - Populated results with realistic data
  - Query parameter handling (listing_id filter on routes)
"""

import json
import pytest
from unittest.mock import patch

from tests.unit.routers.conftest import (
    TEST_USER_ID, TEST_EMAIL, PROFILE_COLS,
)


# =====================================================================
# GET /users/profile
# =====================================================================

class TestGetProfile:

    def test_returns_profile(self, client, cursor, auth_headers):
        """Authenticated user with a profile gets full profile data."""
        row = (
            "p-001", TEST_USER_ID, "Summer Search",
            "77 Mass Ave, Cambridge", 42.36, -71.09,
            1500, 3000, 2, 4, 30,
            "Korean food, gym, quiet",
            '["korean_food","gym","safety"]',
            True, "2026-04-15", "2026-04-20",
        )
        cursor.set_results(PROFILE_COLS, [row])

        resp = client.get("/users/profile", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["total_count"] == 1
        assert data["data"][0]["budget_max"] == 3000
        assert data["data"][0]["work_address"] == "77 Mass Ave, Cambridge"

    def test_no_profile(self, client, cursor, auth_headers):
        """Authenticated user with no profile gets empty data (not 404)."""
        cursor.set_results(PROFILE_COLS, [])

        resp = client.get("/users/profile", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["total_count"] == 0
        assert data["data"] == []

    def test_requires_auth(self, client, cursor):
        """No token → 401."""
        resp = client.get("/users/profile")
        assert resp.status_code == 401


# =====================================================================
# GET /users/bookmarks
# =====================================================================

class TestGetBookmarks:

    def test_returns_bookmarks(self, client, cursor, auth_headers):
        """Authenticated user with bookmarks gets listing details."""
        cols = [
            "listing_id", "street", "neighborhood", "price",
            "beds", "safety_score", "livability_score",
            "source_url", "is_active", "watch_end",
        ]
        rows = [
            ("lst-001", "123 Main St", "Allston", 2200,
             2, 75, 68, "https://realtor.com/lst-001", True, "2026-05-01"),
            ("lst-002", "456 Oak Ave", "Brighton", 2400,
             3, 82, 71, "https://realtor.com/lst-002", True, "2026-05-05"),
        ]
        cursor.set_results(cols, rows)

        resp = client.get("/users/bookmarks", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["total_count"] == 2
        assert data["data"][0]["listing_id"] == "lst-001"
        assert data["data"][1]["price"] == 2400

    def test_no_bookmarks(self, client, cursor, auth_headers):
        """No bookmarks returns empty data."""
        cursor.set_results(["listing_id"], [])

        resp = client.get("/users/bookmarks", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()["total_count"] == 0

    def test_requires_auth(self, client, cursor):
        resp = client.get("/users/bookmarks")
        assert resp.status_code == 401


# =====================================================================
# GET /users/routes
# =====================================================================

class TestGetRoutes:

    def test_returns_routes(self, client, cursor, auth_headers):
        """Authenticated user with routes gets route configurations."""
        cols = [
            "route_id", "listing_id", "dest_label", "dest_address",
            "dest_lat", "dest_lon", "departure_hour", "travel_mode",
            "duration_min", "distance_text", "transit_lines",
            "waypoints", "waypoint_scores", "is_active", "computed_at",
        ]
        rows = [
            ("r-001", "lst-001", "Work", "77 Mass Ave",
             42.36, -71.09, 8, "transit",
             25.0, "3.2 km", '["Red"]',
             '[{"lat":42.35,"lon":-71.06}]', None, True, "2026-04-18"),
        ]
        cursor.set_results(cols, rows)

        resp = client.get("/users/routes", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["total_count"] == 1
        assert data["data"][0]["dest_label"] == "Work"

    def test_filter_by_listing_id(self, client, cursor, auth_headers):
        """Query param listing_id filters routes."""
        cols = ["route_id", "listing_id", "dest_label", "dest_address",
                "dest_lat", "dest_lon", "departure_hour", "travel_mode",
                "duration_min", "distance_text", "transit_lines",
                "waypoints", "waypoint_scores", "is_active", "computed_at"]
        cursor.set_results(cols, [])

        resp = client.get(
            "/users/routes?listing_id=lst-999",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        # Verify the SQL included listing_id filter
        assert any("listing_id" in sql for sql, _ in cursor.executed)

    def test_no_routes(self, client, cursor, auth_headers):
        cursor.set_results(["route_id"], [])

        resp = client.get("/users/routes", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()["total_count"] == 0

    def test_requires_auth(self, client, cursor):
        resp = client.get("/users/routes")
        assert resp.status_code == 401