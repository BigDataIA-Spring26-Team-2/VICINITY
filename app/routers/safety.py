"""Safety router — crime and complaint data endpoints.

Direct access to crime incidents, complaint records, hourly distributions,
and neighborhood-level stats. Powers the safety detail panel: heatmap
click-through, 24-hour chart, neighborhood comparison table, and
complaint category breakdown.

All public (no auth required). Location-based queries need lat/lon.

Endpoints:
    GET /safety/crimes              — crime incidents near a point
    GET /safety/crimes/hourly       — 24-hour crime distribution
    GET /safety/neighborhood        — aggregate stats by neighborhood name
    GET /safety/complaints          — 311 complaints near a point
    GET /safety/complaints/summary  — complaint category breakdown
"""

from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.database import get_cursor

logger = structlog.get_logger()

router = APIRouter()


# -- GET /safety/crimes ----------------------------------------------------

@router.get(
    "/crimes",
    summary="Crime incidents near a point",
)
def crimes_near_point(
    lat: float = Query(..., description="Center latitude"),
    lon: float = Query(..., description="Center longitude"),
    radius_m: Optional[int] = Query(None, ge=1, le=3000, description="Radius meters"),
    window_days: Optional[int] = Query(None, ge=1, le=365, description="Lookback days"),
    severity: Optional[str] = Query(None, description="violent|property|minor"),
    hour_min: Optional[int] = Query(None, ge=0, le=23, description="Start hour (24h)"),
    hour_max: Optional[int] = Query(None, ge=0, le=23, description="End hour (24h)"),
    shooting_only: bool = Query(False, description="Only shooting incidents"),
    limit: Optional[int] = Query(None, ge=1, le=2000, description="Max results"),
    cursor=Depends(get_cursor),
):
    """Crime incidents within radius of a point.

    Returns per incident: incident_id, offense_description, severity,
    occurred_on_date, hour, day_of_week, street, district, lat, lon,
    shooting flag, and distance_m from center.

    Supports time-of-day filtering with midnight wraparound
    (hour_min=22, hour_max=4 returns 10PM–4AM incidents).

    Used by the frontend for both heatmap rendering and safety
    detail drill-down on listing click.
    """
    from app.services.crime_queries import crimes_near_point as _crimes

    result = _crimes(
        cursor, lat, lon,
        radius_m=radius_m, window_days=window_days,
        severity=severity, hour_min=hour_min, hour_max=hour_max,
        shooting_only=shooting_only, limit=limit,
    )

    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)

    return result.to_dict()


# -- GET /safety/crimes/hourly ---------------------------------------------

@router.get(
    "/crimes/hourly",
    summary="24-hour crime distribution",
)
def crimes_hourly(
    lat: float = Query(..., description="Center latitude"),
    lon: float = Query(..., description="Center longitude"),
    radius_m: Optional[int] = Query(None, ge=1, le=3000),
    months_back: Optional[int] = Query(None, ge=1, le=24),
    cursor=Depends(get_cursor),
):
    """24-bucket crime count by hour for bar chart rendering.

    Returns up to 24 objects with fields: hour (int 0-23), count
    (total incidents), violent (violent-severity incidents). Hours
    with zero incidents are omitted — the frontend should fill gaps.
    """
    from app.services.crime_queries import hourly_distribution

    result = hourly_distribution(
        cursor, lat, lon,
        radius_m=radius_m, months_back=months_back,
    )

    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)

    return result.to_dict()


# -- GET /safety/neighborhood ----------------------------------------------

@router.get(
    "/neighborhood",
    summary="Aggregate crime stats for a neighborhood",
)
def neighborhood_stats(
    neighborhood: str = Query(..., min_length=1, description="Neighborhood name"),
    window_days: Optional[int] = Query(None, ge=1, le=365),
    cursor=Depends(get_cursor),
):
    """Aggregate crime statistics for a named neighborhood.

    Returns per police district overlapping the neighborhood: total
    incidents, violent count, property count, shootings count,
    streets_affected (distinct streets with incidents), and
    most_common_offense. Use for neighborhood comparison tables
    or radar charts.
    """
    from app.services.crime_queries import neighborhood_stats as _stats

    result = _stats(cursor, neighborhood, window_days=window_days)

    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)

    return result.to_dict()


# -- GET /safety/complaints ------------------------------------------------

@router.get(
    "/complaints",
    summary="311 complaints near a point",
)
def complaints_near_point(
    lat: float = Query(..., description="Center latitude"),
    lon: float = Query(..., description="Center longitude"),
    radius_m: Optional[int] = Query(None, ge=1, le=3000),
    window_days: Optional[int] = Query(None, ge=1, le=365),
    category: Optional[str] = Query(
        None,
        description="noise|pest|rodent|road|sanitation|heat|housing|other",
    ),
    limit: Optional[int] = Query(None, ge=1, le=500),
    cursor=Depends(get_cursor),
):
    """311 complaints within radius of a point.

    Returns per complaint: case_enquiry_id, open_dt, case_title,
    type, category, street, neighborhood, zip_code, lat, lon,
    case_status, and distance_m from center.
    """
    from app.services.complaint_queries import complaints_near_point as _complaints

    result = _complaints(
        cursor, lat, lon,
        radius_m=radius_m, window_days=window_days,
        category=category, limit=limit,
    )

    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)

    return result.to_dict()


# -- GET /safety/complaints/summary ----------------------------------------

@router.get(
    "/complaints/summary",
    summary="Complaint category breakdown near a point",
)
def complaint_summary(
    lat: float = Query(..., description="Center latitude"),
    lon: float = Query(..., description="Center longitude"),
    radius_m: Optional[int] = Query(None, ge=1, le=3000),
    window_days: Optional[int] = Query(None, ge=1, le=365),
    cursor=Depends(get_cursor),
):
    """Category breakdown of 311 complaints near a point.

    Returns per category: category name, count, earliest complaint
    date, latest complaint date. Use for pie/donut charts or
    stacked bar charts in the livability panel.
    """
    from app.services.complaint_queries import complaint_summary as _summary

    result = _summary(
        cursor, lat, lon,
        radius_m=radius_m, window_days=window_days,
    )

    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)

    return result.to_dict()