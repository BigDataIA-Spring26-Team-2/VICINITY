"""Tests for app.services.amenity_lookup."""

import pytest
from unittest.mock import patch, MagicMock
from app.services.amenity_lookup import (
    search_stored_amenities, search_overpass_live,
)


class TestSearchStoredAmenities:

    COLS = [
        "osm_id", "name", "category", "subcategory", "lat", "lon",
        "address", "opening_hours", "website", "phone", "brand",
        "tags", "distance_m",
    ]

    def test_basic(self, cursor):
        row = (123456, "CVS Pharmacy", "amenity", "pharmacy",
               42.349, -71.062, "100 Tremont St", "8AM-10PM",
               "https://cvs.com", "617-555-0100", "CVS", None, 150)
        cursor.set_results(self.COLS, [row])
        result = search_stored_amenities(cursor, lat=42.35, lon=-71.06)
        assert result.success
        assert result.data[0]["name"] == "CVS Pharmacy"
        assert result.data[0]["distance_m"] == 150

    def test_subcategory_filter(self, cursor):
        cursor.set_results(self.COLS, [])
        result = search_stored_amenities(cursor, lat=42.35, lon=-71.06,
                                         subcategory="pharmacy")
        assert result.success
        assert "LOWER(a.subcategory) = LOWER(%s)" in cursor.last_sql

    def test_name_search(self, cursor):
        cursor.set_results(self.COLS, [])
        result = search_stored_amenities(cursor, lat=42.35, lon=-71.06,
                                         name_contains="starbucks")
        assert result.success
        assert "LOWER(a.name) LIKE %s" in cursor.last_sql

    def test_radius_clamped(self, cursor):
        cursor.set_results(self.COLS, [])
        result = search_stored_amenities(cursor, lat=42.35, lon=-71.06,
                                         radius_m=99999)
        assert result.success
        assert "99999" not in cursor.last_sql

    def test_db_error(self, cursor):
        cursor.set_error(Exception("timeout"))
        result = search_stored_amenities(cursor, lat=42.35, lon=-71.06)
        assert not result.success


class TestSearchOverpassLive:

    @patch("app.services.amenity_lookup.get_cache")
    @patch("app.services.amenity_lookup.httpx.post")
    def test_basic(self, mock_post, mock_cache):
        # Disable cache
        cache = MagicMock()
        cache.enabled = False
        mock_cache.return_value = cache

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "elements": [
                {
                    "type": "node", "id": 111,
                    "lat": 42.349, "lon": -71.062,
                    "tags": {
                        "name": "Seoul Kitchen",
                        "amenity": "restaurant",
                        "cuisine": "korean",
                        "addr:street": "Brighton Ave",
                    },
                },
            ]
        }
        mock_post.return_value = mock_resp

        result = search_overpass_live(
            lat=42.35, lon=-71.06,
            tags={"amenity": "restaurant", "cuisine": "korean"},
        )
        assert result.success
        assert result.total_count == 1
        assert result.data[0]["name"] == "Seoul Kitchen"
        assert result.data[0]["cuisine"] == "korean"

    def test_empty_tags(self):
        result = search_overpass_live(lat=42.35, lon=-71.06, tags={})
        assert not result.success
        assert "No tags" in result.error

    @patch("app.services.amenity_lookup._search_google_places")
    @patch("app.services.amenity_lookup.get_cache")
    @patch("app.services.amenity_lookup.httpx.post")
    def test_api_error(self, mock_post, mock_cache, mock_google):
        cache = MagicMock()
        cache.enabled = False
        mock_cache.return_value = cache

        mock_post.side_effect = Exception("Overpass 503")
        mock_google.return_value = None

        result = search_overpass_live(
            lat=42.35, lon=-71.06,
            tags={"amenity": "cafe"},
        )
        assert not result.success

    @patch("app.services.amenity_lookup.get_cache")
    @patch("app.services.amenity_lookup.httpx.post")
    def test_cache_hit(self, mock_post, mock_cache):
        cache = MagicMock()
        cache.enabled = True
        cache.get.return_value = [{"name": "Cached Cafe", "distance_m": 50}]
        mock_cache.return_value = cache

        result = search_overpass_live(
            lat=42.35, lon=-71.06,
            tags={"amenity": "cafe"},
        )
        assert result.success
        assert result.data[0]["name"] == "Cached Cafe"
        mock_post.assert_not_called()  # No HTTP call on cache hit