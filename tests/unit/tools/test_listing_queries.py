"""Tests for app.services.listing_queries."""

import pytest
from app.services.listing_queries import (
    get_listing_detail, search_listings, compare_listings,
    scorecard_history, route_scorecard_history,
    get_listing_by_url, get_bookmarked_listings, get_configured_routes,
)


# -- Fixtures for common row shapes --------------------------------------

LISTING_COLS = [
    "listing_id", "source", "source_url", "price", "beds", "baths", "sqft",
    "street", "unit", "city", "zip_code", "neighborhood", "lat", "lon",
    "primary_photo_url", "mls_id", "mls_status", "days_on_mls", "agent_name",
    "style", "list_date", "is_current", "first_seen_at", "last_seen_at",
    "description_text",
    "safety_score", "livability_score", "summary_is_active",
    "nearest_stops", "last_scored_at", "price_history", "safety_trend",
    "safety_metadata", "livability_metadata", "lifestyle_scores",
    "nearby_amenities", "url_status",
]

LISTING_ROW = (
    "abc123", "homeharvest", "https://realtor.com/abc", 2500, 2, 1, 850,
    "88 Wareham St", "307", "Boston", "02118", "South End", 42.34, -71.07,
    "https://photo.jpg", "MLS123", "FOR_RENT", 12, "Agent Smith",
    "apartment", "2026-03-24", True, "2026-03-20", "2026-04-18",
    "Sunny 2BR",
    75, 80, True,
    '["Park St"]', "2026-04-18", None, None,
    '{"percentile": 75}', '{"percentile": 80}', None,
    None, "active",
)


class TestGetListingDetail:

    def test_found(self, cursor):
        cursor.set_results(LISTING_COLS, [LISTING_ROW])
        result = get_listing_detail(cursor, "abc123")
        assert result.success
        assert result.total_count == 1
        assert result.data[0]["listing_id"] == "abc123"
        assert result.data[0]["price"] == 2500

    def test_not_found(self, cursor):
        cursor.set_results(LISTING_COLS, [])
        result = get_listing_detail(cursor, "nonexistent")
        assert result.success
        assert result.total_count == 0

    def test_flagged_url_warning(self, cursor):
        flagged_row = list(LISTING_ROW)
        flagged_row[-1] = "flagged"  # url_status
        cursor.set_results(LISTING_COLS, [tuple(flagged_row)])
        result = get_listing_detail(cursor, "abc123")
        assert result.success
        assert len(result.warnings) > 0
        assert "flagged" in result.warnings[0].lower()

    def test_db_error(self, cursor):
        cursor.set_error(Exception("Snowflake timeout"))
        result = get_listing_detail(cursor, "abc123")
        assert not result.success
        assert "timeout" in result.error.lower()


class TestSearchListings:

    SEARCH_COLS = [
        "listing_id", "source", "source_url", "price", "beds", "baths",
        "sqft", "street", "unit", "city", "zip_code", "neighborhood",
        "lat", "lon", "primary_photo_url", "days_on_mls", "style",
        "list_date", "is_current", "url_status",
        "safety_score", "livability_score", "nearest_stops", "last_scored_at",
    ]

    ROW_A = (
        "a1", "homeharvest", "https://r.com/a1", 2000, 1, 1, 600,
        "10 Main St", None, "Boston", "02134", "Allston",
        42.35, -71.13, "https://photo.jpg", 5, "apartment",
        "2026-04-01", True, "active", 85, 70, None, "2026-04-18",
    )
    ROW_B = (
        "b2", "homeharvest", "https://r.com/b2", 3500, 3, 2, 1200,
        "20 Beacon St", "4A", "Boston", "02108", "Beacon Hill",
        42.36, -71.06, "https://photo2.jpg", 20, "condo",
        "2026-03-15", True, "active", 60, 90, None, "2026-04-18",
    )

    def test_no_filters(self, cursor):
        cursor.set_results(self.SEARCH_COLS, [self.ROW_A, self.ROW_B])
        result = search_listings(cursor)
        assert result.success
        assert result.total_count == 2

    def test_price_filter(self, cursor):
        cursor.set_results(self.SEARCH_COLS, [self.ROW_A])
        result = search_listings(cursor, max_price=2500)
        assert result.success
        assert "price <= %s" in cursor.last_sql

    def test_neighborhood_filter(self, cursor):
        cursor.set_results(self.SEARCH_COLS, [self.ROW_A])
        result = search_listings(cursor, neighborhood="Allston")
        assert result.success
        assert "LOWER(l.neighborhood)" in cursor.last_sql

    def test_sort_validation(self, cursor):
        cursor.set_results(self.SEARCH_COLS, [])
        result = search_listings(cursor, sort_by="DROP TABLE")
        assert result.success
        # Invalid sort falls back to default, not injected
        assert "DROP TABLE" not in cursor.last_sql

    def test_limit_clamped(self, cursor):
        cursor.set_results(self.SEARCH_COLS, [])
        result = search_listings(cursor, limit=99999)
        assert result.success
        assert "LIMIT %s" in cursor.last_sql
        # Limit param should be clamped to max_results (50)
        limit_param = cursor.last_params[-2]  # second to last (before offset)
        assert limit_param <= 50

    def test_db_error(self, cursor):
        cursor.set_error(Exception("connection lost"))
        result = search_listings(cursor)
        assert not result.success


class TestCompareListings:

    COMPARE_COLS = [
        "listing_id", "source", "source_url", "price", "beds", "baths",
        "sqft", "street", "unit", "city", "zip_code", "neighborhood",
        "lat", "lon", "primary_photo_url", "days_on_mls", "style",
        "list_date", "is_current", "first_seen_at", "url_status",
        "safety_score", "livability_score",
        "safety_metadata", "livability_metadata",
        "nearest_stops", "last_scored_at",
        "latest_score_date", "crime_count", "violent_count", "crime_trend",
        "complaint_count", "citizen_incidents_48h", "citizen_nighttime_48h",
        "nearby_transit_stops", "nearby_amenity_count",
        "current_price", "scoring_metadata",
    ]

    def test_two_listings(self, cursor):
        row_a = ("a1",) + ("hh",) * 2 + (2000,) + (0,) * (len(self.COMPARE_COLS) - 4)
        row_b = ("b2",) + ("hh",) * 2 + (3000,) + (0,) * (len(self.COMPARE_COLS) - 4)
        cursor.set_results(self.COMPARE_COLS, [row_a, row_b])
        result = compare_listings(cursor, ["a1", "b2"])
        assert result.success
        assert result.total_count == 2
        # Preserves input order
        assert result.data[0]["listing_id"] == "a1"

    def test_empty_list(self, cursor):
        result = compare_listings(cursor, [])
        assert not result.success

    def test_too_many(self, cursor):
        result = compare_listings(cursor, [f"id{i}" for i in range(20)])
        assert not result.success
        assert "max" in result.error.lower()

    def test_missing_listing_warning(self, cursor):
        row = ("a1",) + (None,) * (len(self.COMPARE_COLS) - 1)
        cursor.set_results(self.COMPARE_COLS, [row])
        result = compare_listings(cursor, ["a1", "missing"])
        assert result.success
        assert any("not found" in w.lower() for w in result.warnings)


class TestScorecardHistory:

    COLS = [
        "listing_id", "score_date", "crime_count", "violent_count",
        "crime_trend", "complaint_count", "citizen_incidents_48h",
        "citizen_nighttime_48h", "nearby_transit_stops",
        "nearby_amenity_count", "listing_active", "current_price",
        "safety_score", "livability_score", "scoring_metadata",
        "pipeline_run_id",
    ]

    def test_default_lookback(self, cursor):
        row = ("abc", "2026-04-18") + (0,) * (len(self.COLS) - 2)
        cursor.set_results(self.COLS, [row])
        result = scorecard_history(cursor, "abc")
        assert result.success
        assert "DATEADD" in cursor.last_sql

    def test_explicit_date_range(self, cursor):
        cursor.set_results(self.COLS, [])
        result = scorecard_history(cursor, "abc",
                                   start_date="2026-04-01",
                                   end_date="2026-04-18")
        assert result.success
        assert "BETWEEN" in cursor.last_sql

    def test_db_error(self, cursor):
        cursor.set_error(Exception("timeout"))
        result = scorecard_history(cursor, "abc")
        assert not result.success


class TestRouteScorecardHistory:

    COLS = [
        "route_id", "listing_id", "score_date", "crime_count",
        "violent_count", "shooting_count", "crimes_at_dep_hour",
        "citizen_incidents", "citizen_nighttime",
        "scoring_metadata", "pipeline_run_id",
    ]

    def test_default(self, cursor):
        row = ("r1", "abc", "2026-04-18") + (0,) * (len(self.COLS) - 3)
        cursor.set_results(self.COLS, [row])
        result = route_scorecard_history(cursor, "r1")
        assert result.success
        assert result.total_count == 1


class TestGetListingByUrl:

    COLS = [
        "listing_id", "source", "source_url", "price", "beds", "baths",
        "street", "city", "neighborhood", "is_current", "url_status",
    ]

    def test_found(self, cursor):
        row = ("abc", "hh", "https://r.com/abc", 2500, 2, 1,
               "Main St", "Boston", "Allston", True, "active")
        cursor.set_results(self.COLS, [row])
        result = get_listing_by_url(cursor, "https://r.com/abc")
        assert result.success
        assert result.data[0]["listing_id"] == "abc"

    def test_not_found(self, cursor):
        cursor.set_results(self.COLS, [])
        result = get_listing_by_url(cursor, "https://fake.com")
        assert result.success
        assert result.total_count == 0


class TestGetBookmarkedListings:

    COLS = [
        "bookmark_id", "listing_id", "notes", "bookmark_active",
        "added_at", "watch_end",
        "source", "source_url", "price", "beds", "baths", "sqft",
        "street", "city", "neighborhood", "lat", "lon",
        "primary_photo_url", "is_current", "url_status",
        "safety_score", "livability_score", "nearest_stops", "last_scored_at",
    ]

    def test_with_bookmarks(self, cursor):
        row = ("bm1", "abc", None, True, "2026-04-10", "2026-04-24",
               "hh", "https://r.com/abc", 2500, 2, 1, 850,
               "Main St", "Boston", "Allston", 42.35, -71.13,
               None, True, "active", 75, 80, None, None)
        cursor.set_results(self.COLS, [row])
        result = get_bookmarked_listings(cursor, "user1")
        assert result.success
        assert result.total_count == 1

    def test_empty(self, cursor):
        cursor.set_results(self.COLS, [])
        result = get_bookmarked_listings(cursor, "user1")
        assert result.success
        assert result.total_count == 0


class TestGetConfiguredRoutes:

    COLS = [
        "route_id", "listing_id", "dest_label", "dest_address",
        "dest_lat", "dest_lon", "departure_hour", "travel_mode",
        "duration_min", "distance_text", "transit_lines",
        "waypoints", "waypoint_scores", "is_active", "computed_at",
    ]

    def test_returns_routes(self, cursor):
        row = ("r1", "abc", "Work", "77 Mass Ave", 42.36, -71.09,
               8, "transit", 25.0, "3.2 km", '["Red"]',
               '[{"lat":42.35,"lon":-71.06}]', None, True, "2026-04-18")
        cursor.set_results(self.COLS, [row])
        result = get_configured_routes(cursor, "user1")
        assert result.success
        assert result.data[0]["dest_label"] == "Work"

    def test_filter_by_listing(self, cursor):
        cursor.set_results(self.COLS, [])
        result = get_configured_routes(cursor, "user1", listing_id="abc")
        assert result.success
        assert "cr.listing_id = %s" in cursor.last_sql