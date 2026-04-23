"""Tests for app.routers.safety — /safety/crimes, /hourly, /neighborhood, /complaints, /summary."""

import pytest
from unittest.mock import patch

from app.services.listing_queries import QueryResult


def _qr(success=True, data=None, total=None, error=None):
    data = data or []
    return QueryResult(
        success=success, query_type="test",
        data=data, total_count=total if total is not None else len(data),
        error=error,
    )


_C = "app.services.crime_queries"
_CO = "app.services.complaint_queries"


class TestCrimes:

    def test_returns_crimes(self, client, cursor):
        row = {"incident_id": "inc-001", "severity": "property", "distance_m": 120}
        with patch(f"{_C}.crimes_near_point", return_value=_qr(data=[row])):
            resp = client.get("/safety/crimes?lat=42.35&lon=-71.06")
        assert resp.status_code == 200
        assert resp.json()["data"][0]["severity"] == "property"

    def test_all_params(self, client, cursor):
        with patch(f"{_C}.crimes_near_point", return_value=_qr(data=[])) as mock:
            resp = client.get(
                "/safety/crimes?lat=42.35&lon=-71.06"
                "&radius_m=1000&window_days=60&severity=violent"
                "&hour_min=22&hour_max=4&shooting_only=true&limit=500"
            )
        assert resp.status_code == 200
        kw = mock.call_args[1]
        assert kw["radius_m"] == 1000
        assert kw["severity"] == "violent"
        assert kw["hour_min"] == 22
        assert kw["hour_max"] == 4
        assert kw["shooting_only"] is True

    def test_missing_lat_lon(self, client, cursor):
        resp = client.get("/safety/crimes")
        assert resp.status_code == 422

    def test_service_error(self, client, cursor):
        with patch(f"{_C}.crimes_near_point", return_value=_qr(success=False, error="DB")):
            resp = client.get("/safety/crimes?lat=42.35&lon=-71.06")
        assert resp.status_code == 500

    def test_no_auth_required(self, client, cursor):
        with patch(f"{_C}.crimes_near_point", return_value=_qr(data=[])):
            resp = client.get("/safety/crimes?lat=42.35&lon=-71.06")
        assert resp.status_code == 200


class TestCrimesHourly:

    def test_returns_buckets(self, client, cursor):
        buckets = [{"hour": 0, "count": 5, "violent": 1},
                   {"hour": 14, "count": 12, "violent": 3}]
        with patch(f"{_C}.hourly_distribution", return_value=_qr(data=buckets)):
            resp = client.get("/safety/crimes/hourly?lat=42.35&lon=-71.06")
        assert resp.status_code == 200
        assert resp.json()["data"][1]["count"] == 12

    def test_with_params(self, client, cursor):
        with patch(f"{_C}.hourly_distribution", return_value=_qr(data=[])) as mock:
            resp = client.get(
                "/safety/crimes/hourly?lat=42.35&lon=-71.06&radius_m=500&months_back=12"
            )
        assert resp.status_code == 200
        assert mock.call_args[1]["radius_m"] == 500
        assert mock.call_args[1]["months_back"] == 12

    def test_missing_lat_lon(self, client, cursor):
        resp = client.get("/safety/crimes/hourly")
        assert resp.status_code == 422

    def test_service_error(self, client, cursor):
        with patch(f"{_C}.hourly_distribution", return_value=_qr(success=False, error="timeout")):
            resp = client.get("/safety/crimes/hourly?lat=42.35&lon=-71.06")
        assert resp.status_code == 500


class TestNeighborhood:

    def test_returns_stats(self, client, cursor):
        row = {"district": "B2", "total": 150, "violent": 30}
        with patch(f"{_C}.neighborhood_stats", return_value=_qr(data=[row])):
            resp = client.get("/safety/neighborhood?neighborhood=Allston")
        assert resp.status_code == 200
        assert resp.json()["data"][0]["total"] == 150

    def test_missing_neighborhood(self, client, cursor):
        resp = client.get("/safety/neighborhood")
        assert resp.status_code == 422

    def test_with_window(self, client, cursor):
        with patch(f"{_C}.neighborhood_stats", return_value=_qr(data=[])) as mock:
            resp = client.get("/safety/neighborhood?neighborhood=Allston&window_days=90")
        assert resp.status_code == 200
        assert mock.call_args[1]["window_days"] == 90

    def test_service_error(self, client, cursor):
        with patch(f"{_C}.neighborhood_stats", return_value=_qr(success=False, error="err")):
            resp = client.get("/safety/neighborhood?neighborhood=X")
        assert resp.status_code == 500


class TestComplaints:

    def test_returns_complaints(self, client, cursor):
        row = {"case_enquiry_id": "101", "category": "noise", "distance_m": 200}
        with patch(f"{_CO}.complaints_near_point", return_value=_qr(data=[row])):
            resp = client.get("/safety/complaints?lat=42.35&lon=-71.06")
        assert resp.status_code == 200
        assert resp.json()["data"][0]["category"] == "noise"

    def test_category_filter(self, client, cursor):
        with patch(f"{_CO}.complaints_near_point", return_value=_qr(data=[])) as mock:
            resp = client.get("/safety/complaints?lat=42.35&lon=-71.06&category=pest")
        assert resp.status_code == 200
        assert mock.call_args[1]["category"] == "pest"

    def test_missing_lat_lon(self, client, cursor):
        resp = client.get("/safety/complaints")
        assert resp.status_code == 422

    def test_service_error(self, client, cursor):
        with patch(f"{_CO}.complaints_near_point", return_value=_qr(success=False, error="DB")):
            resp = client.get("/safety/complaints?lat=42.35&lon=-71.06")
        assert resp.status_code == 500


class TestComplaintSummary:

    def test_returns_summary(self, client, cursor):
        rows = [{"category": "noise", "count": 42, "earliest": "2026-03-01", "latest": "2026-04-15"}]
        with patch(f"{_CO}.complaint_summary", return_value=_qr(data=rows)):
            resp = client.get("/safety/complaints/summary?lat=42.35&lon=-71.06")
        assert resp.status_code == 200
        assert resp.json()["data"][0]["count"] == 42

    def test_with_params(self, client, cursor):
        with patch(f"{_CO}.complaint_summary", return_value=_qr(data=[])) as mock:
            resp = client.get(
                "/safety/complaints/summary?lat=42.35&lon=-71.06&radius_m=2000&window_days=90"
            )
        assert resp.status_code == 200
        assert mock.call_args[1]["radius_m"] == 2000

    def test_missing_lat_lon(self, client, cursor):
        resp = client.get("/safety/complaints/summary")
        assert resp.status_code == 422

    def test_service_error(self, client, cursor):
        with patch(f"{_CO}.complaint_summary", return_value=_qr(success=False, error="timeout")):
            resp = client.get("/safety/complaints/summary?lat=42.35&lon=-71.06")
        assert resp.status_code == 500