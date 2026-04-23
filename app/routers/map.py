"""Map router — GeoJSON-ready endpoints for the frontend map layer.

All endpoints return data with lat/lon coordinates suitable for
Google Maps / Mapbox rendering. Designed for the interactive dashboard
that shows listings as pins, crime as a heatmap, transit stops as
markers, and commute routes as polylines.

Public endpoints (no auth):
    GET /map/listings   — all active listing pins with scores
    GET /map/crimes     — crime incidents within a bounding box
    GET /map/transit    — MBTA stops with route info
    GET /map/amenities  — amenities near a point

Authenticated endpoints:
    GET /map/routes     — user's commute route polylines + waypoint scores
"""

from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import get_current_user, get_optional_user
from app.core.cache import get_cache
from app.core.database import get_cursor

logger = structlog.get_logger()

router = APIRouter()


# -- GET /map/listings -----------------------------------------------------

@router.get(
    "/listings",
    summary="Listing map pins with scores",
)
def map_listings(
    min_lat: Optional[float] = Query(None, description="Viewport south bound"),
    max_lat: Optional[float] = Query(None, description="Viewport north bound"),
    min_lon: Optional[float] = Query(None, description="Viewport west bound"),
    max_lon: Optional[float] = Query(None, description="Viewport east bound"),
    min_price: Optional[int] = Query(None, ge=0),
    max_price: Optional[int] = Query(None, ge=0),
    beds_min: Optional[int] = Query(None, ge=0),
    min_safety_score: Optional[int] = Query(None, ge=0, le=100),
    limit: int = Query(500, ge=1, le=1000, description="Max pins"),
    cursor=Depends(get_cursor),
):
    """Lightweight listing data for map pin rendering.

    Returns listing_id, lat, lon, price, beds, baths, safety_score,
    livability_score, neighborhood, and primary_photo_url. Optimized
    for volume — up to 1000 pins per viewport. Use /listings/{id} for
    full detail on click.

    Bounding box parameters are optional. Without them, returns all
    active listings up to the limit.
    """
    from app.services.listing_queries import _rows_to_dicts

    conditions = ["l.is_current = TRUE", "l.lat IS NOT NULL"]
    params = []

    if min_lat is not None and max_lat is not None:
        conditions.append("l.lat BETWEEN %s AND %s")
        params.extend([min_lat, max_lat])
    if min_lon is not None and max_lon is not None:
        conditions.append("l.lon BETWEEN %s AND %s")
        params.extend([min_lon, max_lon])
    if min_price is not None:
        conditions.append("l.price >= %s")
        params.append(min_price)
    if max_price is not None:
        conditions.append("l.price <= %s")
        params.append(max_price)
    if beds_min is not None:
        conditions.append("l.beds >= %s")
        params.append(beds_min)
    if min_safety_score is not None:
        conditions.append("ls.safety_score >= %s")
        params.append(min_safety_score)

    where = " AND ".join(conditions)

    sql = f"""
        SELECT
            l.listing_id, l.lat, l.lon,
            l.price, l.beds, l.baths, l.sqft,
            l.street, l.neighborhood, l.city,
            l.primary_photo_url, l.source, l.source_url,
            ls.safety_score, ls.livability_score
        FROM RAW.LISTINGS l
        LEFT JOIN SCORECARDS.LISTING_SUMMARY ls
            ON l.listing_id = ls.listing_id
        WHERE {where}
        ORDER BY ls.safety_score DESC NULLS LAST
        LIMIT %s
    """
    params.append(limit)

    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    except Exception as e:
        logger.error("map_listings_failed", error=str(e)[:200])
        raise HTTPException(status_code=500, detail="Failed to load map data")

    data = _rows_to_dicts(cursor, rows)
    return {"success": True, "data": data, "total_count": len(data)}


# -- GET /map/crimes -------------------------------------------------------

@router.get(
    "/crimes",
    summary="Crime incidents for heatmap layer",
)
def map_crimes(
    lat: float = Query(..., description="Center latitude"),
    lon: float = Query(..., description="Center longitude"),
    radius_m: int = Query(1000, ge=100, le=5000, description="Search radius meters"),
    window_days: int = Query(30, ge=1, le=365, description="Lookback days"),
    severity: Optional[str] = Query(None, description="violent|property|minor"),
    limit: int = Query(500, ge=1, le=2000, description="Max incidents"),
    cursor=Depends(get_cursor),
):
    """Crime incidents near a point for heatmap rendering.

    Returns incident_id, lat, lon, severity, offense_description,
    occurred_on_date, hour, shooting flag, and distance_m from center.
    Use severity filter for focused layers (e.g. violent-only heatmap).
    """
    from app.services.crime_queries import crimes_near_point

    result = crimes_near_point(
        cursor, lat, lon,
        radius_m=radius_m, window_days=window_days,
        severity=severity, limit=limit,
    )

    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)

    return result.to_dict()


# -- GET /map/transit ------------------------------------------------------

@router.get(
    "/transit",
    summary="MBTA transit stops for map layer",
)
def map_transit(
    min_lat: Optional[float] = Query(None),
    max_lat: Optional[float] = Query(None),
    min_lon: Optional[float] = Query(None),
    max_lon: Optional[float] = Query(None),
    route_type: Optional[int] = Query(
        None, description="0=Light Rail, 1=Heavy Rail, 2=Commuter Rail, 3=Bus",
    ),
    limit: int = Query(500, ge=1, le=2000),
    cursor=Depends(get_cursor),
):
    """MBTA transit stops with route names and types.

    Returns stop_id, stop_name, lat, lon, municipality,
    wheelchair_boarding, route_ids, route_names, route_types.
    Filter by bounding box for viewport queries or by route_type
    for layer toggling (subway only, bus only, etc.).
    """
    from app.services.listing_queries import _rows_to_dicts

    conditions = ["t.lat IS NOT NULL"]
    params = []

    if min_lat is not None and max_lat is not None:
        conditions.append("t.lat BETWEEN %s AND %s")
        params.extend([min_lat, max_lat])
    if min_lon is not None and max_lon is not None:
        conditions.append("t.lon BETWEEN %s AND %s")
        params.extend([min_lon, max_lon])
    if route_type is not None:
        conditions.append("ARRAY_CONTAINS(%s::VARIANT, t.route_types)")
        params.append(route_type)

    where = " AND ".join(conditions)

    sql = f"""
        SELECT
            t.stop_id, t.stop_name, t.lat, t.lon,
            t.municipality, t.wheelchair_boarding,
            t.route_ids, t.route_names, t.route_types
        FROM RAW.TRANSIT_STOPS t
        WHERE {where}
        LIMIT %s
    """
    params.append(limit)

    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    except Exception as e:
        logger.error("map_transit_failed", error=str(e)[:200])
        raise HTTPException(status_code=500, detail="Failed to load transit data")

    data = _rows_to_dicts(cursor, rows)
    return {"success": True, "data": data, "total_count": len(data)}


# -- GET /map/amenities ----------------------------------------------------

@router.get(
    "/amenities",
    summary="Amenities near a point for map markers",
)
def map_amenities(
    lat: float = Query(..., description="Center latitude"),
    lon: float = Query(..., description="Center longitude"),
    radius_m: int = Query(800, ge=100, le=3000, description="Search radius"),
    subcategory: Optional[str] = Query(None, description="e.g. pharmacy, cafe, park"),
    category: Optional[str] = Query(None, description="e.g. amenity, shop, leisure"),
    name_contains: Optional[str] = Query(None, description="Name text search"),
    limit: int = Query(100, ge=1, le=500, description="Max results"),
    cursor=Depends(get_cursor),
):
    """Stored amenities near a point with distance.

    Returns osm_id, name, category, subcategory, lat, lon, address,
    opening_hours, website, phone, brand, tags, and distance_m.
    Queries the pre-indexed RAW.AMENITIES table (35 subcategory types).
    For exotic venue types not in the index, the chat agent uses live
    Overpass queries instead.
    """
    from app.services.amenity_lookup import search_stored_amenities

    result = search_stored_amenities(
        cursor, lat, lon,
        subcategory=subcategory, category=category,
        name_contains=name_contains,
        radius_m=radius_m, limit=limit,
    )

    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)

    return result.to_dict()


# -- GET /map/routes -------------------------------------------------------

@router.get(
    "/routes",
    summary="User's commute route polylines for map rendering",
)
def map_routes(
    listing_id: Optional[str] = Query(None, description="Filter to one listing"),
    user_id: str = Depends(get_current_user),
    cursor=Depends(get_cursor),
):
    """Configured commute routes with waypoints for polyline rendering.

    Returns route_id, listing_id, dest_label, dest_address, dest_lat,
    dest_lon, departure_hour, travel_mode, duration_min, distance_text,
    transit_lines, waypoints (array of {lat, lon}), waypoint_scores
    (per-waypoint safety scores for gradient coloring), is_active,
    and computed_at.
    """
    from app.services.listing_queries import get_configured_routes

    result = get_configured_routes(cursor, user_id, listing_id=listing_id)

    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)

    return result.to_dict()