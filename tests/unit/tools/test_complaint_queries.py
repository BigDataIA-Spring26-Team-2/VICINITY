"""Tests for app.services.complaint_queries."""

import pytest
from app.services.complaint_queries import (
    complaints_near_point, complaint_summary,
)


class TestComplaintsNearPoint:

    COLS = [
        "case_enquiry_id", "open_dt", "case_title", "type", "category",
        "street", "neighborhood", "zip_code", "lat", "lon",
        "case_status", "distance_m",
    ]

    def test_basic(self, cursor):
        row = ("CE1", "2026-04-10", "Rodent Activity", "Pest Control",
               "pest", "Tremont St", "South End", "02118",
               42.345, -71.068, "Open", 120)
        cursor.set_results(self.COLS, [row])
        result = complaints_near_point(cursor, lat=42.35, lon=-71.06)
        assert result.success
        assert result.data[0]["category"] == "pest"
        assert "ST_DISTANCE" in cursor.last_sql

    def test_category_filter(self, cursor):
        cursor.set_results(self.COLS, [])
        result = complaints_near_point(cursor, lat=42.35, lon=-71.06,
                                       category="noise")
        assert result.success
        assert "c.category = %s" in cursor.last_sql

    def test_invalid_category(self, cursor):
        result = complaints_near_point(cursor, lat=42.35, lon=-71.06,
                                       category="fake_category")
        assert not result.success
        assert "Invalid category" in result.error

    def test_radius_clamped(self, cursor):
        cursor.set_results(self.COLS, [])
        result = complaints_near_point(cursor, lat=42.35, lon=-71.06,
                                       radius_m=50000)
        assert result.success
        assert "50000" not in cursor.last_sql

    def test_db_error(self, cursor):
        cursor.set_error(Exception("connection reset"))
        result = complaints_near_point(cursor, lat=42.35, lon=-71.06)
        assert not result.success


class TestComplaintSummary:

    COLS = ["category", "count", "earliest", "latest"]

    def test_basic(self, cursor):
        rows = [
            ("pest", 15, "2026-03-20", "2026-04-18"),
            ("noise", 8, "2026-04-01", "2026-04-17"),
            ("heat", 3, "2026-04-10", "2026-04-15"),
        ]
        cursor.set_results(self.COLS, rows)
        result = complaint_summary(cursor, lat=42.35, lon=-71.06)
        assert result.success
        assert result.total_count == 3
        assert result.data[0]["category"] == "pest"

    def test_empty(self, cursor):
        cursor.set_results(self.COLS, [])
        result = complaint_summary(cursor, lat=42.35, lon=-71.06)
        assert result.success
        assert result.total_count == 0
        assert "GROUP BY" in cursor.last_sql