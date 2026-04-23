"""MCP read tools — direct data access without agent overhead.

Each tool function is module-level and importable for testing.
register_read_tools() wraps them with @mcp.tool().

IMPORTANT: _get_cursor() is called INSIDE the try block so that
connection failures return {"success": false} instead of crashing.
"""

from __future__ import annotations

import json
from typing import Optional

import structlog

logger = structlog.get_logger()


def _get_cursor():
    """Fresh Snowflake cursor. Closes connection on cursor.close()."""
    from app.core.database import _connect
    conn = _connect()
    cursor = conn.cursor()
    original_close = cursor.close
    def _close_both():
        try:
            original_close()
        finally:
            try:
                conn.close()
            except Exception:
                pass
    cursor.close = _close_both
    return cursor


def search_listings(
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    beds_min: Optional[int] = None,
    beds_max: Optional[int] = None,
    neighborhood: Optional[str] = None,
    city: Optional[str] = None,
    min_safety_score: Optional[int] = None,
    sort_by: Optional[str] = None,
    limit: int = 20,
) -> str:
    """Search Boston apartment listings with safety and livability scores."""
    cursor = None
    try:
        from app.services.listing_queries import search_listings as _search
        cursor = _get_cursor()
        result = _search(
            cursor,
            min_price=min_price, max_price=max_price,
            beds_min=beds_min, beds_max=beds_max,
            neighborhood=neighborhood, city=city,
            min_safety_score=min_safety_score,
            sort_by=sort_by, limit=limit,
        )
        return json.dumps(result.to_dict(), default=str)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)[:300]})
    finally:
        if cursor:
            cursor.close()


def get_listing(listing_id: str) -> str:
    """Get full detail for one listing with scoring metadata."""
    cursor = None
    try:
        from app.services.listing_queries import get_listing_detail
        cursor = _get_cursor()
        result = get_listing_detail(cursor, listing_id)
        return json.dumps(result.to_dict(), default=str)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)[:300]})
    finally:
        if cursor:
            cursor.close()


def get_safety(
    lat: float,
    lon: float,
    radius_m: int = 500,
    window_days: int = 30,
    severity: Optional[str] = None,
) -> str:
    """Get crime incidents near a location."""
    cursor = None
    try:
        from app.services.crime_queries import crimes_near_point
        cursor = _get_cursor()
        result = crimes_near_point(
            cursor, lat, lon,
            radius_m=radius_m, window_days=window_days,
            severity=severity,
        )
        return json.dumps(result.to_dict(), default=str)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)[:300]})
    finally:
        if cursor:
            cursor.close()


def get_neighborhood(
    neighborhood: str,
    window_days: int = 30,
) -> str:
    """Aggregate crime stats for a Boston neighborhood."""
    cursor = None
    try:
        from app.services.crime_queries import neighborhood_stats
        cursor = _get_cursor()
        result = neighborhood_stats(cursor, neighborhood, window_days=window_days)
        return json.dumps(result.to_dict(), default=str)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)[:300]})
    finally:
        if cursor:
            cursor.close()


def get_amenities(
    lat: float,
    lon: float,
    subcategory: Optional[str] = None,
    name_contains: Optional[str] = None,
    radius_m: int = 800,
    limit: int = 20,
) -> str:
    """Find amenities near a location."""
    cursor = None
    try:
        from app.services.amenity_lookup import search_stored_amenities
        cursor = _get_cursor()
        result = search_stored_amenities(
            cursor, lat, lon,
            subcategory=subcategory, name_contains=name_contains,
            radius_m=radius_m, limit=limit,
        )
        return json.dumps(result.to_dict(), default=str)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)[:300]})
    finally:
        if cursor:
            cursor.close()


def register_read_tools(mcp):
    """Register all read tools on a FastMCP server instance."""
    mcp.tool()(search_listings)
    mcp.tool()(get_listing)
    mcp.tool()(get_safety)
    mcp.tool()(get_neighborhood)
    mcp.tool()(get_amenities)