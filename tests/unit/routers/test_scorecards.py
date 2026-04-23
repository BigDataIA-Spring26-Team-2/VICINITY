"""Tests for app.routers.scorecards — /scorecards/route/{route_id}."""

import pytest
from unittest.mock import patch

from app.services.listing_queries import QueryResult
from tests.unit.routers.conftest import TEST_USER_ID

_P = "app.services.listing_queries"


def _qr(success=True, data=None, total=None, error=None):
    data = data or []
    return QueryResult(
        success=success, query_type="test",
        data=data, total_count=total if total is not None else len(data),
        error=error,
    )


ROUTE_SCORE_ROW = {
    "route_id": "r-001", "listing_id": "lst-001",
    "score_date": "2026-04-20", "crime_count": 5,
    "violent_count": 1, "shooting_count": 0,
    "crimes_at_dep_hour": 2, "citizen_incidents": 3,
    "citizen_nighttime": 1,
}


class TestRouteScorecard:

    def test_returns_time_series(self, client, cursor, auth_headers):
        with patch(f"{_P}.route_scorecard_history", return_value=_qr(data=[ROUTE_SCORE_ROW])):
            resp = client.get("/scorecards/route/r-001", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["data"][0]["crime_count"] == 5

    def test_with_days(self, client, cursor, auth_headers):
        with patch(f"{_P}.route_scorecard_history", return_value=_qr(data=[])) as mock:
            resp = client.get("/scorecards/route/r-001?days=30", headers=auth_headers)
        assert resp.status_code == 200
        assert mock.call_args[1]["days"] == 30

    def test_with_date_range(self, client, cursor, auth_headers):
        with patch(f"{_P}.route_scorecard_history", return_value=_qr(data=[])) as mock:
            resp = client.get(
                "/scorecards/route/r-001?start_date=2026-04-01&end_date=2026-04-20",
                headers=auth_headers,
            )
        assert resp.status_code == 200
        assert mock.call_args[1]["start_date"] == "2026-04-01"

    def test_empty_results(self, client, cursor, auth_headers):
        with patch(f"{_P}.route_scorecard_history", return_value=_qr(data=[])):
            resp = client.get("/scorecards/route/r-001", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["total_count"] == 0

    def test_requires_auth(self, client, cursor):
        resp = client.get("/scorecards/route/r-001")
        assert resp.status_code == 401

    def test_service_error(self, client, cursor, auth_headers):
        with patch(f"{_P}.route_scorecard_history", return_value=_qr(success=False, error="DB")):
            resp = client.get("/scorecards/route/r-001", headers=auth_headers)
        assert resp.status_code == 500