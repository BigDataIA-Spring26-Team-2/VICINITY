"""Tests for mcp_vicinity.tools — direct read tools.

Calls the actual module-level tool functions. Patches _get_cursor
and service functions at the source module level.
"""

import json
import pytest
from unittest.mock import patch, MagicMock

from mcp_vicinity.tools import (
    search_listings,
    get_listing,
    get_safety,
    get_neighborhood,
    get_amenities,
)
from tests.unit.mcp import (
    make_qr, SAMPLE_LISTING, SAMPLE_CRIME,
    SAMPLE_NEIGHBORHOOD, SAMPLE_AMENITY,
)


def _mock_cursor():
    cursor = MagicMock()
    cursor.close = MagicMock()
    return cursor


# =====================================================================
# search_listings
# =====================================================================

class TestSearchListings:

    def test_returns_results(self):
        cursor = _mock_cursor()
        with patch("mcp_vicinity.tools._get_cursor", return_value=cursor), \
             patch("app.services.listing_queries.search_listings",
                   return_value=make_qr(data=[SAMPLE_LISTING])):
            result = json.loads(search_listings(max_price=3000, beds_min=2))

        assert result["success"] is True
        assert result["total_count"] == 1
        assert result["data"][0]["listing_id"] == "lst-mcp-001"

    def test_passes_all_params(self):
        cursor = _mock_cursor()
        with patch("mcp_vicinity.tools._get_cursor", return_value=cursor), \
             patch("app.services.listing_queries.search_listings",
                   return_value=make_qr()) as mock:
            search_listings(
                min_price=1000, max_price=3000, beds_min=2, beds_max=4,
                neighborhood="Allston", city="Boston",
                min_safety_score=50, sort_by="price", limit=10,
            )

        kw = mock.call_args[1]
        assert kw["min_price"] == 1000
        assert kw["max_price"] == 3000
        assert kw["beds_min"] == 2
        assert kw["neighborhood"] == "Allston"
        assert kw["sort_by"] == "price"
        assert kw["limit"] == 10

    def test_empty_results(self):
        cursor = _mock_cursor()
        with patch("mcp_vicinity.tools._get_cursor", return_value=cursor), \
             patch("app.services.listing_queries.search_listings",
                   return_value=make_qr(data=[])):
            result = json.loads(search_listings())

        assert result["success"] is True
        assert result["total_count"] == 0

    def test_connection_error_returns_json(self):
        with patch("mcp_vicinity.tools._get_cursor",
                   side_effect=Exception("connection refused")):
            result = json.loads(search_listings())

        assert result["success"] is False
        assert "connection refused" in result["error"]

    def test_service_error_returns_json(self):
        cursor = _mock_cursor()
        with patch("mcp_vicinity.tools._get_cursor", return_value=cursor), \
             patch("app.services.listing_queries.search_listings",
                   side_effect=Exception("query failed")):
            result = json.loads(search_listings())

        assert result["success"] is False
        cursor.close.assert_called_once()

    def test_cursor_closed_on_success(self):
        cursor = _mock_cursor()
        with patch("mcp_vicinity.tools._get_cursor", return_value=cursor), \
             patch("app.services.listing_queries.search_listings",
                   return_value=make_qr()):
            search_listings()

        cursor.close.assert_called_once()


# =====================================================================
# get_listing
# =====================================================================

class TestGetListing:

    def test_returns_detail(self):
        cursor = _mock_cursor()
        with patch("mcp_vicinity.tools._get_cursor", return_value=cursor), \
             patch("app.services.listing_queries.get_listing_detail",
                   return_value=make_qr(data=[SAMPLE_LISTING])):
            result = json.loads(get_listing("lst-mcp-001"))

        assert result["success"] is True
        assert result["data"][0]["street"] == "100 Test St"

    def test_passes_listing_id(self):
        cursor = _mock_cursor()
        with patch("mcp_vicinity.tools._get_cursor", return_value=cursor), \
             patch("app.services.listing_queries.get_listing_detail",
                   return_value=make_qr()) as mock:
            get_listing("abc123")

        assert mock.call_args[0][1] == "abc123"

    def test_not_found(self):
        cursor = _mock_cursor()
        with patch("mcp_vicinity.tools._get_cursor", return_value=cursor), \
             patch("app.services.listing_queries.get_listing_detail",
                   return_value=make_qr(data=[])):
            result = json.loads(get_listing("nonexistent"))

        assert result["total_count"] == 0

    def test_connection_error_returns_json(self):
        with patch("mcp_vicinity.tools._get_cursor",
                   side_effect=Exception("timeout")):
            result = json.loads(get_listing("x"))

        assert result["success"] is False


# =====================================================================
# get_safety
# =====================================================================

class TestGetSafety:

    def test_returns_crimes(self):
        cursor = _mock_cursor()
        with patch("mcp_vicinity.tools._get_cursor", return_value=cursor), \
             patch("app.services.crime_queries.crimes_near_point",
                   return_value=make_qr(data=[SAMPLE_CRIME])):
            result = json.loads(get_safety(42.35, -71.06))

        assert result["success"] is True
        assert result["data"][0]["severity"] == "property"

    def test_passes_all_params(self):
        cursor = _mock_cursor()
        with patch("mcp_vicinity.tools._get_cursor", return_value=cursor), \
             patch("app.services.crime_queries.crimes_near_point",
                   return_value=make_qr()) as mock:
            get_safety(42.35, -71.06, radius_m=1000, window_days=60, severity="violent")

        kw = mock.call_args[1]
        assert kw["radius_m"] == 1000
        assert kw["window_days"] == 60
        assert kw["severity"] == "violent"

    def test_connection_error_returns_json(self):
        with patch("mcp_vicinity.tools._get_cursor",
                   side_effect=Exception("db down")):
            result = json.loads(get_safety(42.35, -71.06))

        assert result["success"] is False

    def test_cursor_closed(self):
        cursor = _mock_cursor()
        with patch("mcp_vicinity.tools._get_cursor", return_value=cursor), \
             patch("app.services.crime_queries.crimes_near_point",
                   return_value=make_qr()):
            get_safety(42.35, -71.06)

        cursor.close.assert_called_once()


# =====================================================================
# get_neighborhood
# =====================================================================

class TestGetNeighborhood:

    def test_returns_stats(self):
        cursor = _mock_cursor()
        with patch("mcp_vicinity.tools._get_cursor", return_value=cursor), \
             patch("app.services.crime_queries.neighborhood_stats",
                   return_value=make_qr(data=[SAMPLE_NEIGHBORHOOD])):
            result = json.loads(get_neighborhood("Allston"))

        assert result["success"] is True
        assert result["data"][0]["total"] == 120

    def test_passes_params(self):
        cursor = _mock_cursor()
        with patch("mcp_vicinity.tools._get_cursor", return_value=cursor), \
             patch("app.services.crime_queries.neighborhood_stats",
                   return_value=make_qr()) as mock:
            get_neighborhood("Mission Hill", window_days=90)

        assert mock.call_args[0][1] == "Mission Hill"
        assert mock.call_args[1]["window_days"] == 90

    def test_connection_error_returns_json(self):
        with patch("mcp_vicinity.tools._get_cursor",
                   side_effect=Exception("timeout")):
            result = json.loads(get_neighborhood("X"))

        assert result["success"] is False


# =====================================================================
# get_amenities
# =====================================================================

class TestGetAmenities:

    def test_returns_amenities(self):
        cursor = _mock_cursor()
        with patch("mcp_vicinity.tools._get_cursor", return_value=cursor), \
             patch("app.services.amenity_lookup.search_stored_amenities",
                   return_value=make_qr(data=[SAMPLE_AMENITY])):
            result = json.loads(get_amenities(42.35, -71.06))

        assert result["success"] is True
        assert result["data"][0]["name"] == "CVS Pharmacy"

    def test_passes_all_params(self):
        cursor = _mock_cursor()
        with patch("mcp_vicinity.tools._get_cursor", return_value=cursor), \
             patch("app.services.amenity_lookup.search_stored_amenities",
                   return_value=make_qr()) as mock:
            get_amenities(42.35, -71.06, subcategory="pharmacy",
                          name_contains="CVS", radius_m=1000, limit=5)

        kw = mock.call_args[1]
        assert kw["subcategory"] == "pharmacy"
        assert kw["name_contains"] == "CVS"
        assert kw["radius_m"] == 1000
        assert kw["limit"] == 5

    def test_connection_error_returns_json(self):
        with patch("mcp_vicinity.tools._get_cursor",
                   side_effect=Exception("db down")):
            result = json.loads(get_amenities(42.35, -71.06))

        assert result["success"] is False

    def test_cursor_closed(self):
        cursor = _mock_cursor()
        with patch("mcp_vicinity.tools._get_cursor", return_value=cursor), \
             patch("app.services.amenity_lookup.search_stored_amenities",
                   return_value=make_qr()):
            get_amenities(42.35, -71.06)

        cursor.close.assert_called_once()