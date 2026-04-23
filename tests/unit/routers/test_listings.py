"""Tests for app.routers.listings — /listings/search, /compare, /{id}, /{id}/scorecard.

Patch target: the router imports functions at module level:
    from app.services.listing_queries import search_listings, get_listing_detail, ...
So we patch where the name is bound: app.routers.listings.search_listings
"""

import pytest
from unittest.mock import patch

from app.services.listing_queries import QueryResult
from tests.unit.routers.conftest import TEST_USER_ID

# Patch at the router module — that's where the imported names live
_R = "app.routers.listings"


def _qr(success=True, data=None, total=None, error=None, warnings=None):
    data = data or []
    return QueryResult(
        success=success, query_type="test",
        data=data, total_count=total if total is not None else len(data),
        warnings=warnings or [], error=error,
    )


LISTING_ROW = {
    "listing_id": "lst-001", "source": "realtor",
    "source_url": "https://realtor.com/lst-001",
    "price": 2500, "beds": 2, "baths": 1, "sqft": 800,
    "street": "123 Main St", "city": "Boston", "neighborhood": "Allston",
    "lat": 42.35, "lon": -71.13,
    "safety_score": 75, "livability_score": 68,
    "primary_photo_url": "https://photos.com/1.jpg",
}


class TestListingsSearch:

    def test_returns_results(self, client, cursor):
        with patch(f"{_R}.search_listings", return_value=_qr(data=[LISTING_ROW])):
            resp = client.get("/listings/search?max_price=3000&beds_min=2")
        assert resp.status_code == 200
        assert resp.json()["total_count"] == 1
        assert resp.json()["data"][0]["listing_id"] == "lst-001"

    def test_empty_results(self, client, cursor):
        with patch(f"{_R}.search_listings", return_value=_qr(data=[])):
            resp = client.get("/listings/search?neighborhood=Nowhere")
        assert resp.status_code == 200
        assert resp.json()["total_count"] == 0

    def test_no_filters_required(self, client, cursor):
        with patch(f"{_R}.search_listings", return_value=_qr(data=[])):
            resp = client.get("/listings/search")
        assert resp.status_code == 200

    def test_all_filters(self, client, cursor):
        with patch(f"{_R}.search_listings", return_value=_qr(data=[])) as mock:
            resp = client.get(
                "/listings/search?"
                "min_price=1000&max_price=3000&beds_min=1&beds_max=3"
                "&baths_min=1&city=Boston&neighborhood=Allston"
                "&zip_code=02134&min_sqft=500&min_safety_score=50"
                "&min_livability_score=40&has_photo=true"
                "&sort_by=price&sort_order=asc&limit=10&offset=5"
            )
        assert resp.status_code == 200
        kw = mock.call_args[1]
        assert kw["min_price"] == 1000
        assert kw["max_price"] == 3000
        assert kw["neighborhood"] == "Allston"
        assert kw["sort_by"] == "price"
        assert kw["offset"] == 5

    def test_service_error_returns_500(self, client, cursor):
        with patch(f"{_R}.search_listings", return_value=_qr(success=False, error="DB down")):
            resp = client.get("/listings/search")
        assert resp.status_code == 500

    def test_no_auth_required(self, client, cursor):
        with patch(f"{_R}.search_listings", return_value=_qr(data=[])):
            resp = client.get("/listings/search")
        assert resp.status_code == 200


class TestListingDetail:

    def test_returns_detail(self, client, cursor):
        with patch(f"{_R}.get_listing_detail", return_value=_qr(data=[LISTING_ROW])):
            resp = client.get("/listings/lst-001")
        assert resp.status_code == 200
        assert resp.json()["data"][0]["street"] == "123 Main St"

    def test_not_found(self, client, cursor):
        with patch(f"{_R}.get_listing_detail", return_value=_qr(data=[])):
            resp = client.get("/listings/lst-nonexistent")
        assert resp.status_code == 404

    def test_service_error(self, client, cursor):
        with patch(f"{_R}.get_listing_detail", return_value=_qr(success=False, error="timeout")):
            resp = client.get("/listings/lst-001")
        assert resp.status_code == 500

    def test_no_auth_required(self, client, cursor):
        with patch(f"{_R}.get_listing_detail", return_value=_qr(data=[LISTING_ROW])):
            resp = client.get("/listings/lst-001")
        assert resp.status_code == 200


class TestListingsCompare:

    def test_compare_two(self, client, cursor):
        row2 = {**LISTING_ROW, "listing_id": "lst-002", "price": 2800}
        with patch(f"{_R}.compare_listings", return_value=_qr(data=[LISTING_ROW, row2])):
            resp = client.get("/listings/compare?ids=lst-001,lst-002")
        assert resp.status_code == 200
        assert resp.json()["total_count"] == 2

    def test_too_few_ids(self, client, cursor):
        resp = client.get("/listings/compare?ids=lst-001")
        assert resp.status_code == 400

    def test_too_many_ids(self, client, cursor):
        ids = ",".join(f"lst-{i:03d}" for i in range(12))
        resp = client.get(f"/listings/compare?ids={ids}")
        assert resp.status_code == 400

    def test_missing_ids_param(self, client, cursor):
        resp = client.get("/listings/compare")
        assert resp.status_code == 422

    def test_service_error(self, client, cursor):
        with patch(f"{_R}.compare_listings", return_value=_qr(success=False, error="DB error")):
            resp = client.get("/listings/compare?ids=lst-001,lst-002")
        assert resp.status_code == 500


class TestListingScorecard:

    def test_returns_scorecard(self, client, cursor, auth_headers):
        row = {"listing_id": "lst-001", "score_date": "2026-04-20",
               "safety_score": 75, "crime_count": 12}
        with patch(f"{_R}.scorecard_history", return_value=_qr(data=[row])):
            resp = client.get("/listings/lst-001/scorecard", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["data"][0]["safety_score"] == 75

    def test_with_date_params(self, client, cursor, auth_headers):
        with patch(f"{_R}.scorecard_history", return_value=_qr(data=[])) as mock:
            resp = client.get(
                "/listings/lst-001/scorecard?start_date=2026-04-01&end_date=2026-04-20",
                headers=auth_headers,
            )
        assert resp.status_code == 200
        assert mock.call_args[1]["start_date"] == "2026-04-01"

    def test_with_days_param(self, client, cursor, auth_headers):
        with patch(f"{_R}.scorecard_history", return_value=_qr(data=[])) as mock:
            resp = client.get("/listings/lst-001/scorecard?days=7", headers=auth_headers)
        assert resp.status_code == 200
        assert mock.call_args[1]["days"] == 7

    def test_requires_auth(self, client, cursor):
        resp = client.get("/listings/lst-001/scorecard")
        assert resp.status_code == 401

    def test_service_error(self, client, cursor, auth_headers):
        with patch(f"{_R}.scorecard_history", return_value=_qr(success=False, error="timeout")):
            resp = client.get("/listings/lst-001/scorecard", headers=auth_headers)
        assert resp.status_code == 500