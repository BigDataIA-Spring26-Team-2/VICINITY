"""Scorecards router — route corridor safety time series.

Route scorecards don't nest under /listings (they're per-route, not
per-listing), so they live here. Listing scorecards are served by
/listings/{listing_id}/scorecard.

Endpoints:
    GET /scorecards/route/{route_id}  — route corridor safety time series
"""

from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import get_current_user
from app.core.database import get_cursor

logger = structlog.get_logger()

router = APIRouter()


@router.get(
    "/route/{route_id}",
    summary="Route corridor safety time series",
)
def route_scorecard(
    route_id: str,
    days: Optional[int] = Query(None, ge=1, le=90, description="Lookback days"),
    start_date: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="End date YYYY-MM-DD"),
    user_id: str = Depends(get_current_user),
    cursor=Depends(get_cursor),
):
    """Daily ROUTE_SCORECARD rows for a commute corridor.

    One row per scored day, ordered chronologically. Each row contains:
    route_id, listing_id, score_date, crime_count, violent_count,
    shooting_count, crimes_at_dep_hour (incidents during the user's
    departure window), citizen_incidents, citizen_nighttime,
    scoring_metadata (VARIANT with corridor buffer, waypoint count,
    per-severity breakdown), and pipeline_run_id.

    Date range: provide days for relative lookback, or start_date/end_date
    for absolute range. Default is 14 days.
    """
    from app.services.listing_queries import route_scorecard_history

    result = route_scorecard_history(
        cursor, route_id,
        days=days, start_date=start_date, end_date=end_date,
    )

    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)

    return result.to_dict()