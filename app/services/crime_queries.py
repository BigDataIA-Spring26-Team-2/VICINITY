"""Crime query service -- ad-hoc single-point and corridor crime lookups.

Different from route_scorer.py (corridor spatial join for scoring) and
queries.py (all-listing batch for nightly pipeline). This module serves
the Chat Agent's drill-down questions about specific locations:
  "show me violent crimes near this listing"
  "what's the hourly pattern at this address"
  "how does this neighborhood compare"

Usage:
    from app.services.crime_queries import crimes_near_point, hourly_distribution
    with snowflake_cursor() as cursor:
        result = crimes_near_point(cursor, lat=42.35, lon=-71.06, radius_m=500)
"""

from __future__ import annotations

import math
import time
from typing import Optional

import structlog

from app.core.config_loader import CONFIG_DIR
from app.services.listing_queries import QueryResult, _rows_to_dicts, _clamp

logger = structlog.get_logger()

# Boston center latitude for bbox delta computation
_BOSTON_LAT_RAD = math.radians(42.35)


# -- Config ------------------------------------------------------

def _cfg() -> dict:
    if not hasattr(_cfg, "_cache"):
        import yaml
        with open(CONFIG_DIR / "services.yml", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        _cfg._cache = raw.get("crime_queries", {})
    return _cfg._cache


def reload_config():
    if hasattr(_cfg, "_cache"):
        del _cfg._cache


# -- Spatial helpers ---------------------------------------------

def _bbox_deltas(radius_m: int) -> tuple[float, float]:
    """Lat/lon deltas for Snowflake bbox pre-filter at Boston latitude."""
    dlat = radius_m / 111_000
    dlon = radius_m / (111_000 * math.cos(_BOSTON_LAT_RAD))
    return dlat, dlon


# -- Crimes Near Point -------------------------------------------

def crimes_near_point(
    cursor,
    lat: float,
    lon: float,
    *,
    radius_m: Optional[int] = None,
    window_days: Optional[int] = None,
    severity: Optional[str] = None,
    hour_min: Optional[int] = None,
    hour_max: Optional[int] = None,
    shooting_only: bool = False,
    limit: Optional[int] = None,
) -> QueryResult:
    """Recent crimes within radius of a lat/lon point.

    Args:
        cursor: Snowflake cursor.
        lat, lon: Center point.
        radius_m: Search radius in meters.
        window_days: Lookback from today.
        severity: Filter to "violent", "property", or "minor".
        hour_min, hour_max: Hour range filter (24h). Handles midnight wrap.
        shooting_only: If True, only shooting incidents.
        limit: Max rows returned.
    """
    cfg = _cfg()
    pt = cfg.get("near_point", {})
    log = logger.bind(service="crime_queries", query="near_point")

    if not cfg.get("enabled", True):
        return QueryResult(success=False, query_type="crime_near_point",
                           error="crime_queries service is disabled")

    radius_m = _clamp(radius_m or pt.get("default_radius_m", 500),
                      1, pt.get("max_radius_m", 3000))
    window_days = _clamp(window_days or pt.get("default_window_days", 30),
                         1, pt.get("max_window_days", 365))
    limit = _clamp(limit or pt.get("max_results", 500),
                   1, pt.get("max_results", 500))

    dlat, dlon = _bbox_deltas(radius_m)

    conditions = [
        f"c.lat BETWEEN %s AND %s",
        f"c.lon BETWEEN %s AND %s",
        f"ST_DISTANCE(ST_MAKEPOINT(%s, %s), ST_MAKEPOINT(c.lon, c.lat)) <= {radius_m}",
        f"c.occurred_on_date >= DATEADD(day, -{window_days}, CURRENT_DATE())",
        "c.lat IS NOT NULL",
    ]
    params = [
        lat - dlat, lat + dlat,
        lon - dlon, lon + dlon,
        lon, lat,  # ST_MAKEPOINT takes (lon, lat)
    ]

    if severity:
        conditions.append("c.severity = %s")
        params.append(severity)

    if shooting_only:
        conditions.append("c.shooting = TRUE")

    if hour_min is not None and hour_max is not None:
        if hour_min <= hour_max:
            conditions.append("c.hour BETWEEN %s AND %s")
            params.extend([hour_min, hour_max])
        else:
            # Midnight wraparound (e.g. 22 to 4)
            conditions.append("(c.hour >= %s OR c.hour <= %s)")
            params.extend([hour_min, hour_max])

    where = " AND ".join(conditions)

    sql = f"""
        SELECT
            c.incident_id, c.offense_description, c.severity,
            c.occurred_on_date, c.hour, c.day_of_week,
            c.street, c.district, c.lat, c.lon, c.shooting,
            ROUND(ST_DISTANCE(
                ST_MAKEPOINT(%s, %s), ST_MAKEPOINT(c.lon, c.lat)
            )) AS distance_m
        FROM RAW.CRIME_INCIDENTS c
        WHERE {where}
        ORDER BY c.occurred_on_date DESC
        LIMIT {limit}
    """
    # distance_m SELECT params
    params = [lon, lat] + params

    start = time.perf_counter()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("query_failed", error=str(e)[:200])
        return QueryResult(success=False, query_type="crime_near_point",
                           error=str(e)[:500])

    data = _rows_to_dicts(cursor, rows)
    log.info("complete", incidents=len(data), radius_m=radius_m,
             window_days=window_days, ms=ms)

    return QueryResult(
        success=True, query_type="crime_near_point",
        data=data, total_count=len(data), duration_ms=ms,
        sql_executed=sql.strip(),
    )


# -- Crimes Near Corridor ----------------------------------------

def crimes_near_corridor(
    cursor,
    waypoints: list[dict],
    *,
    buffer_m: Optional[int] = None,
    window_days: Optional[int] = None,
    severity: Optional[str] = None,
    limit: Optional[int] = None,
) -> QueryResult:
    """Crime incidents along a route corridor -- drill-down after route scoring.

    Unlike route_scorer.score_corridor (which returns aggregated counts),
    this returns individual incident records for the agent to cite.

    Args:
        cursor: Snowflake cursor.
        waypoints: [{"lat": float, "lon": float}, ...] from a computed route.
        buffer_m: Corridor buffer in meters.
        window_days: Lookback from today.
        severity: Filter to a severity level.
        limit: Max rows returned.
    """
    cfg = _cfg()
    corr = cfg.get("near_corridor", {})
    log = logger.bind(service="crime_queries", query="near_corridor")

    if not cfg.get("enabled", True):
        return QueryResult(success=False, query_type="crime_near_corridor",
                           error="crime_queries service is disabled")

    if not waypoints:
        return QueryResult(success=False, query_type="crime_near_corridor",
                           error="No waypoints provided")

    buffer_m = _clamp(buffer_m or corr.get("default_buffer_m", 300),
                      1, corr.get("max_buffer_m", 1000))
    window_days = _clamp(window_days or corr.get("default_window_days", 30),
                         1, corr.get("max_window_days", 365))
    limit = _clamp(limit or corr.get("max_results", 1000),
                   1, corr.get("max_results", 1000))

    # Route bounding box
    lats = [w["lat"] for w in waypoints]
    lons = [w["lon"] for w in waypoints]
    dlat, dlon = _bbox_deltas(buffer_m)
    bbox = {
        "min_lat": min(lats) - dlat, "max_lat": max(lats) + dlat,
        "min_lon": min(lons) - dlon, "max_lon": max(lons) + dlon,
    }

    # Waypoints as VALUES CTE
    wp_rows = ", ".join(f"({w['lat']:.6f}, {w['lon']:.6f})" for w in waypoints)
    cte = f"wp AS (SELECT column1 AS lat, column2 AS lon FROM VALUES {wp_rows})"

    sev_filter = ""
    params = []
    if severity:
        sev_filter = "AND c.severity = %s"
        params.append(severity)

    sql = f"""
        WITH {cte}
        SELECT DISTINCT
            c.incident_id, c.offense_description, c.severity,
            c.occurred_on_date, c.hour, c.day_of_week,
            c.street, c.district, c.lat, c.lon, c.shooting
        FROM RAW.CRIME_INCIDENTS c
        JOIN wp w
            ON c.lat BETWEEN {bbox['min_lat']} AND {bbox['max_lat']}
            AND c.lon BETWEEN {bbox['min_lon']} AND {bbox['max_lon']}
            AND ST_DISTANCE(ST_MAKEPOINT(w.lon, w.lat),
                            ST_MAKEPOINT(c.lon, c.lat)) <= {buffer_m}
        WHERE c.lat IS NOT NULL
            AND c.occurred_on_date >= DATEADD(day, -{window_days}, CURRENT_DATE())
            {sev_filter}
        ORDER BY c.occurred_on_date DESC
        LIMIT {limit}
    """

    start = time.perf_counter()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("query_failed", error=str(e)[:200])
        return QueryResult(success=False, query_type="crime_near_corridor",
                           error=str(e)[:500])

    data = _rows_to_dicts(cursor, rows)
    log.info("complete", incidents=len(data), waypoints=len(waypoints),
             buffer_m=buffer_m, ms=ms)

    return QueryResult(
        success=True, query_type="crime_near_corridor",
        data=data, total_count=len(data), duration_ms=ms,
        sql_executed=sql.strip(),
    )


# -- Hourly Distribution ----------------------------------------

def hourly_distribution(
    cursor,
    lat: float,
    lon: float,
    *,
    radius_m: Optional[int] = None,
    months_back: Optional[int] = None,
) -> QueryResult:
    """24-bucket crime count by hour for a point. Answers "when is it dangerous."

    Args:
        cursor: Snowflake cursor.
        lat, lon: Center point.
        radius_m: Search radius.
        months_back: How far back to aggregate.
    """
    cfg = _cfg()
    pt = cfg.get("near_point", {})
    hr = cfg.get("hourly", {})
    log = logger.bind(service="crime_queries", query="hourly")

    if not cfg.get("enabled", True):
        return QueryResult(success=False, query_type="crime_hourly",
                           error="crime_queries service is disabled")

    radius_m = _clamp(radius_m or pt.get("default_radius_m", 500),
                      1, pt.get("max_radius_m", 3000))
    months_back = _clamp(months_back or hr.get("default_months_back", 6),
                         1, hr.get("max_months_back", 24))

    dlat, dlon = _bbox_deltas(radius_m)

    sql = f"""
        SELECT c.hour, COUNT(*) AS count,
               COUNT(CASE WHEN c.severity = 'violent' THEN 1 END) AS violent
        FROM RAW.CRIME_INCIDENTS c
        WHERE c.lat BETWEEN %s AND %s
            AND c.lon BETWEEN %s AND %s
            AND ST_DISTANCE(ST_MAKEPOINT(%s, %s), ST_MAKEPOINT(c.lon, c.lat)) <= {radius_m}
            AND c.occurred_on_date >= DATEADD(month, -{months_back}, CURRENT_DATE())
            AND c.lat IS NOT NULL AND c.hour IS NOT NULL
        GROUP BY c.hour
        ORDER BY c.hour
    """
    params = [lat - dlat, lat + dlat, lon - dlon, lon + dlon, lon, lat]

    start = time.perf_counter()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("query_failed", error=str(e)[:200])
        return QueryResult(success=False, query_type="crime_hourly",
                           error=str(e)[:500])

    data = [{"hour": int(r[0]), "count": r[1], "violent": r[2]} for r in rows]
    log.info("complete", buckets=len(data), months_back=months_back, ms=ms)

    return QueryResult(
        success=True, query_type="crime_hourly",
        data=data, total_count=len(data), duration_ms=ms,
        sql_executed=sql.strip(),
    )


# -- Neighborhood Stats -----------------------------------------

def neighborhood_stats(
    cursor,
    neighborhood: str,
    *,
    window_days: Optional[int] = None,
) -> QueryResult:
    """Aggregate crime stats for a neighborhood. Answers "how safe is Allston."

    Args:
        cursor: Snowflake cursor.
        neighborhood: Neighborhood name (matched against listing neighborhood).
        window_days: Lookback from today.
    """
    cfg = _cfg()
    ns = cfg.get("neighborhood_stats", {})
    log = logger.bind(service="crime_queries", query="neighborhood_stats")

    if not cfg.get("enabled", True):
        return QueryResult(success=False, query_type="crime_neighborhood",
                           error="crime_queries service is disabled")

    window_days = _clamp(window_days or ns.get("default_window_days", 30),
                         1, ns.get("max_window_days", 365))

    # Use safety_radius_m from scoring.yml for neighborhood-level spatial join
    from app.scoring.config import load_scoring_config
    scoring = load_scoring_config()
    radius_m = scoring.safety_radius_m
    dlat, dlon = _bbox_deltas(radius_m)

    sql = f"""
        SELECT
            c.district,
            COUNT(*) AS total,
            COUNT(CASE WHEN c.severity = 'violent' THEN 1 END) AS violent,
            COUNT(CASE WHEN c.severity = 'property' THEN 1 END) AS property,
            COUNT(CASE WHEN c.shooting = TRUE THEN 1 END) AS shootings,
            COUNT(DISTINCT c.street) AS streets_affected,
            MODE(c.offense_description) AS most_common_offense
        FROM RAW.CRIME_INCIDENTS c
        JOIN RAW.LISTINGS l
            ON c.lat BETWEEN l.lat - {dlat} AND l.lat + {dlat}
            AND c.lon BETWEEN l.lon - {dlon} AND l.lon + {dlon}
            AND ST_DISTANCE(ST_MAKEPOINT(l.lon, l.lat),
                            ST_MAKEPOINT(c.lon, c.lat)) <= {radius_m}
        WHERE LOWER(l.neighborhood) = LOWER(%s)
            AND l.is_current = TRUE
            AND c.occurred_on_date >= DATEADD(day, -{window_days}, CURRENT_DATE())
            AND c.lat IS NOT NULL
        GROUP BY c.district
    """
    params = [neighborhood]

    start = time.perf_counter()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("query_failed", neighborhood=neighborhood, error=str(e)[:200])
        return QueryResult(success=False, query_type="crime_neighborhood",
                           error=str(e)[:500])

    data = _rows_to_dicts(cursor, rows)
    log.info("complete", neighborhood=neighborhood, districts=len(data),
             window_days=window_days, ms=ms)

    return QueryResult(
        success=True, query_type="crime_neighborhood",
        data=data, total_count=len(data), duration_ms=ms,
        sql_executed=sql.strip(),
    )