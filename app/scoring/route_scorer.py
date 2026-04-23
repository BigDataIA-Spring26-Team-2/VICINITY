"""Route corridor safety scorer.

Two-tier spatial join of crime/citizen incidents against route waypoints:

  Tier 1 (snapshot)  — recent window for current score.
  Tier 2 (series)    — all-time monthly counts for trend + YoY.

Used by:
  Agent (real-time)  — score_corridor(cursor, waypoints, include_series=False)
                       Returns immediate counts for conversation response.
  Agent (detailed)   — score_corridor(cursor, waypoints, include_series=True)
                       Returns full trend for report generation.
  Pipeline (daily)   — score_corridor(cursor, waypoints, include_series=True)
                       Writes to ROUTE_SCORECARD for watch period accumulation.

Config:
  Time windows  — config/scoring.yml (crime_window_days, citizen_window_hours)
  Corridor      — config/sources/routes.yml (buffer_m, hour_window)
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import structlog

from app.core.config_loader import load_source_config
from app.scoring.config import load_scoring_config
from app.scoring.scorer import compute_yoy_change

logger = structlog.get_logger()


# ── Data classes ─────────────────────────────────────────────

@dataclass
class WaypointScore:
    """Per-waypoint incident counts for map rendering."""
    lat: float
    lon: float
    crime_count: int = 0
    violent_count: int = 0
    shooting_count: int = 0
    citizen_count: int = 0
    citizen_nighttime: int = 0


@dataclass
class CorridorScore:
    """Full corridor safety result — recent snapshot + historical trend."""

    # Recent snapshot (ROUTE_SCORECARD columns)
    crime_count: int = 0
    violent_count: int = 0
    shooting_count: int = 0
    crimes_at_dep_hour: int = 0
    citizen_incidents: int = 0
    citizen_nighttime: int = 0
    waypoint_scores: list[WaypointScore] = field(default_factory=list)

    # Historical trend (from tier 2 query)
    monthly_series: dict[str, dict] = field(default_factory=dict)
    yoy_change: Optional[float] = None
    crime_trend: str = "stable"

    # Provenance (stored in ROUTE_SCORECARD.scoring_metadata)
    buffer_m: int = 0
    hour_window: int = 0
    departure_hour: int = 0
    crime_window_days: int = 0
    citizen_window_hours: int = 0
    waypoint_count: int = 0
    duration_ms: int = 0

    def to_scorecard_dict(self) -> dict:
        """ROUTE_SCORECARD row data."""
        return {
            "crime_count": self.crime_count,
            "violent_count": self.violent_count,
            "shooting_count": self.shooting_count,
            "crimes_at_dep_hour": self.crimes_at_dep_hour,
            "citizen_incidents": self.citizen_incidents,
            "citizen_nighttime": self.citizen_nighttime,
            "scoring_metadata": {
                "buffer_m": self.buffer_m,
                "hour_window": self.hour_window,
                "departure_hour": self.departure_hour,
                "crime_window_days": self.crime_window_days,
                "citizen_window_hours": self.citizen_window_hours,
                "waypoint_count": self.waypoint_count,
                "yoy_change": self.yoy_change,
                "crime_trend": self.crime_trend,
                "monthly_series": self.monthly_series,
                "duration_ms": self.duration_ms,
            },
        }

    def to_waypoint_variant(self) -> list[dict]:
        """CONFIGURED_ROUTES.waypoint_scores — only hot spots for map."""
        return [
            {
                "lat": ws.lat, "lon": ws.lon,
                "crimes": ws.crime_count, "violent": ws.violent_count,
                "citizen": ws.citizen_count,
            }
            for ws in self.waypoint_scores
            if ws.crime_count > 0 or ws.citizen_count > 0
        ]


# ── Public API ───────────────────────────────────────────────

def score_corridor(
    cursor,
    waypoints: list[dict],
    departure_hour: int = 8,
    buffer_m: Optional[int] = None,
    hour_window: Optional[int] = None,
    include_series: bool = True,
) -> CorridorScore:
    """Score a route corridor for safety.

    Args:
        cursor: Snowflake cursor.
        waypoints: [{"lat": float, "lon": float}, ...] from compute_route.
        departure_hour: 24h hour the user travels this route.
        buffer_m: Override corridor buffer (meters).
        hour_window: Override ± hour filter.
        include_series: Include all-time monthly series for trend analysis.
                        False for lightweight real-time scoring.
    """
    log = logger.bind(component="route_scorer")
    start = time.perf_counter()

    # Config
    corr = load_source_config("routes").get("corridor", {})
    scoring = load_scoring_config()
    buffer_m = buffer_m or corr.get("buffer_m", scoring.corridor_buffer_m)
    hour_window = hour_window or corr.get("hour_window", 2)

    if not waypoints:
        log.warning("no_waypoints")
        return CorridorScore()

    log.info("start",
             waypoints=len(waypoints), buffer_m=buffer_m,
             departure_hour=departure_hour, hour_window=hour_window,
             crime_days=scoring.crime_window_days,
             citizen_hours=scoring.citizen_window_hours,
             include_series=include_series)

    # Route bounding box
    lats = [w["lat"] for w in waypoints]
    lons = [w["lon"] for w in waypoints]
    bbox = _route_bbox(min(lats), max(lats), min(lons), max(lons), buffer_m)

    # Tier 1: Recent snapshot
    crime_by_wp = _query_crimes(
        cursor, waypoints, bbox, buffer_m,
        scoring.crime_window_days, departure_hour, hour_window,
    )
    citizen_by_wp = _query_citizen(
        cursor, waypoints, bbox, buffer_m,
        scoring.citizen_window_hours,
    )

    # Per-waypoint assembly
    wp_scores = []
    for wp in waypoints:
        key = _wp_key(wp)
        c = crime_by_wp.get(key, {})
        ci = citizen_by_wp.get(key, {})
        wp_scores.append(WaypointScore(
            lat=wp["lat"], lon=wp["lon"],
            crime_count=c.get("total", 0),
            violent_count=c.get("violent", 0),
            shooting_count=c.get("shooting", 0),
            citizen_count=ci.get("total", 0),
            citizen_nighttime=ci.get("nighttime", 0),
        ))

    # Tier 2: Monthly series
    monthly = {}
    yoy = None
    trend = "stable"

    if include_series:
        monthly = _query_monthly_series(cursor, waypoints, bbox, buffer_m)
        yoy = compute_yoy_change(monthly)
        if yoy is not None:
            trend = "declining" if yoy < -10 else "rising" if yoy > 10 else "stable"

    result = CorridorScore(
        crime_count=sum(ws.crime_count for ws in wp_scores),
        violent_count=sum(ws.violent_count for ws in wp_scores),
        shooting_count=sum(ws.shooting_count for ws in wp_scores),
        crimes_at_dep_hour=crime_by_wp.get("_at_dep_hour", 0),
        citizen_incidents=sum(ws.citizen_count for ws in wp_scores),
        citizen_nighttime=sum(ws.citizen_nighttime for ws in wp_scores),
        waypoint_scores=wp_scores,
        monthly_series=monthly,
        yoy_change=yoy,
        crime_trend=trend,
        buffer_m=buffer_m,
        hour_window=hour_window,
        departure_hour=departure_hour,
        crime_window_days=scoring.crime_window_days,
        citizen_window_hours=scoring.citizen_window_hours,
        waypoint_count=len(waypoints),
        duration_ms=int((time.perf_counter() - start) * 1000),
    )

    log.info("complete",
             crimes=result.crime_count, violent=result.violent_count,
             at_dep_hour=result.crimes_at_dep_hour,
             citizen=result.citizen_incidents,
             trend=result.crime_trend, yoy=result.yoy_change,
             series_months=len(monthly),
             hot_spots=len(result.to_waypoint_variant()),
             ms=result.duration_ms)

    return result


# ── Internal ─────────────────────────────────────────────────

def _route_bbox(min_lat, max_lat, min_lon, max_lon, buffer_m):
    """Expand route bounds by buffer for Snowflake bbox pre-filter."""
    dlat = buffer_m / 111_000
    cos_lat = math.cos(math.radians((min_lat + max_lat) / 2))
    dlon = buffer_m / (111_000 * cos_lat)
    return {
        "min_lat": min_lat - dlat, "max_lat": max_lat + dlat,
        "min_lon": min_lon - dlon, "max_lon": max_lon + dlon,
    }


def _wp_key(wp: dict) -> str:
    return f"{wp['lat']:.5f},{wp['lon']:.5f}"


def _wp_cte(waypoints: list[dict]) -> str:
    """Waypoints as a Snowflake VALUES CTE."""
    rows = ", ".join(f"({wp['lat']:.6f}, {wp['lon']:.6f})" for wp in waypoints)
    return f"wp AS (SELECT column1 AS lat, column2 AS lon FROM VALUES {rows})"


def _query_crimes(cursor, waypoints, bbox, buffer_m,
                  crime_window_days, departure_hour, hour_window) -> dict:
    """Recent crimes within buffer of any waypoint."""
    log = logger.bind(component="route_scorer", query="crime")
    cte = _wp_cte(waypoints)

    h_lo = (departure_hour - hour_window) % 24
    h_hi = (departure_hour + hour_window) % 24
    hour_filter = (
        f"c.hour BETWEEN {h_lo} AND {h_hi}"
        if h_lo <= h_hi
        else f"(c.hour >= {h_lo} OR c.hour <= {h_hi})"
    )

    sql = f"""
        WITH {cte}
        SELECT
            w.lat AS wp_lat, w.lon AS wp_lon,
            COUNT(*) AS total,
            COUNT(CASE WHEN c.severity = 'violent' THEN 1 END) AS violent,
            COUNT(CASE WHEN c.shooting = TRUE THEN 1 END) AS shooting,
            COUNT(CASE WHEN {hour_filter} THEN 1 END) AS at_dep_hour
        FROM wp w
        JOIN RAW.CRIME_INCIDENTS c
            ON c.lat BETWEEN {bbox['min_lat']} AND {bbox['max_lat']}
            AND c.lon BETWEEN {bbox['min_lon']} AND {bbox['max_lon']}
            AND ST_DISTANCE(ST_MAKEPOINT(w.lon, w.lat),
                            ST_MAKEPOINT(c.lon, c.lat)) <= {buffer_m}
            AND c.occurred_on_date >= DATEADD(day, -{crime_window_days}, CURRENT_DATE())
        WHERE c.lat IS NOT NULL
        GROUP BY w.lat, w.lon
    """

    t = time.perf_counter()
    try:
        cursor.execute(sql)
        rows = cursor.fetchall()
    except Exception as e:
        log.error("failed", error=str(e)[:200])
        return {}

    log.info("complete", matched=len(rows), ms=int((time.perf_counter() - t) * 1000))

    result = {}
    dep_total = 0
    for wp_lat, wp_lon, total, violent, shooting, at_dep in rows:
        result[f"{wp_lat:.5f},{wp_lon:.5f}"] = {
            "total": total, "violent": violent, "shooting": shooting,
        }
        dep_total += at_dep
    result["_at_dep_hour"] = dep_total
    return result


def _query_citizen(cursor, waypoints, bbox, buffer_m,
                   citizen_window_hours) -> dict:
    """Recent citizen incidents within buffer of any waypoint."""
    log = logger.bind(component="route_scorer", query="citizen")
    cte = _wp_cte(waypoints)

    sql = f"""
        WITH {cte}
        SELECT
            w.lat AS wp_lat, w.lon AS wp_lon,
            COUNT(*) AS total,
            COUNT(CASE WHEN ci.is_nighttime = TRUE THEN 1 END) AS nighttime
        FROM wp w
        JOIN RAW.CITIZEN_INCIDENTS ci
            ON ci.lat BETWEEN {bbox['min_lat']} AND {bbox['max_lat']}
            AND ci.lon BETWEEN {bbox['min_lon']} AND {bbox['max_lon']}
            AND ST_DISTANCE(ST_MAKEPOINT(w.lon, w.lat),
                            ST_MAKEPOINT(ci.lon, ci.lat)) <= {buffer_m}
            AND ci.incident_ts >= DATEADD(hour, -{citizen_window_hours}, CURRENT_TIMESTAMP())
        WHERE ci.lat IS NOT NULL
        GROUP BY w.lat, w.lon
    """

    t = time.perf_counter()
    try:
        cursor.execute(sql)
        rows = cursor.fetchall()
    except Exception as e:
        log.error("failed", error=str(e)[:200])
        return {}

    log.info("complete", matched=len(rows), ms=int((time.perf_counter() - t) * 1000))

    result = {}
    for wp_lat, wp_lon, total, nighttime in rows:
        result[f"{wp_lat:.5f},{wp_lon:.5f}"] = {"total": total, "nighttime": nighttime}
    return result


def _query_monthly_series(cursor, waypoints, bbox, buffer_m) -> dict[str, dict]:
    """All-time monthly crime counts along corridor.

    Returns {"2026-03": {"total": 5, "violent": 2}, ...}
    Compatible with compute_yoy_change() from app.scoring.scorer.
    """
    log = logger.bind(component="route_scorer", query="monthly_series")
    cte = _wp_cte(waypoints)

    sql = f"""
        WITH {cte}
        SELECT
            TO_CHAR(DATE_TRUNC('month', c.occurred_on_date), 'YYYY-MM') AS month,
            COUNT(*) AS total,
            COUNT(CASE WHEN c.severity = 'violent' THEN 1 END) AS violent
        FROM wp w
        JOIN RAW.CRIME_INCIDENTS c
            ON c.lat BETWEEN {bbox['min_lat']} AND {bbox['max_lat']}
            AND c.lon BETWEEN {bbox['min_lon']} AND {bbox['max_lon']}
            AND ST_DISTANCE(ST_MAKEPOINT(w.lon, w.lat),
                            ST_MAKEPOINT(c.lon, c.lat)) <= {buffer_m}
        WHERE c.lat IS NOT NULL
          AND c.occurred_on_date IS NOT NULL
        GROUP BY DATE_TRUNC('month', c.occurred_on_date)
        ORDER BY month
    """

    t = time.perf_counter()
    try:
        cursor.execute(sql)
        rows = cursor.fetchall()
    except Exception as e:
        log.error("failed", error=str(e)[:200])
        return {}

    log.info("complete", months=len(rows), ms=int((time.perf_counter() - t) * 1000))

    return {month: {"total": total, "violent": violent} for month, total, violent in rows}