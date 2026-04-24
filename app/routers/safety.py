"""Safety router — crime, complaints, heatmap endpoints.

Single-point crime/complaint queries delegate to the service layer
(reused by the agent). The `/crimes/heatmap` endpoint owns its SQL
here because it's frontend-only — no agent consumes it.

Public:
    GET /safety/crimes              — crimes near a point
    GET /safety/crimes/hourly       — 24-hour crime distribution
    GET /safety/crimes/heatmap      — region-wide density points (frontend-only)
    GET /safety/neighborhood        — aggregate neighborhood stats
    GET /safety/complaints          — 311 near a point
    GET /safety/complaints/summary  — 311 category breakdown
"""

from __future__ import annotations

import time
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.database import get_cursor

logger = structlog.get_logger()
router = APIRouter()


@router.get("/crimes", summary="Crime incidents near a point")
def crimes_near_point(
    lat: float = Query(..., description="Center latitude"),
    lon: float = Query(..., description="Center longitude"),
    radius_m: Optional[int] = Query(None, ge=1, le=3000),
    window_days: Optional[int] = Query(None, ge=1, le=365),
    severity: Optional[str] = Query(None, description="violent|property|minor"),
    hour_min: Optional[int] = Query(None, ge=0, le=23),
    hour_max: Optional[int] = Query(None, ge=0, le=23),
    shooting_only: bool = Query(False),
    limit: Optional[int] = Query(None, ge=1, le=2000),
    cursor=Depends(get_cursor),
):
    from app.services.crime_queries import crimes_near_point as _q
    r = _q(
        cursor, lat, lon,
        radius_m=radius_m, window_days=window_days,
        severity=severity, hour_min=hour_min, hour_max=hour_max,
        shooting_only=shooting_only, limit=limit,
    )
    if not r.success:
        raise HTTPException(status_code=500, detail=r.error)
    return r.to_dict()


@router.get("/crimes/hourly", summary="24-hour crime distribution")
def crimes_hourly(
    lat: float = Query(...),
    lon: float = Query(...),
    radius_m: Optional[int] = Query(None, ge=1, le=3000),
    months_back: Optional[int] = Query(None, ge=1, le=24),
    cursor=Depends(get_cursor),
):
    from app.services.crime_queries import hourly_distribution
    r = hourly_distribution(cursor, lat, lon, radius_m=radius_m, months_back=months_back)
    if not r.success:
        raise HTTPException(status_code=500, detail=r.error)
    return r.to_dict()


@router.get(
    "/crimes/heatmap",
    summary="Region-wide crime density points for the HeatmapLayer (frontend-only)",
)
def crimes_heatmap(
    min_lat: float = Query(42.23, description="Bbox south"),
    max_lat: float = Query(42.40, description="Bbox north"),
    min_lon: float = Query(-71.20, description="Bbox west"),
    max_lon: float = Query(-70.98, description="Bbox east"),
    window_days: Optional[int] = Query(
        None, ge=1, le=3650,
        description="Lookback days. None = all historical (default for heatmap).",
    ),
    max_points: int = Query(20000, ge=1, le=100000),
    cursor=Depends(get_cursor),
):
    """Region-wide crime density for a Google Maps HeatmapLayer render.

    Returns minimal records: lat, lon, severity, weight, occurred_on_date.
    No radius filter — heatmaps need full coverage. Weight is severity-scaled
    (violent=3, property=1.5, minor=1, non_crime=0.5) so the renderer
    emphasizes violent clusters without hiding minor density.

    This endpoint owns its SQL — no agent consumes it.
    """
    log = logger.bind(endpoint="crimes_heatmap")

    max_points = max(1, min(int(max_points), 100000))

    conditions = [
        "c.lat BETWEEN %s AND %s",
        "c.lon BETWEEN %s AND %s",
        "c.lat IS NOT NULL",
    ]
    params: list = [min_lat, max_lat, min_lon, max_lon]

    if window_days is not None:
        conditions.append(
            f"c.occurred_on_date >= DATEADD(day, -{int(window_days)}, CURRENT_DATE())"
        )

    where = " AND ".join(conditions)
    sql = (
        "SELECT "
        "  c.lat, c.lon, c.severity, "
        "  c.occurred_on_date::STRING AS occurred_on_date, "
        "  CASE c.severity "
        "    WHEN 'violent'   THEN 3.0 "
        "    WHEN 'property'  THEN 1.5 "
        "    WHEN 'minor'     THEN 1.0 "
        "    WHEN 'non_crime' THEN 0.5 "
        "    ELSE 1.0 "
        "  END AS weight "
        "FROM RAW.CRIME_INCIDENTS c "
        f"WHERE {where} "
        "ORDER BY c.occurred_on_date DESC "
        f"LIMIT {max_points}"
    )

    t0 = time.perf_counter()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    except Exception as e:
        log.error("heatmap_query_failed", error=str(e)[:200])
        raise HTTPException(status_code=500, detail="Heatmap query failed")

    cols = [d[0].lower() for d in cursor.description]
    data = [dict(zip(cols, row)) for row in rows]
    ms = int((time.perf_counter() - t0) * 1000)

    log.info("heatmap_ok", points=len(data), window_days=window_days,
             capped=(len(data) >= max_points), ms=ms)

    return {
        "success": True,
        "query_type": "crime_heatmap",
        "data": data,
        "total_count": len(data),
        "duration_ms": ms,
    }


@router.get(
    "/crimes/distribution",
    summary="Historical hour-of-day / day-of-week / yearly distribution of all crimes near a point",
)
def crimes_distribution(
    lat: float = Query(..., description="Center latitude"),
    lon: float = Query(..., description="Center longitude"),
    radius_m: Optional[int] = Query(
        None, ge=50, le=3000,
        description="Radius in meters. Defaults to crime_queries.near_point.default_radius_m.",
    ),
    cursor=Depends(get_cursor),
):
    """Full-history crime time distribution near a point.

    Reads radius default from ``config/services.yml → crime_queries.near_point``
    so it stays in sync with the agent's crime lookup tools.

    Aggregates the ENTIRE RAW.CRIME_INCIDENTS table (~3 years, 246K+ records)
    into three distributions: hour-of-day (24 buckets), day-of-week (7),
    year (N). Returns totals + severity breakdown + date range.
    """
    from app.services.crime_queries import _cfg as _crime_cfg
    log = logger.bind(endpoint="crimes_distribution")

    cfg = _crime_cfg().get("near_point", {})
    if radius_m is None:
        radius_m = cfg.get("default_radius_m", 1000)
    radius_m = max(50, min(int(radius_m), cfg.get("max_radius_m", 3000)))

    # Bbox half-spans with margin so the haversine precision filter does
    # the precision work without the bbox cutting corners.
    lat_span = (radius_m / 111000.0) * 1.2
    lon_span = (radius_m / (111000.0 * 0.74)) * 1.2  # cos(42°N) ≈ 0.74

    sql = """
        WITH scoped AS (
            SELECT
                occurred_on_date,
                severity
            FROM RAW.CRIME_INCIDENTS
            WHERE lat IS NOT NULL
              AND lat BETWEEN %s AND %s
              AND lon BETWEEN %s AND %s
              AND HAVERSINE(%s, %s, lat, lon) * 1000 <= %s
        ),
        hourly AS (
            SELECT OBJECT_AGG(hr::STRING, cnt) AS h
            FROM (
                SELECT HOUR(occurred_on_date) AS hr, COUNT(*) AS cnt
                FROM scoped
                GROUP BY 1
            )
        ),
        dow AS (
            SELECT OBJECT_AGG(day_name, cnt) AS d
            FROM (
                SELECT DAYNAME(occurred_on_date) AS day_name, COUNT(*) AS cnt
                FROM scoped
                GROUP BY 1
            )
        ),
        yearly AS (
            SELECT OBJECT_AGG(yr::STRING, cnt) AS y
            FROM (
                SELECT YEAR(occurred_on_date)::STRING AS yr, COUNT(*) AS cnt
                FROM scoped
                GROUP BY 1
            )
        ),
        meta AS (
            SELECT
                COUNT(*) AS total,
                SUM(IFF(severity = 'violent',  1, 0)) AS violent,
                SUM(IFF(severity = 'property', 1, 0)) AS property_count,
                SUM(IFF(severity = 'minor',    1, 0)) AS minor_count,
                MIN(occurred_on_date)::STRING AS earliest_date,
                MAX(occurred_on_date)::STRING AS latest_date
            FROM scoped
        )
        SELECT
            meta.total,
            meta.violent,
            meta.property_count,
            meta.minor_count,
            meta.earliest_date,
            meta.latest_date,
            hourly.h AS hourly,
            dow.d    AS dow,
            yearly.y AS yearly
        FROM meta, hourly, dow, yearly
    """
    params = [
        lat - lat_span, lat + lat_span,
        lon - lon_span, lon + lon_span,
        lat, lon, radius_m,
    ]

    t0 = time.perf_counter()
    try:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    except Exception as e:
        log.error("distribution_query_failed", error=str(e)[:200])
        raise HTTPException(status_code=500, detail="Distribution query failed")

    import json as _json
    cols = [d[0].lower() for d in cursor.description]
    data = dict(zip(cols, row)) if row else {}

    for key in ("hourly", "dow", "yearly"):
        v = data.get(key)
        if isinstance(v, str):
            try:
                data[key] = _json.loads(v)
            except Exception:
                data[key] = {}
        elif v is None:
            data[key] = {}

    data["radius_m"] = radius_m
    ms = int((time.perf_counter() - t0) * 1000)
    log.info("distribution_ok",
             total=data.get("total", 0),
             radius_m=radius_m, ms=ms)

    return {
        "success": True,
        "query_type": "crime_distribution",
        "data": data,
        "duration_ms": ms,
    }


@router.get(
    "/crimes/types",
    summary="Top offense descriptions near a point — what kind of incidents happen here",
)
def crimes_types(
    lat: float = Query(..., description="Center latitude"),
    lon: float = Query(..., description="Center longitude"),
    radius_m: Optional[int] = Query(
        None, ge=50, le=3000,
        description="Radius in meters. Defaults to crime_queries.near_point.default_radius_m.",
    ),
    window_days: Optional[int] = Query(
        None, ge=1, le=3650,
        description="Lookback days. Omit for all historical data.",
    ),
    top: int = Query(10, ge=1, le=40, description="Max offense types to return."),
    cursor=Depends(get_cursor),
):
    """Top offense descriptions aggregated over a point's neighborhood.

    Replaces the "Types" tile's flat count with an actionable breakdown:
    which specific offense descriptions dominate this area, their counts,
    percentage share, and the dominant severity class per offense.

    Reads radius default from ``config/services.yml → crime_queries.near_point``
    for consistency with the agent's crime tools.
    """
    from app.services.crime_queries import _cfg as _crime_cfg
    log = logger.bind(endpoint="crimes_types")

    cfg = _crime_cfg().get("near_point", {})
    if radius_m is None:
        radius_m = cfg.get("default_radius_m", 1000)
    radius_m = max(50, min(int(radius_m), cfg.get("max_radius_m", 3000)))

    lat_span = (radius_m / 111000.0) * 1.2
    lon_span = (radius_m / (111000.0 * 0.74)) * 1.2

    day_clause = ""
    if window_days:
        day_clause = f"AND occurred_on_date >= DATEADD(day, -{int(window_days)}, CURRENT_DATE())"

    sql = f"""
        WITH scoped AS (
            SELECT offense_description, severity
            FROM RAW.CRIME_INCIDENTS
            WHERE lat IS NOT NULL
              AND lat BETWEEN %s AND %s
              AND lon BETWEEN %s AND %s
              AND HAVERSINE(%s, %s, lat, lon) * 1000 <= %s
              {day_clause}
              AND offense_description IS NOT NULL
        ),
        total AS (SELECT COUNT(*) AS n FROM scoped),
        ranked AS (
            SELECT
                offense_description,
                COUNT(*)                              AS cnt,
                MODE(severity)                        AS dominant_severity,
                SUM(IFF(severity = 'violent',  1, 0)) AS violent,
                SUM(IFF(severity = 'property', 1, 0)) AS property,
                SUM(IFF(severity = 'minor',    1, 0)) AS minor
            FROM scoped
            GROUP BY offense_description
            ORDER BY cnt DESC
            LIMIT {int(top)}
        )
        SELECT
            r.offense_description,
            r.cnt,
            ROUND(100.0 * r.cnt / NULLIF(t.n, 0), 1) AS pct,
            r.dominant_severity,
            r.violent,
            r.property,
            r.minor,
            t.n AS total_scoped
        FROM ranked r, total t
        ORDER BY r.cnt DESC
    """
    params = [
        lat - lat_span, lat + lat_span,
        lon - lon_span, lon + lon_span,
        lat, lon, radius_m,
    ]

    t0 = time.perf_counter()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    except Exception as e:
        log.error("types_query_failed", error=str(e)[:200])
        raise HTTPException(status_code=500, detail="Types query failed")

    total_scoped = int(rows[0][7]) if rows else 0
    data = [
        {
            "offense": r[0],
            "count": int(r[1]),
            "pct": float(r[2]) if r[2] is not None else 0.0,
            "severity": r[3],
            "violent": int(r[4]),
            "property": int(r[5]),
            "minor": int(r[6]),
        }
        for r in rows
    ]

    ms = int((time.perf_counter() - t0) * 1000)
    log.info(
        "types_ok",
        radius_m=radius_m,
        total_scoped=total_scoped,
        returned=len(data),
        ms=ms,
    )

    return {
        "success": True,
        "query_type": "crime_types",
        "data": {
            "total_scoped": total_scoped,
            "radius_m": radius_m,
            "window_days": window_days,
            "offenses": data,
        },
        "duration_ms": ms,
    }


@router.get("/neighborhood", summary="Aggregate crime stats for a neighborhood")
def neighborhood_stats(
    neighborhood: str = Query(..., min_length=1),
    window_days: Optional[int] = Query(None, ge=1, le=365),
    cursor=Depends(get_cursor),
):
    from app.services.crime_queries import neighborhood_stats as _q
    r = _q(cursor, neighborhood, window_days=window_days)
    if not r.success:
        raise HTTPException(status_code=500, detail=r.error)
    return r.to_dict()


@router.get("/complaints", summary="311 complaints near a point")
def complaints_near_point(
    lat: float = Query(...),
    lon: float = Query(...),
    radius_m: Optional[int] = Query(None, ge=1, le=3000),
    window_days: Optional[int] = Query(None, ge=1, le=365),
    category: Optional[str] = Query(None),
    limit: Optional[int] = Query(None, ge=1, le=500),
    cursor=Depends(get_cursor),
):
    from app.services.complaint_queries import complaints_near_point as _q
    r = _q(
        cursor, lat, lon,
        radius_m=radius_m, window_days=window_days,
        category=category, limit=limit,
    )
    if not r.success:
        raise HTTPException(status_code=500, detail=r.error)
    return r.to_dict()


@router.get("/complaints/summary", summary="Complaint category breakdown")
def complaint_summary(
    lat: float = Query(...),
    lon: float = Query(...),
    radius_m: Optional[int] = Query(None, ge=1, le=3000),
    window_days: Optional[int] = Query(None, ge=1, le=365),
    cursor=Depends(get_cursor),
):
    from app.services.complaint_queries import complaint_summary as _q
    r = _q(cursor, lat, lon, radius_m=radius_m, window_days=window_days)
    if not r.success:
        raise HTTPException(status_code=500, detail=r.error)
    return r.to_dict()