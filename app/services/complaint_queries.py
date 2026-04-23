"""Complaint query service -- ad-hoc 311 lookups near a point.

Simple spatial queries against COMPLAINTS_311. The agent provides a
lat/lon, we return complaints within radius via ST_DISTANCE. No
two-tier fallback -- that's the batch scoring pipeline's concern.

Usage:
    from app.services.complaint_queries import complaints_near_point
    with snowflake_cursor() as cursor:
        result = complaints_near_point(cursor, lat=42.35, lon=-71.06)
"""

from __future__ import annotations

import math
import time
from typing import Optional

import structlog

from app.core.config_loader import CONFIG_DIR
from app.services.listing_queries import QueryResult, _rows_to_dicts, _clamp

logger = structlog.get_logger()

_BOSTON_LAT_RAD = math.radians(42.35)


# -- Config ------------------------------------------------------

def _cfg() -> dict:
    if not hasattr(_cfg, "_cache"):
        import yaml
        with open(CONFIG_DIR / "services.yml", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        _cfg._cache = raw.get("complaint_queries", {})
    return _cfg._cache


def reload_config():
    if hasattr(_cfg, "_cache"):
        del _cfg._cache


def _bbox_deltas(radius_m: int) -> tuple[float, float]:
    dlat = radius_m / 111_000
    dlon = radius_m / (111_000 * math.cos(_BOSTON_LAT_RAD))
    return dlat, dlon


# -- Complaints Near Point ---------------------------------------

def complaints_near_point(
    cursor,
    lat: float,
    lon: float,
    *,
    radius_m: Optional[int] = None,
    window_days: Optional[int] = None,
    category: Optional[str] = None,
    limit: Optional[int] = None,
) -> QueryResult:
    """311 complaints within radius of a lat/lon.

    Args:
        cursor: Snowflake cursor.
        lat, lon: Center point.
        radius_m: Search radius in meters.
        window_days: Lookback from today.
        category: Filter to a category (validated against config allowlist).
        limit: Max rows returned.
    """
    cfg = _cfg()
    pt = cfg.get("near_point", {})
    log = logger.bind(service="complaint_queries", query="near_point")

    if not cfg.get("enabled", True):
        return QueryResult(success=False, query_type="complaints_near_point",
                           error="complaint_queries service is disabled")

    radius_m = _clamp(radius_m or pt.get("default_radius_m", 500),
                      1, pt.get("max_radius_m", 3000))
    window_days = _clamp(window_days or pt.get("default_window_days", 30),
                         1, pt.get("max_window_days", 365))
    limit = _clamp(limit or pt.get("max_results", 500),
                   1, pt.get("max_results", 500))

    valid_cats = cfg.get("valid_categories", [])
    if category and valid_cats and category not in valid_cats:
        return QueryResult(
            success=False, query_type="complaints_near_point",
            error=f"Invalid category '{category}'. Valid: {', '.join(valid_cats)}"
        )

    dlat, dlon = _bbox_deltas(radius_m)

    cat_filter = ""
    params = [
        lon, lat,                                        # distance_m SELECT
        lat - dlat, lat + dlat, lon - dlon, lon + dlon,  # bbox
        lon, lat,                                        # ST_DISTANCE WHERE
    ]
    if category:
        cat_filter = "AND c.category = %s"
        params.append(category)

    sql = f"""
        SELECT
            c.case_enquiry_id, c.open_dt, c.case_title,
            c.type, c.category, c.street, c.neighborhood,
            c.zip_code, c.lat, c.lon, c.case_status,
            ROUND(ST_DISTANCE(
                ST_MAKEPOINT(%s, %s), ST_MAKEPOINT(c.lon, c.lat)
            )) AS distance_m
        FROM RAW.COMPLAINTS_311 c
        WHERE c.lat IS NOT NULL AND c.lat != 0
            AND c.lat BETWEEN %s AND %s
            AND c.lon BETWEEN %s AND %s
            AND ST_DISTANCE(
                ST_MAKEPOINT(%s, %s), ST_MAKEPOINT(c.lon, c.lat)
            ) <= {radius_m}
            AND c.open_dt >= DATEADD(day, -{window_days}, CURRENT_DATE())
            {cat_filter}
        ORDER BY c.open_dt DESC
        LIMIT {limit}
    """

    start = time.perf_counter()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("query_failed", error=str(e)[:200])
        return QueryResult(success=False, query_type="complaints_near_point",
                           error=str(e)[:500])

    data = _rows_to_dicts(cursor, rows)
    log.info("complete", complaints=len(data), radius_m=radius_m,
             window_days=window_days, ms=ms)

    return QueryResult(
        success=True, query_type="complaints_near_point",
        data=data, total_count=len(data), duration_ms=ms,
        sql_executed=sql.strip(),
    )


# -- Complaint Summary by Category ------------------------------

def complaint_summary(
    cursor,
    lat: float,
    lon: float,
    *,
    radius_m: Optional[int] = None,
    window_days: Optional[int] = None,
) -> QueryResult:
    """Category breakdown of complaints near a point. Counts only, no records.

    Args:
        cursor: Snowflake cursor.
        lat, lon: Center point.
        radius_m: Search radius.
        window_days: Lookback from today.
    """
    cfg = _cfg()
    pt = cfg.get("near_point", {})
    log = logger.bind(service="complaint_queries", query="summary")

    if not cfg.get("enabled", True):
        return QueryResult(success=False, query_type="complaint_summary",
                           error="complaint_queries service is disabled")

    radius_m = _clamp(radius_m or pt.get("default_radius_m", 500),
                      1, pt.get("max_radius_m", 3000))
    window_days = _clamp(window_days or pt.get("default_window_days", 30),
                         1, pt.get("max_window_days", 365))

    dlat, dlon = _bbox_deltas(radius_m)

    sql = f"""
        SELECT
            c.category,
            COUNT(*) AS count,
            MIN(c.open_dt) AS earliest,
            MAX(c.open_dt) AS latest
        FROM RAW.COMPLAINTS_311 c
        WHERE c.lat IS NOT NULL AND c.lat != 0
            AND c.lat BETWEEN %s AND %s
            AND c.lon BETWEEN %s AND %s
            AND ST_DISTANCE(ST_MAKEPOINT(%s, %s), ST_MAKEPOINT(c.lon, c.lat)) <= {radius_m}
            AND c.open_dt >= DATEADD(day, -{window_days}, CURRENT_DATE())
        GROUP BY c.category
        ORDER BY count DESC
    """
    params = [lat - dlat, lat + dlat, lon - dlon, lon + dlon, lon, lat]

    start = time.perf_counter()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("query_failed", error=str(e)[:200])
        return QueryResult(success=False, query_type="complaint_summary",
                           error=str(e)[:500])

    data = _rows_to_dicts(cursor, rows)
    log.info("complete", categories=len(data), radius_m=radius_m, ms=ms)

    return QueryResult(
        success=True, query_type="complaint_summary",
        data=data, total_count=len(data), duration_ms=ms,
        sql_executed=sql.strip(),
    )