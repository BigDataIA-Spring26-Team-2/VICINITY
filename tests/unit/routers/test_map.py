"""Tests for app.routers.map — /map/listings, /transit, /amenities, /routes."""

import pytest
from unittest.mock import patch

from app.services.listing_queries import QueryResult
from tests.unit.routers.conftest import TEST_USER_ID


def _qr(success=True, data=None, total=None, error=None):
    data = data or []
    return QueryResult(
        success=success, query_type="test",
        data=data, total_count=total if total is not None else len(data),
        error=error,
    )


PIN_ROW = (
    "lst-001", 42.35, -71.13, 2500, 2, 1, 800,
    "123 Main St", "Allston", "Boston",
    "https://photos.com/1.jpg", "realtor", "https://realtor.com/1",
    75, 68,
)
PIN_COLS = [
    "listing_id", "lat", "lon", "price", "beds", "baths", "sqft",
    "street", "neighborhood", "city",
    "primary_photo_url", "source", "source_url",
    "safety_score", "livability_score",
]

TRANSIT_ROW = (
    "place-harsq", "Harvard Square", 42.3736, -71.1189,
    "Cambridge", 1, '["Red"]', '["Red Line"]', '[1]',
)
TRANSIT_COLS = [
    "stop_id", "stop_name", "lat", "lon",
    "municipality", "wheelchair_boarding",
    "route_ids", "route_names", "route_types",
]


class TestMapListings:

    def test_returns_pins(self, client, cursor):
        cursor.set_results(PIN_COLS, [PIN_ROW])
        resp = client.get("/map/listings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["total_count"] == 1
        assert data["data"][0]["lat"] == 42.35

    def test_with_bbox(self, client, cursor):
        cursor.set_results(PIN_COLS, [])
        resp = client.get(
            "/map/listings?min_lat=42.3&max_lat=42.4&min_lon=-71.2&max_lon=-71.0"
        )
        assert resp.status_code == 200
        assert "BETWEEN" in cursor.last_sql

    def test_with_filters(self, client, cursor):
        cursor.set_results(PIN_COLS, [])
        resp = client.get(
            "/map/listings?min_price=1000&max_price=3000&beds_min=2&min_safety_score=50"
        )
        assert resp.status_code == 200

    def test_limit_param(self, client, cursor):
        cursor.set_results(PIN_COLS, [])
        resp = client.get("/map/listings?limit=100")
        assert resp.status_code == 200
        # Limit is passed as a parameterized value, not inlined in SQL
        assert cursor.last_params[-1] == 100

    def test_empty_results(self, client, cursor):
        cursor.set_results(PIN_COLS, [])
        resp = client.get("/map/listings")
        assert resp.status_code == 200
        assert resp.json()["total_count"] == 0

    def test_db_error(self, client, cursor):
        cursor.set_error(Exception("connection lost"))
        resp = client.get("/map/listings")
        assert resp.status_code == 500

    def test_no_auth_required(self, client, cursor):
        cursor.set_results(PIN_COLS, [])
        resp = client.get("/map/listings")
        assert resp.status_code == 200


class TestMapTransit:

    def test_returns_stops(self, client, cursor):
        cursor.set_results(TRANSIT_COLS, [TRANSIT_ROW])
        resp = client.get("/map/transit")
        assert resp.status_code == 200
        assert resp.json()["total_count"] == 1
        assert resp.json()["data"][0]["stop_name"] == "Harvard Square"

    def test_bbox_filter(self, client, cursor):
        cursor.set_results(TRANSIT_COLS, [])
        resp = client.get(
            "/map/transit?min_lat=42.3&max_lat=42.4&min_lon=-71.2&max_lon=-71.0"
        )
        assert resp.status_code == 200
        assert "BETWEEN" in cursor.last_sql

    def test_route_type_filter(self, client, cursor):
        cursor.set_results(TRANSIT_COLS, [])
        resp = client.get("/map/transit?route_type=1")
        assert resp.status_code == 200
        assert "ARRAY_CONTAINS" in cursor.last_sql

    def test_empty_results(self, client, cursor):
        cursor.set_results(TRANSIT_COLS, [])
        resp = client.get("/map/transit")
        assert resp.status_code == 200
        assert resp.json()["total_count"] == 0

    def test_db_error(self, client, cursor):
        cursor.set_error(Exception("connection lost"))
        resp = client.get("/map/transit")
        assert resp.status_code == 500


class TestMapAmenities:

    def test_returns_amenities(self, client, cursor):
        with patch("app.services.amenity_lookup.search_stored_amenities",
                   return_value=_qr(data=[{"name": "CVS", "distance_m": 200}])):
            resp = client.get("/map/amenities?lat=42.35&lon=-71.06")
        assert resp.status_code == 200
        assert resp.json()["data"][0]["name"] == "CVS"

    def test_with_subcategory(self, client, cursor):
        with patch("app.services.amenity_lookup.search_stored_amenities",
                   return_value=_qr(data=[])) as mock:
            resp = client.get(
                "/map/amenities?lat=42.35&lon=-71.06&subcategory=pharmacy&radius_m=1000"
            )
        assert resp.status_code == 200
        kw = mock.call_args[1]
        assert kw["subcategory"] == "pharmacy"
        assert kw["radius_m"] == 1000

    def test_missing_lat_lon(self, client, cursor):
        resp = client.get("/map/amenities")
        assert resp.status_code == 422

    def test_service_error(self, client, cursor):
        with patch("app.services.amenity_lookup.search_stored_amenities",
                   return_value=_qr(success=False, error="disabled")):
            resp = client.get("/map/amenities?lat=42.35&lon=-71.06")
        assert resp.status_code == 500


class TestMapRoutes:

    def test_returns_routes(self, client, cursor, auth_headers):
        with patch("app.services.listing_queries.get_configured_routes",
                   return_value=_qr(data=[{
                       "route_id": "r-001",
                       "waypoints": [{"lat": 42.35, "lon": -71.06}],
                       "duration_min": 25.0,
                   }])):
            resp = client.get("/map/routes", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["total_count"] == 1

    def test_filter_by_listing(self, client, cursor, auth_headers):
        with patch("app.services.listing_queries.get_configured_routes",
                   return_value=_qr(data=[])) as mock:
            resp = client.get("/map/routes?listing_id=lst-001", headers=auth_headers)
        assert resp.status_code == 200
        assert mock.call_args[1]["listing_id"] == "lst-001"

    def test_requires_auth(self, client, cursor):
        resp = client.get("/map/routes")
        assert resp.status_code == 401

    def test_service_error(self, client, cursor, auth_headers):
        with patch("app.services.listing_queries.get_configured_routes",
                   return_value=_qr(success=False, error="DB error")):
            resp = client.get("/map/routes", headers=auth_headers)
        assert resp.status_code == 500