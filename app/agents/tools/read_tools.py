"""Chat Agent read tools -- bound to LangGraph via @tool decorator.

Six tools covering all read paths. The LLM sees these tool schemas
(name + docstring + typed params) and decides which to call.

Cursor lifecycle: tools receive a cursor_provider callable that returns
a fresh cursor per invocation. The graph node injects this at bind time.
This keeps tools stateless and cursor management in one place.

Tool -> Service mapping:
    query_listings    -> listing_queries.*
    query_safety      -> crime_queries.* + complaint_queries.*
    search_narratives -> pinecone_search.search_narratives
    lookup_amenities  -> amenity_lookup.*
    report_issue      -> url_health.flag_url
    run_sql           -> sql_freeform.execute_freeform
"""

from __future__ import annotations

import json
from typing import Callable, Optional

from langchain_core.tools import tool

# Cursor provider is injected at graph construction time.
# Each tool calls _get_cursor() to get a fresh Snowflake cursor.
_cursor_provider: Optional[Callable] = None


def set_cursor_provider(provider: Callable):
    """Set the cursor factory. Called once at graph build time."""
    global _cursor_provider
    _cursor_provider = provider


def _get_cursor():
    if _cursor_provider is None:
        raise RuntimeError("Cursor provider not set. Call set_cursor_provider() first.")
    return _cursor_provider()


# =====================================================================
# Tool 1: query_listings
# =====================================================================

@tool
def query_listings(
    action: str,
    listing_id: Optional[str] = None,
    listing_ids: Optional[list[str]] = None,
    user_id: Optional[str] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    beds_min: Optional[int] = None,
    beds_max: Optional[int] = None,
    baths_min: Optional[int] = None,
    city: Optional[str] = None,
    neighborhood: Optional[str] = None,
    zip_code: Optional[str] = None,
    min_sqft: Optional[int] = None,
    min_safety_score: Optional[int] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    limit: Optional[int] = None,
    days: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    route_id: Optional[str] = None,
    source_url: Optional[str] = None,
) -> str:
    """Query listing data from the Vicinity database.

    action must be one of:
      "detail"           - Full detail for one listing. Requires listing_id.
      "search"           - Search listings by filters (price, beds, neighborhood, etc).
      "compare"          - Side-by-side comparison. Requires listing_ids (list of 2-10 IDs).
      "scorecard"        - Daily score history for a listing. Requires listing_id. Optional days/start_date/end_date.
      "route_scorecard"  - Daily route safety history. Requires route_id. Optional days/start_date/end_date.
      "bookmarks"        - User's bookmarked listings. Requires user_id.
      "routes"           - User's configured commute routes. Requires user_id. Optional listing_id to filter.
      "by_url"           - Find listing by source URL. Requires source_url.

    Returns JSON with: success, data (list of records), total_count, warnings.
    """
    from app.services import listing_queries as lq

    cursor = _get_cursor()
    try:
        if action == "detail":
            result = lq.get_listing_detail(cursor, listing_id)
        elif action == "search":
            result = lq.search_listings(
                cursor,
                min_price=min_price, max_price=max_price,
                beds_min=beds_min, beds_max=beds_max,
                baths_min=baths_min, city=city,
                neighborhood=neighborhood, zip_code=zip_code,
                min_sqft=min_sqft, min_safety_score=min_safety_score,
                sort_by=sort_by, sort_order=sort_order, limit=limit,
            )
        elif action == "compare":
            result = lq.compare_listings(cursor, listing_ids or [])
        elif action == "scorecard":
            result = lq.scorecard_history(
                cursor, listing_id,
                days=days, start_date=start_date, end_date=end_date,
            )
        elif action == "route_scorecard":
            result = lq.route_scorecard_history(
                cursor, route_id,
                days=days, start_date=start_date, end_date=end_date,
            )
        elif action == "bookmarks":
            result = lq.get_bookmarked_listings(cursor, user_id)
        elif action == "routes":
            result = lq.get_configured_routes(
                cursor, user_id, listing_id=listing_id,
            )
        elif action == "by_url":
            result = lq.get_listing_by_url(cursor, source_url)
        else:
            return json.dumps({"success": False, "error": f"Unknown action: {action}"})

        return json.dumps(result.to_dict(), default=str)
    finally:
        cursor.close()


# =====================================================================
# Tool 2: query_safety
# =====================================================================

@tool
def query_safety(
    action: str,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    radius_m: Optional[int] = None,
    window_days: Optional[int] = None,
    severity: Optional[str] = None,
    hour_min: Optional[int] = None,
    hour_max: Optional[int] = None,
    shooting_only: bool = False,
    category: Optional[str] = None,
    neighborhood: Optional[str] = None,
    waypoints: Optional[list[dict]] = None,
    buffer_m: Optional[int] = None,
    months_back: Optional[int] = None,
    limit: Optional[int] = None,
) -> str:
    """Query crime and complaint data near a location.

    action must be one of:
      "crimes"           - Crime incidents near a point. Requires lat, lon.
                           Optional: radius_m, window_days, severity (violent|property|minor),
                           hour_min/hour_max (24h, handles midnight wrap), shooting_only, limit.
      "crimes_corridor"  - Crimes along a route. Requires waypoints (list of {lat, lon} dicts).
                           Optional: buffer_m, window_days, severity, limit.
      "hourly"           - 24-hour crime distribution at a point. Requires lat, lon.
                           Optional: radius_m, months_back.
      "neighborhood"     - Aggregate crime stats for a neighborhood. Requires neighborhood name.
                           Optional: window_days.
      "complaints"       - 311 complaints near a point. Requires lat, lon.
                           Optional: radius_m, window_days, category
                           (noise|pest|rodent|road|sanitation|heat|housing|other), limit.
      "complaint_summary" - Category breakdown of complaints near a point. Requires lat, lon.
                           Optional: radius_m, window_days.

    Returns JSON with: success, data (list of records), total_count, warnings.
    """
    from app.services import crime_queries as cq
    from app.services import complaint_queries as comp

    cursor = _get_cursor()
    try:
        if action == "crimes":
            result = cq.crimes_near_point(
                cursor, lat, lon,
                radius_m=radius_m, window_days=window_days,
                severity=severity, hour_min=hour_min, hour_max=hour_max,
                shooting_only=shooting_only, limit=limit,
            )
        elif action == "crimes_corridor":
            result = cq.crimes_near_corridor(
                cursor, waypoints or [],
                buffer_m=buffer_m, window_days=window_days,
                severity=severity, limit=limit,
            )
        elif action == "hourly":
            result = cq.hourly_distribution(
                cursor, lat, lon,
                radius_m=radius_m, months_back=months_back,
            )
        elif action == "neighborhood":
            result = cq.neighborhood_stats(
                cursor, neighborhood, window_days=window_days,
            )
        elif action == "complaints":
            result = comp.complaints_near_point(
                cursor, lat, lon,
                radius_m=radius_m, window_days=window_days,
                category=category, limit=limit,
            )
        elif action == "complaint_summary":
            result = comp.complaint_summary(
                cursor, lat, lon,
                radius_m=radius_m, window_days=window_days,
            )
        else:
            return json.dumps({"success": False, "error": f"Unknown action: {action}"})

        return json.dumps(result.to_dict(), default=str)
    finally:
        cursor.close()


# =====================================================================
# Tool 3: search_narratives
# =====================================================================

@tool
def search_narratives(
    question: str,
    source: Optional[str] = None,
    preference_tag: Optional[str] = None,
    neighborhoods: Optional[list[str]] = None,
    sentiment: Optional[str] = None,
    top_k: Optional[int] = None,
    skip_hyde: bool = False,
) -> str:
    """Search Reddit posts, news articles, crime descriptions, and event details
    for narrative evidence about Boston neighborhoods and listings.

    Use this for open-ended questions the user asks about neighborhood vibe,
    community sentiment, or specific incidents that numbers alone can't answer.
    Examples: "what do people say about Allston at night", "any recent news
    about safety in South End", "what's the vibe like near this listing".

    Args:
        question: Natural language question to search for.
        source: Filter by data source (crime, reddit, news, citizen, meetup, eventbrite).
        preference_tag: Filter by preference tag (safety, noise, korean_food, etc).
        neighborhoods: Filter to specific neighborhoods (e.g. ["Allston", "Brighton"]).
        sentiment: Filter by sentiment (positive, negative, mixed, neutral).
        top_k: Max results to return.
        skip_hyde: If true, embed the raw question instead of generating a
                   hypothetical document first. Use when the question is already
                   specific and factual rather than conversational.

    Returns JSON with matched narratives, each containing: signal_id, score,
    source, preference_tag, sentiment, neighborhoods, url.
    """
    from app.services.pinecone_search import search_narratives as _search

    filters = {}
    if source:
        filters["signal_source"] = source
    if preference_tag:
        filters["preference_tag"] = preference_tag
    if neighborhoods:
        filters["neighborhoods"] = neighborhoods
    if sentiment:
        filters["sentiment"] = sentiment

    result = _search(
        question,
        filters=filters if filters else None,
        top_k=top_k,
        skip_hyde=skip_hyde,
    )

    # Hydrate: fetch title, narrative, and evidence from Snowflake
    # for each matched signal. Pinecone only stores metadata for filtering;
    # the actual content lives in RAW.LIFESTYLE_SIGNALS.
    if result.success and result.data:
        signal_ids = [d["signal_id"] for d in result.data]
        try:
            cursor = _get_cursor()
            try:
                placeholders = ", ".join(["%s"] * len(signal_ids))
                cursor.execute(
                    f"SELECT signal_id, title, snippet_text, url, "
                    f"  classification_metadata "
                    f"FROM RAW.LIFESTYLE_SIGNALS "
                    f"WHERE signal_id IN ({placeholders})",
                    tuple(signal_ids),
                )
                rows = cursor.fetchall()
                cols = [desc[0].lower() for desc in cursor.description]
                hydrated = {row[0]: dict(zip(cols, row)) for row in rows}

                for item in result.data:
                    sid = item["signal_id"]
                    if sid in hydrated:
                        h = hydrated[sid]
                        item["title"] = h.get("title", "")
                        item["narrative"] = h.get("snippet_text", "")
                        item["url"] = h.get("url", item.get("url", ""))
                        # Parse classification_metadata for evidence + topics
                        raw_meta = h.get("classification_metadata")
                        if isinstance(raw_meta, str):
                            try:
                                parsed = json.loads(raw_meta)
                                item["evidence"] = parsed.get("evidence", [])
                                item["topics"] = parsed.get("topics", [])
                                item["neighborhoods_mentioned"] = parsed.get("neighborhoods_mentioned", [])
                                item["thread_quality"] = parsed.get("thread_quality", {})
                                item["discussion_date"] = parsed.get("discussion_date", "")
                                item["subreddit"] = parsed.get("subreddit", "")
                                item["post_score"] = parsed.get("post_score", 0)
                                item["num_comments"] = parsed.get("num_comments", 0)
                            except (json.JSONDecodeError, TypeError):
                                pass
            finally:
                cursor.close()
        except Exception as e:
            # Hydration failure is non-fatal — return Pinecone results without content
            import structlog
            structlog.get_logger().warning(
                "narrative_hydration_failed", error=str(e)[:200],
                signal_count=len(signal_ids),
            )

    return json.dumps(result.to_dict(), default=str)

# =====================================================================
# Tool 4: lookup_amenities
# =====================================================================

@tool
def lookup_amenities(
    lat: float,
    lon: float,
    subcategory: Optional[str] = None,
    category: Optional[str] = None,
    name_contains: Optional[str] = None,
    tags: Optional[dict] = None,
    radius_m: Optional[int] = None,
    limit: Optional[int] = None,
) -> str:
    """Find amenities and venues near a location.

    Two modes:
      Stored (default): Queries the Vicinity database of 35 amenity types
        already indexed for Boston. Use subcategory (pharmacy, cafe,
        fitness_centre, park, supermarket, library, etc), category
        (amenity, shop, leisure), or name_contains for text search.

      Live Overpass: Queries OpenStreetMap in real-time for specific
        venue types not in the stored database. Use tags dict with exact
        OSM key-value pairs, e.g. {"amenity": "restaurant", "cuisine": "korean"}.
        The tags come from the user's preference expansion in their profile.

    Args:
        lat, lon: Center point for the search.
        subcategory: Exact amenity type (e.g. "pharmacy", "cafe", "dog_park").
        category: OSM category (e.g. "amenity", "shop", "leisure").
        name_contains: Case-insensitive name search (e.g. "starbucks").
        tags: OSM tag dict for live Overpass query. If provided, uses live mode.
        radius_m: Search radius in meters (default 800).
        limit: Max results.

    Returns JSON with venues including: name, distance_m, address, opening_hours.
    """
    from app.services.amenity_lookup import (
        search_stored_amenities, search_overpass_live,
    )

    # Live Overpass path if tags provided
    if tags:
        result = search_overpass_live(
            lat, lon, tags=tags, radius_m=radius_m, limit=limit,
        )
        return json.dumps(result.to_dict(), default=str)

    # Stored path
    cursor = _get_cursor()
    try:
        result = search_stored_amenities(
            cursor, lat, lon,
            subcategory=subcategory, category=category,
            name_contains=name_contains,
            radius_m=radius_m, limit=limit,
        )
        return json.dumps(result.to_dict(), default=str)
    finally:
        cursor.close()


# =====================================================================
# Tool 5: report_issue
# =====================================================================

@tool
def report_issue(
    listing_id: Optional[str] = None,
    signal_id: Optional[str] = None,
    url: str = "",
    issue_type: str = "broken_url",
) -> str:
    """Report a broken URL or data quality issue.

    Call this when the user says a listing link doesn't work or
    data looks wrong. The system will validate the URL and update
    its status in the database.

    Args:
        listing_id: The listing with the broken URL (provide this OR signal_id).
        signal_id: The lifestyle signal with the broken URL.
        url: The URL that's broken.
        issue_type: Type of issue (broken_url, wrong_data, stale_listing).

    Returns JSON confirming the flag was recorded and whether the URL
    was auto-checked (alive or confirmed dead).
    """
    from app.services.url_health import flag_url

    if listing_id:
        table = "RAW.LISTINGS"
        record_id = listing_id
    elif signal_id:
        table = "RAW.LIFESTYLE_SIGNALS"
        record_id = signal_id
    else:
        return json.dumps({"success": False, "error": "Provide listing_id or signal_id"})

    cursor = _get_cursor()
    try:
        result = flag_url(cursor, table, record_id, url)
        return json.dumps(result.to_dict(), default=str)
    finally:
        cursor.close()


# =====================================================================
# Tool 6: run_sql
# =====================================================================

@tool
def run_sql(sql: str) -> str:
    """Execute a custom SQL query against the Vicinity database.

    USE THIS ONLY when the other tools don't cover the question.
    The query must be a SELECT statement. INSERT, UPDATE, DELETE,
    DROP and other mutations are blocked.

    The database schema includes these tables:
      RAW.LISTINGS - apartment listings (price, beds, baths, sqft, lat, lon, neighborhood)
      RAW.CRIME_INCIDENTS - crime records (offense_description, severity, hour, lat, lon, shooting)
      RAW.COMPLAINTS_311 - 311 complaints (category, open_dt, street, neighborhood)
      RAW.CITIZEN_INCIDENTS - real-time incidents (title, severity, is_nighttime, lat, lon)
      RAW.TRANSIT_STOPS - MBTA stops (stop_name, route_names, lat, lon)
      RAW.AMENITIES - OSM amenities (name, subcategory, lat, lon, opening_hours)
      RAW.LIFESTYLE_SIGNALS - Reddit/news/event signals (preference_tag, sentiment, snippet_text)
      SCORECARDS.LOCATION_SCORECARD - daily scores per listing (safety_score, livability_score)
      SCORECARDS.LISTING_SUMMARY - latest scores + listing details
      SCORECARDS.ROUTE_SCORECARD - daily route corridor safety scores
      USER_DATA.SEARCH_PROFILES - user preferences and budget
      USER_DATA.BOOKMARKED_LISTINGS - user's watched listings
      USER_DATA.CONFIGURED_ROUTES - user's commute routes with waypoints

    SPATIAL QUERIES: Use ST_DISTANCE(ST_MAKEPOINT(lon, lat), ST_MAKEPOINT(lon2, lat2))
    for distance in meters. ST_MAKEPOINT takes (longitude, latitude) - lon first.
    Always add a bounding box pre-filter before ST_DISTANCE for performance.

    TIME QUERIES: Use DATEADD(day, -N, CURRENT_DATE()) for relative dates.
    Crime hour is INT 0-23. For midnight-crossing ranges: (hour >= 22 OR hour <= 4).

    Args:
        sql: The SELECT query to execute. Will be validated and LIMIT enforced.

    Returns JSON with: success, data (query results), sql_executed (the actual SQL run),
    error (if query failed - use this to fix and retry).
    """
    from app.services.sql_freeform import execute_freeform

    cursor = _get_cursor()
    try:
        result = execute_freeform(cursor, sql)
        return json.dumps(result.to_dict(), default=str)
    finally:
        cursor.close()


# =====================================================================
# Tool list for binding
# =====================================================================

CHAT_AGENT_TOOLS = [
    query_listings,
    query_safety,
    search_narratives,
    lookup_amenities,
    report_issue,
    run_sql,
]