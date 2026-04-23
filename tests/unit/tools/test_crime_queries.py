"""Tests for app.services.crime_queries."""

import pytest
from app.services.crime_queries import (
    crimes_near_point, crimes_near_corridor,
    hourly_distribution, neighborhood_stats,
)


class TestCrimesNearPoint:

    COLS = [
        "incident_id", "offense_description", "severity",
        "occurred_on_date", "hour", "day_of_week",
        "street", "district", "lat", "lon", "shooting", "distance_m",
    ]

    def test_basic_query(self, cursor):
        row = ("INC1", "ASSAULT", "violent", "2026-04-15", 22, "Friday",
               "Tremont St", "A1", 42.35, -71.06, False, 150)
        cursor.set_results(self.COLS, [row])
        result = crimes_near_point(cursor, lat=42.35, lon=-71.06)
        assert result.success
        assert result.data[0]["incident_id"] == "INC1"
        assert "ST_DISTANCE" in cursor.last_sql

    def test_severity_filter(self, cursor):
        cursor.set_results(self.COLS, [])
        result = crimes_near_point(cursor, lat=42.35, lon=-71.06,
                                   severity="violent")
        assert result.success
        assert "c.severity = %s" in cursor.last_sql

    def test_hour_filter_normal(self, cursor):
        cursor.set_results(self.COLS, [])
        result = crimes_near_point(cursor, lat=42.35, lon=-71.06,
                                   hour_min=8, hour_max=12)
        assert result.success
        assert "BETWEEN" in cursor.last_sql

    def test_hour_filter_midnight_wrap(self, cursor):
        cursor.set_results(self.COLS, [])
        result = crimes_near_point(cursor, lat=42.35, lon=-71.06,
                                   hour_min=22, hour_max=4)
        assert result.success
        assert ">=" in cursor.last_sql and "OR" in cursor.last_sql

    def test_shooting_only(self, cursor):
        cursor.set_results(self.COLS, [])
        result = crimes_near_point(cursor, lat=42.35, lon=-71.06,
                                   shooting_only=True)
        assert result.success
        assert "shooting = TRUE" in cursor.last_sql

    def test_radius_clamped(self, cursor):
        cursor.set_results(self.COLS, [])
        result = crimes_near_point(cursor, lat=42.35, lon=-71.06,
                                   radius_m=99999)
        assert result.success
        # Should be clamped to max_radius_m (3000)
        assert "99999" not in cursor.last_sql

    def test_db_error(self, cursor):
        cursor.set_error(Exception("query timeout"))
        result = crimes_near_point(cursor, lat=42.35, lon=-71.06)
        assert not result.success


class TestCrimesNearCorridor:

    COLS = [
        "incident_id", "offense_description", "severity",
        "occurred_on_date", "hour", "day_of_week",
        "street", "district", "lat", "lon", "shooting",
    ]

    WAYPOINTS = [
        {"lat": 42.36, "lon": -71.09},
        {"lat": 42.355, "lon": -71.07},
        {"lat": 42.35, "lon": -71.06},
    ]

    def test_basic_corridor(self, cursor):
        row = ("INC1", "ROBBERY", "violent", "2026-04-15", 23,
               "Saturday", "Brighton Ave", "D14", 42.355, -71.075, False)
        cursor.set_results(self.COLS, [row])
        result = crimes_near_corridor(cursor, self.WAYPOINTS)
        assert result.success
        assert "VALUES" in cursor.last_sql  # waypoint CTE

    def test_empty_waypoints(self, cursor):
        result = crimes_near_corridor(cursor, [])
        assert not result.success

    def test_severity_filter(self, cursor):
        cursor.set_results(self.COLS, [])
        result = crimes_near_corridor(cursor, self.WAYPOINTS,
                                      severity="violent")
        assert result.success
        assert "c.severity = %s" in cursor.last_sql

    def test_db_error(self, cursor):
        cursor.set_error(Exception("timeout"))
        result = crimes_near_corridor(cursor, self.WAYPOINTS)
        assert not result.success


class TestHourlyDistribution:

    COLS = ["hour", "count", "violent"]

    def test_basic(self, cursor):
        rows = [(h, 10, 2) for h in range(24)]
        cursor.set_results(self.COLS, rows)
        result = hourly_distribution(cursor, lat=42.35, lon=-71.06)
        assert result.success
        assert result.total_count == 24
        assert result.data[0]["hour"] == 0

    def test_empty(self, cursor):
        cursor.set_results(self.COLS, [])
        result = hourly_distribution(cursor, lat=42.35, lon=-71.06)
        assert result.success
        assert result.total_count == 0


class TestNeighborhoodStats:

    COLS = [
        "district", "total", "violent", "property",
        "shootings", "streets_affected", "most_common_offense",
    ]

    def test_basic(self, cursor):
        row = ("D14", 150, 30, 80, 5, 25, "LARCENY")
        cursor.set_results(self.COLS, [row])
        result = neighborhood_stats(cursor, "Allston")
        assert result.success
        assert result.data[0]["district"] == "D14"
        assert "LOWER(l.neighborhood) = LOWER(%s)" in cursor.last_sql

    def test_db_error(self, cursor):
        cursor.set_error(Exception("timeout"))
        result = neighborhood_stats(cursor, "Allston")
        assert not result.success