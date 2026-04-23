"""Tests for app.services.url_health."""

import pytest
from unittest.mock import patch, MagicMock
from app.services.url_health import validate_url, flag_url, validate_flagged_urls


class TestValidateUrl:

    @patch("app.services.url_health.httpx.head")
    def test_alive(self, mock_head):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_head.return_value = mock_resp

        result = validate_url("https://example.com")
        assert result["alive"]
        assert result["status_code"] == 200

    @patch("app.services.url_health.httpx.head")
    def test_dead_404(self, mock_head):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_head.return_value = mock_resp

        result = validate_url("https://example.com/gone")
        assert not result["alive"]
        assert result["status_code"] == 404

    @patch("app.services.url_health.httpx.head")
    def test_redirect_alive(self, mock_head):
        mock_resp = MagicMock()
        mock_resp.status_code = 301
        mock_head.return_value = mock_resp

        result = validate_url("https://old.com")
        assert result["alive"]

    @patch("app.services.url_health.httpx.head")
    def test_timeout(self, mock_head):
        import httpx
        mock_head.side_effect = httpx.TimeoutException("timeout")

        result = validate_url("https://slow.com")
        assert not result["alive"]
        assert result["error"] == "timeout"


class TestFlagUrl:

    @patch("app.services.url_health.validate_url")
    def test_flag_alive_url(self, mock_validate, cursor):
        mock_validate.return_value = {"alive": True, "status_code": 200}
        # One dummy row so rowcount=1 after UPDATE
        cursor.set_results(["x"], [("x",)])

        result = flag_url(cursor, "RAW.LISTINGS", "abc", "https://r.com/abc")
        assert result.success
        assert result.data[0]["new_status"] == "active"

    @patch("app.services.url_health.validate_url")
    def test_flag_dead_url(self, mock_validate, cursor):
        mock_validate.return_value = {"alive": False, "status_code": 404}
        cursor.set_results(["x"], [("x",)])

        result = flag_url(cursor, "RAW.LISTINGS", "abc", "https://r.com/gone")
        assert result.success
        assert result.data[0]["new_status"] == "confirmed_dead"

    def test_invalid_table(self, cursor):
        result = flag_url(cursor, "RAW.FAKE_TABLE", "abc", "https://x.com")
        assert not result.success
        assert "not configured" in result.error.lower()

    def test_record_not_found(self, cursor):
        with patch("app.services.url_health.validate_url") as mock_v:
            mock_v.return_value = {"alive": True, "status_code": 200}
            cursor.set_results([], [])
            cursor.rowcount = 0
            result = flag_url(cursor, "RAW.LISTINGS", "nonexistent", "https://x.com")
            assert not result.success
            assert "not found" in result.error.lower()


class TestValidateFlaggedUrls:

    @patch("app.services.url_health.validate_url")
    def test_batch(self, mock_validate, cursor):
        mock_validate.return_value = {"alive": False, "status_code": 404}
        cursor.set_results(
            ["listing_id", "source_url"],
            [("abc", "https://r.com/abc"), ("def", "https://r.com/def")],
        )

        result = validate_flagged_urls(cursor)
        assert result.success
        assert result.total_count == 4  # 2 URLs x 2 target tables in config
        assert all(not r["alive"] for r in result.data)

    def test_no_flagged(self, cursor):
        cursor.set_results(["listing_id", "source_url"], [])
        result = validate_flagged_urls(cursor)
        assert result.success
        assert result.total_count == 0