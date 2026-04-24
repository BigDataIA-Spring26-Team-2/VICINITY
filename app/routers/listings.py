"""Listings router — listing detail, narratives, search, compare, scorecard.

Owns its own SQL for the enriched detail and narratives endpoints; those
are frontend-only and don't go through the agent service layer. Search,
compare, and scorecard delegate to existing services that the agent also
uses, so the shape doesn't drift between HTTP and tool calls.

ROUTE ORDER matters: /compare and /search declared BEFORE /{listing_id}
so FastAPI doesn't match literal paths as IDs.
"""

from __future__ import annotations

import json
import time
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import get_current_user
from app.core.database import get_cursor
from app.services.listing_queries import (
    search_listings,
    compare_listings,
    scorecard_history,
)

logger = structlog.get_logger()
router = APIRouter()

_JSON_COLS = {
    "safety_metadata", "livability_metadata", "lifestyle_overlay",
    "nearest_stops", "nearby_amenities",
    "safety_trend", "price_history",
    "classification_metadata", "topics",
}


def _rows_to_dicts(cursor, rows: list) -> list[dict]:
    if not rows:
        return []
    cols = [d[0].lower() for d in cursor.description]
    out: list[dict] = []
    for row in rows:
        d: dict = {}
        for col, val in zip(cols, row):
            if isinstance(val, str) and col in _JSON_COLS:
                try:
                    d[col] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    d[col] = val
            else:
                d[col] = val
        out.append(d)
    return out


@router.get("/search", summary="Search listings with filters and scores")
def search(
    min_price: Optional[int] = Query(None, ge=0),
    max_price: Optional[int] = Query(None, ge=0),
    beds_min: Optional[int] = Query(None, ge=0),
    beds_max: Optional[int] = Query(None, ge=0),
    baths_min: Optional[int] = Query(None, ge=0),
    city: Optional[str] = Query(None, max_length=100),
    neighborhood: Optional[str] = Query(None, max_length=100),
    zip_code: Optional[str] = Query(None, max_length=10),
    min_sqft: Optional[int] = Query(None, ge=0),
    min_safety_score: Optional[int] = Query(None, ge=0, le=100),
    min_livability_score: Optional[int] = Query(None, ge=0, le=100),
    has_photo: Optional[bool] = Query(None),
    sort_by: Optional[str] = Query(None),
    sort_order: Optional[str] = Query(None, pattern="^(asc|desc)$"),
    limit: Optional[int] = Query(None, ge=1, le=50),
    offset: int = Query(0, ge=0),
    cursor=Depends(get_cursor),
):
    r = search_listings(
        cursor,
        min_price=min_price, max_price=max_price,
        beds_min=beds_min, beds_max=beds_max, baths_min=baths_min,
        city=city, neighborhood=neighborhood, zip_code=zip_code,
        min_sqft=min_sqft,
        min_safety_score=min_safety_score,
        min_livability_score=min_livability_score,
        has_photo=has_photo,
        sort_by=sort_by, sort_order=sort_order,
        limit=limit, offset=offset,
    )
    if not r.success:
        raise HTTPException(status_code=500, detail=r.error)
    return r.to_dict()


@router.get("/compare", summary="Side-by-side listing comparison")
def compare(
    ids: str = Query(..., description="Comma-separated listing IDs (2-10)"),
    cursor=Depends(get_cursor),
):
    listing_ids = [lid.strip() for lid in ids.split(",") if lid.strip()]
    if len(listing_ids) < 2:
        raise HTTPException(status_code=400, detail="Provide at least 2 listing IDs")
    if len(listing_ids) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 listings per comparison")
    r = compare_listings(cursor, listing_ids)
    if not r.success:
        raise HTTPException(status_code=500, detail=r.error)
    return r.to_dict()


@router.get("/{listing_id}", summary="Full detail + lifestyle_overlay")
def detail(listing_id: str, cursor=Depends(get_cursor)):
    """Full listing detail — LISTINGS + LISTING_SUMMARY + lifestyle_overlay.

    The scoring pipeline writes `scoring_metadata:lifestyle_overlay`
    (matched Reddit/news signals per neighborhood) to LOCATION_SCORECARD
    but the LISTING_SUMMARY MERGE never copies it. This endpoint pulls
    the most recent LOCATION_SCORECARD row's overlay via a correlated
    subquery so the detail panel can render neighborhood sentiment.
    """
    log = logger.bind(listing_id=listing_id)

    # Snowflake rejects correlated scalar subqueries that return VARIANT
    # ("Unsupported subquery type cannot be evaluated"). Use a CTE pinned
    # to this listing_id instead — single row, no correlation.
    sql = """
        WITH latest_overlay AS (
            SELECT
                listing_id,
                scoring_metadata:lifestyle_overlay AS lifestyle_overlay
            FROM SCORECARDS.LOCATION_SCORECARD
            WHERE listing_id = %s
            ORDER BY score_date DESC
            LIMIT 1
        )
        SELECT
            l.listing_id, l.source, l.source_url,
            l.price, l.beds, l.baths, l.sqft,
            l.street, l.unit, l.city, l.zip_code, l.neighborhood,
            l.lat, l.lon,
            l.primary_photo_url, l.mls_id, l.mls_status,
            l.days_on_mls, l.agent_name, l.style, l.list_date,
            l.is_current, l.first_seen_at, l.last_seen_at,
            l.description_text,
            ls.safety_score, ls.livability_score,
            ls.is_active AS summary_is_active,
            ls.nearest_stops, ls.last_scored_at,
            ls.safety_metadata, ls.livability_metadata,
            lo.lifestyle_overlay,
            COALESCE(l.url_status, 'active') AS url_status
        FROM RAW.LISTINGS l
        LEFT JOIN SCORECARDS.LISTING_SUMMARY ls
            ON l.listing_id = ls.listing_id
        LEFT JOIN latest_overlay lo
            ON lo.listing_id = l.listing_id
        WHERE l.listing_id = %s
    """

    t0 = time.perf_counter()
    try:
        cursor.execute(sql, (listing_id, listing_id))
        rows = cursor.fetchall()
    except Exception as e:
        log.error("listing_detail_failed", error=str(e)[:200])
        raise HTTPException(status_code=500, detail="Listing query failed")

    data = _rows_to_dicts(cursor, rows)
    if not data:
        raise HTTPException(status_code=404, detail="Listing not found")

    ms = int((time.perf_counter() - t0) * 1000)
    warnings: list[str] = []
    if data[0].get("url_status") == "flagged":
        warnings.append("Source URL flagged as potentially broken.")

    log.info("listing_detail_ok", ms=ms)

    return {
        "success": True,
        "query_type": "listing_detail",
        "data": data,
        "total_count": 1,
        "duration_ms": ms,
        "warnings": warnings,
    }


@router.get(
    "/{listing_id}/narratives",
    summary="Reddit / news / citizen narratives matched to this listing's neighborhood",
)
def narratives(
    listing_id: str,
    limit: int = Query(15, ge=1, le=50),
    min_relevance: int = Query(0, ge=0, le=100),
    cursor=Depends(get_cursor),
):
    """LIFESTYLE_SIGNALS matched to this listing's neighborhood(s).

    Compound names ("West End/Beacon Hill") match both. Rows classified
    with matching `classification_metadata:neighborhoods` are preferred;
    older rows without that tag fall back to title/snippet text match.

    Ordered: negative sentiment first (those matter more for housing
    decisions), then relevance, then recency.
    """
    log = logger.bind(listing_id=listing_id)

    try:
        cursor.execute(
            "SELECT neighborhood FROM RAW.LISTINGS WHERE listing_id = %s",
            (listing_id,),
        )
        row = cursor.fetchone()
    except Exception as e:
        log.error("narratives_lookup_failed", error=str(e)[:200])
        raise HTTPException(status_code=500, detail="Listing lookup failed")

    if not row:
        raise HTTPException(status_code=404, detail="Listing not found")

    neighborhood = row[0]
    hoods: list[str] = []
    if neighborhood:
        hoods = [h.strip() for h in neighborhood.split("/") if h.strip()]

    if not hoods:
        return {
            "success": True,
            "query_type": "listing_narratives",
            "data": [],
            "total_count": 0,
            "duration_ms": 0,
            "warnings": ["Listing has no neighborhood; no narratives matched."],
        }

    text_clauses = " OR ".join(
        ["title ILIKE %s OR snippet_text ILIKE %s"] * len(hoods)
    )
    text_params: list[str] = []
    for h in hoods:
        p = f"%{h}%"
        text_params.extend([p, p])

    hood_array_sql = "ARRAY_CONSTRUCT(" + ", ".join(["%s"] * len(hoods)) + ")"
    array_params = list(hoods)

    sql = (
        "SELECT "
        "  signal_id, signal_source, preference_tag, "
        "  title, snippet_text, url, "
        "  sentiment, relevance_score, fetched_at, raw_thread_text, "
        "  classification_metadata:subreddit::STRING       AS subreddit, "
        "  classification_metadata:post_score::INT         AS post_score, "
        "  classification_metadata:num_comments::INT       AS num_comments, "
        "  classification_metadata:discussion_date::STRING AS discussion_date, "
        "  classification_metadata:topics                  AS topics "
        "FROM RAW.LIFESTYLE_SIGNALS "
        "WHERE ( "
        f"  ARRAYS_OVERLAP(classification_metadata:neighborhoods::ARRAY, {hood_array_sql}) "
        f"  OR ({text_clauses}) "
        ") "
        "AND relevance_score >= %s "
        "AND COALESCE(url_status, 'active') = 'active' "
        "ORDER BY "
        "  CASE sentiment WHEN 'negative' THEN 3 WHEN 'mixed' THEN 2 ELSE 1 END DESC, "
        "  relevance_score DESC NULLS LAST, "
        "  fetched_at DESC "
        "LIMIT %s"
    )
    params = array_params + text_params + [int(min_relevance), int(limit)]

    t0 = time.perf_counter()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    except Exception as e:
        log.error("narratives_query_failed", error=str(e)[:200])
        raise HTTPException(status_code=500, detail="Narratives query failed")

    data = _rows_to_dicts(cursor, rows)

    for d in data:
        txt = d.get("raw_thread_text")
        if isinstance(txt, str) and len(txt) > 2000:
            d["raw_thread_text"] = txt[:2000] + "…"

    ms = int((time.perf_counter() - t0) * 1000)
    log.info("narratives_ok", hoods=len(hoods), results=len(data), ms=ms)

    return {
        "success": True,
        "query_type": "listing_narratives",
        "data": data,
        "total_count": len(data),
        "duration_ms": ms,
    }


@router.get("/{listing_id}/scorecard", summary="Daily score history for charts")
def scorecard(
    listing_id: str,
    days: Optional[int] = Query(None, ge=1, le=90),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    user_id: str = Depends(get_current_user),
    cursor=Depends(get_cursor),
):
    r = scorecard_history(cursor, listing_id, days=days, start_date=start_date, end_date=end_date)
    if not r.success:
        raise HTTPException(status_code=500, detail=r.error)
    return r.to_dict()