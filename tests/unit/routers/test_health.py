"""Tests for app.routers.health — /healthz and /ping."""

import pytest
from unittest.mock import patch


class TestPing:

    def test_returns_ok(self, client):
        resp = client.get("/ping")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_no_auth_required(self, client):
        resp = client.get("/ping")
        assert resp.status_code == 200


class TestHealthz:

    @patch("app.routers.health.check_health")
    def test_returns_health_report(self, mock_check, client):
        mock_check.return_value = {
            "status": "ok",
            "response_ms": 42,
            "components": {
                "snowflake": {"status": "ok", "ms": 20},
                "redis": {"status": "ok", "ms": 2},
                "pinecone": {"status": "ok", "ms": 20},
            },
        }
        resp = client.get("/healthz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "components" in data
        assert "snowflake" in data["components"]

    @patch("app.routers.health.check_health")
    def test_degraded_still_200(self, mock_check, client):
        mock_check.return_value = {
            "status": "degraded",
            "response_ms": 100,
            "components": {
                "snowflake": {"status": "ok", "ms": 50},
                "redis": {"status": "down", "ms": 0},
                "pinecone": {"status": "ok", "ms": 50},
            },
        }
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "degraded"

    @patch("app.routers.health.check_health")
    def test_down_still_200(self, mock_check, client):
        mock_check.return_value = {
            "status": "down",
            "response_ms": 0,
            "components": {
                "snowflake": {"status": "down", "ms": 0, "error": "timeout"},
                "redis": {"status": "down", "ms": 0},
                "pinecone": {"status": "down", "ms": 0},
            },
        }
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "down"

    @patch("app.routers.health.check_health")
    def test_no_auth_required(self, mock_check, client):
        mock_check.return_value = {"status": "ok", "response_ms": 1, "components": {}}
        resp = client.get("/healthz")
        assert resp.status_code == 200