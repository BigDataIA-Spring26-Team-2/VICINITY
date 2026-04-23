"""Listings router — public and authenticated listing endpoints.

Serves the frontend map, listing cards, detail views, and comparison
tables. All endpoints are thin HTTP wrappers around existing service
functions.

ROUTE ORDER: /compare and /search are defined BEFORE /{listing_id}
so FastAPI doesn't match "compare" as a listing_id.

Public endpoints (no auth required):
    GET /listings/search        — filtered search with scores
    GET /listings/compare       — side-by-side comparison
    GET /listings/{listing_id}  — full detail with scoring metadata

Authenticated endpoints:
    GET /listings/{listing_id}/scorecard  — daily score history (charts)
"""

from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.auth import get_optional_user, get_current_user
from app.core.database import get_cursor
from app.services.listing_queries import (
    search_listings,
    get_listing_detail,
    compare_listings,
    scorecard_history,
)

logger = structlog.get_logger()

router = APIRouter()


# -- GET /listings/search --------------------------------------------------

@router.get(
    "/search",
    summary="Search listings with filters and scores",
)
def search(
    min_price: Optional[int] = Query(None, ge=0, description="Minimum price"),
    max_price: Optional[int] = Query(None, ge=0, description="Maximum price"),
    beds_min: Optional[int] = Query(None, ge=0, description="Minimum bedrooms"),
    beds_max: Optional[int] = Query(None, ge=0, description="Maximum bedrooms"),
    baths_min: Optional[int] = Query(None, ge=0, description="Minimum bathrooms"),
    city: Optional[str] = Query(None, max_length=100, description="City filter"),
    neighborhood: Optional[str] = Query(None, max_length=100, description="Neighborhood"),
    zip_code: Optional[str] = Query(None, max_length=10, description="Zip code"),
    min_sqft: Optional[int] = Query(None, ge=0, description="Minimum sqft"),
    min_safety_score: Optional[int] = Query(None, ge=0, le=100, description="Min safety percentile"),
    min_livability_score: Optional[int] = Query(None, ge=0, le=100, description="Min livability"),
    has_photo: Optional[bool] = Query(None, description="Only listings with photos"),
    sort_by: Optional[str] = Query(None, description="Sort field"),
    sort_order: Optional[str] = Query(None, pattern="^(asc|desc)$", description="Sort direction"),
    limit: Optional[int] = Query(None, ge=1, le=50, description="Max results"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    cursor=Depends(get_cursor),
):
    """Search active listings with safety and livability scores."""
    result = search_listings(
        cursor,
        min_price=min_price, max_price=max_price,
        beds_min=beds_min, beds_max=beds_max,
        baths_min=baths_min, city=city,
        neighborhood=neighborhood, zip_code=zip_code,
        min_sqft=min_sqft,
        min_safety_score=min_safety_score,
        min_livability_score=min_livability_score,
        has_photo=has_photo,
        sort_by=sort_by, sort_order=sort_order,
        limit=limit, offset=offset,
    )

    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)

    return result.to_dict()


# -- GET /listings/compare (BEFORE /{listing_id}) --------------------------

@router.get(
    "/compare",
    summary="Side-by-side listing comparison",
)
def compare(
    ids: str = Query(
        ...,
        description="Comma-separated listing IDs (2-10)",
    ),
    cursor=Depends(get_cursor),
):
    """Compare 2-10 listings side-by-side with latest scorecard data."""
    listing_ids = [lid.strip() for lid in ids.split(",") if lid.strip()]

    if len(listing_ids) < 2:
        raise HTTPException(status_code=400, detail="Provide at least 2 listing IDs")
    if len(listing_ids) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 listings per comparison")

    result = compare_listings(cursor, listing_ids)

    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)

    return result.to_dict()


# -- GET /listings/{listing_id} --------------------------------------------

@router.get(
    "/{listing_id}",
    summary="Full listing detail with scoring metadata",
)
def detail(
    listing_id: str,
    cursor=Depends(get_cursor),
):
    """Full listing detail joined with LISTING_SUMMARY."""
    result = get_listing_detail(cursor, listing_id)

    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)

    if not result.data:
        raise HTTPException(status_code=404, detail="Listing not found")

    return result.to_dict()


# -- GET /listings/{listing_id}/scorecard ----------------------------------

@router.get(
    "/{listing_id}/scorecard",
    summary="Daily score history for charts",
)
def scorecard(
    listing_id: str,
    days: Optional[int] = Query(None, ge=1, le=90, description="Lookback days"),
    start_date: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="End date YYYY-MM-DD"),
    user_id: str = Depends(get_current_user),
    cursor=Depends(get_cursor),
):
    """Daily LOCATION_SCORECARD time series for a listing."""
    result = scorecard_history(
        cursor, listing_id,
        days=days, start_date=start_date, end_date=end_date,
    )

    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)

    return result.to_dict()