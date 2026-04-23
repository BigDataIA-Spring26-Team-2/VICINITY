"""Users router — authenticated read endpoints for user data.

Serves the frontend sidebar/dashboard directly. These are thin wrappers
around existing service functions — no business logic here, just HTTP
translation (auth check, cursor injection, response formatting).

All endpoints require a valid JWT in the Authorization header.

Endpoints:
    GET /users/profile              — current user's active search profile
    GET /users/bookmarks            — current user's bookmarked listings
    GET /users/routes               — current user's configured commute routes
    GET /users/routes?listing_id=X  — routes for a specific listing
"""

from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Query

from app.core.auth import get_current_user
from app.core.database import get_cursor

logger = structlog.get_logger()

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /users/profile
# ---------------------------------------------------------------------------

@router.get(
    "/profile",
    summary="Get active search profile",
)
def get_profile(
    user_id: str = Depends(get_current_user),
    cursor=Depends(get_cursor),
):
    """Return the user's active search profile.

    Includes budget, bedrooms, work address, preference tags, etc.
    Returns empty data (not 404) if the user hasn't created a profile yet.
    """
    from app.services.user_data import get_active_profile

    result = get_active_profile(cursor, user_id)
    return result.to_dict()


# ---------------------------------------------------------------------------
# GET /users/bookmarks
# ---------------------------------------------------------------------------

@router.get(
    "/bookmarks",
    summary="Get bookmarked listings",
)
def get_bookmarks(
    user_id: str = Depends(get_current_user),
    cursor=Depends(get_cursor),
):
    """Return the user's active bookmarked listings with scores.

    Each bookmark includes the full listing detail from LISTING_SUMMARY
    (price, beds, safety_score, livability_score, etc.) plus the
    watch_end timestamp. Expired watch periods are flagged in warnings.
    """
    from app.services.listing_queries import get_bookmarked_listings

    result = get_bookmarked_listings(cursor, user_id)
    return result.to_dict()


# ---------------------------------------------------------------------------
# GET /users/routes
# ---------------------------------------------------------------------------

@router.get(
    "/routes",
    summary="Get configured commute routes",
)
def get_routes(
    listing_id: Optional[str] = Query(
        None, description="Filter routes to a specific listing",
    ),
    user_id: str = Depends(get_current_user),
    cursor=Depends(get_cursor),
):
    """Return the user's configured commute routes.

    Includes waypoints, transit lines, duration, and waypoint safety
    scores for map rendering. Optionally filtered to a specific listing.
    """
    from app.services.listing_queries import get_configured_routes

    result = get_configured_routes(cursor, user_id, listing_id=listing_id)
    return result.to_dict()