"""Listing query service -- parameterized read access to listings and scorecards.

Provides the Chat Agent's primary structured data path. Every function
takes a Snowflake cursor + typed parameters, returns dicts ready for
agent consumption. No HTTP, no agent state -- pure data access.

Query types:
    get_listing_detail     -- full detail for one listing (LISTINGS + LISTING_SUMMARY)
    search_listings        -- filtered search with sort/pagination
    compare_listings       -- side-by-side metrics for 2-N listings
    scorecard_history      -- LOCATION_SCORECARD rows over date range
    route_scorecard_history -- ROUTE_SCORECARD rows over date range
    get_listing_by_url     -- lookup by source_url (for URL validation flow)

All queries respect url_filtering config: confirmed_dead listings are
excluded by default, flagged listings are returned with a warning tag.

Usage:
    from app.services.listing_queries import search_listings, get_listing_detail
    from app.core.database import snowflake_cursor

    with snowflake_cursor() as cursor:
        results = search_listings(cursor, max_price=3000, beds_min=2, city="Boston")
        detail = get_listing_detail(cursor, listing_id="abc123")
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import structlog

from app.core.config_loader import CONFIG_DIR

logger = structlog.get_logger()


# -- Config ------------------------------------------------------

def _load_service_config() -> dict:
    """Load and return the listing_queries section of services.yml."""
    import yaml
    with open(CONFIG_DIR / "services.yml", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return raw.get("listing_queries", {})


def _cfg() -> dict:
    """Cached config accessor. Reloads on first call per process."""
    if not hasattr(_cfg, "_cache"):
        _cfg._cache = _load_service_config()
    return _cfg._cache


def reload_config():
    """Force config reload. Call after config file changes in tests."""
    if hasattr(_cfg, "_cache"):
        del _cfg._cache


# -- Result Types ------------------------------------------------

@dataclass
class QueryResult:
    """Standard return type for all listing queries."""
    success: bool
    query_type: str
    data: list[dict] = field(default_factory=list)
    total_count: int = 0
    duration_ms: int = 0
    sql_executed: str = ""
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = {
            "success": self.success,
            "query_type": self.query_type,
            "data": self.data,
            "total_count": self.total_count,
            "duration_ms": self.duration_ms,
        }
        if self.warnings:
            d["warnings"] = self.warnings
        if self.error:
            d["error"] = self.error
        return d


# -- SQL Helpers -------------------------------------------------

def _url_filter_clause(table_alias: str = "l") -> tuple[str, list[str]]:
    """Build WHERE clause fragment for URL status filtering.

    Returns (sql_fragment, warnings_list).
    The fragment is empty string if url_filtering is disabled.
    """
    cfg = _cfg()
    url_cfg = cfg.get("url_filtering", {})

    if not url_cfg.get("enabled", True):
        return "", []

    exclude = url_cfg.get("exclude_statuses", ["confirmed_dead"])

    # Build exclusion clause -- uses COALESCE for rows where url_status is NULL
    # (pre-migration rows default to active behavior).
    # Note: warn_statuses (e.g. "flagged") are NOT excluded here -- they are
    # returned in results and tagged with warnings at the query function level.
    if exclude:
        placeholders = ", ".join(f"'{s}'" for s in exclude)
        clause = f"COALESCE({table_alias}.url_status, 'active') NOT IN ({placeholders})"
        return clause, []

    return "", []


def _rows_to_dicts(cursor, rows: list) -> list[dict]:
    """Convert Snowflake cursor rows to list of dicts with lowercase keys."""
    if not rows:
        return []
    columns = [desc[0].lower() for desc in cursor.description]
    results = []
    for row in rows:
        d = {}
        for col, val in zip(columns, row):
            # Parse VARIANT/ARRAY columns from JSON strings
            if isinstance(val, str) and col in (
                "scoring_metadata", "safety_metadata", "livability_metadata",
                "lifestyle_scores", "nearby_amenities", "nearest_stops",
                "price_history", "safety_trend", "raw_json",
                "classification_metadata", "preference_tags",
                "waypoints", "waypoint_scores", "transit_lines",
                "tool_calls", "decisions", "pending_actions",
                "listings_discussed",
            ):
                try:
                    d[col] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    d[col] = val
            else:
                d[col] = val
        results.append(d)
    return results


def _clamp(value: int, lo: int, hi: int) -> int:
    """Bound an integer to [lo, hi]."""
    return max(lo, min(hi, value))


# -- Listing Detail ----------------------------------------------

def get_listing_detail(
    cursor,
    listing_id: str,
) -> QueryResult:
    """Full detail for a single listing.

    Joins RAW.LISTINGS with SCORECARDS.LISTING_SUMMARY for the latest
    scores and metadata. Returns all columns the agent might need to
    answer any question about this listing.

    Args:
        cursor: Snowflake cursor.
        listing_id: The listing to retrieve.

    Returns:
        QueryResult with a single-element data list, or empty if not found.
    """
    cfg = _cfg()
    log = logger.bind(service="listing_queries", query="detail")

    if not cfg.get("enabled", True):
        return QueryResult(success=False, query_type="listing_detail",
                           error="listing_queries service is disabled")

    include_meta = cfg.get("include_scoring_metadata", True)
    include_desc = cfg.get("include_description_text", True)
    include_raw = cfg.get("include_raw_json", False)

    # Build SELECT columns
    select_cols = [
        "l.listing_id", "l.source", "l.source_url",
        "l.price", "l.beds", "l.baths", "l.sqft",
        "l.street", "l.unit", "l.city", "l.zip_code", "l.neighborhood",
        "l.lat", "l.lon",
        "l.primary_photo_url", "l.mls_id", "l.mls_status",
        "l.days_on_mls", "l.agent_name", "l.style", "l.list_date",
        "l.is_current", "l.first_seen_at", "l.last_seen_at",
    ]

    if include_desc:
        select_cols.append("l.description_text")
    if include_raw:
        select_cols.append("l.raw_json")

    # LISTING_SUMMARY columns (may be NULL if never scored)
    select_cols.extend([
        "ls.safety_score", "ls.livability_score",
        "ls.is_active AS summary_is_active",
        "ls.nearest_stops", "ls.last_scored_at",
        "ls.price_history", "ls.safety_trend",
    ])

    if include_meta:
        select_cols.extend([
            "ls.safety_metadata", "ls.livability_metadata",
            "ls.lifestyle_scores", "ls.nearby_amenities",
        ])

    # URL status column (may not exist pre-migration -- handled by COALESCE)
    select_cols.append("COALESCE(l.url_status, 'active') AS url_status")

    select_str = ",\n        ".join(select_cols)

    sql = f"""
        SELECT
        {select_str}
        FROM RAW.LISTINGS l
        LEFT JOIN SCORECARDS.LISTING_SUMMARY ls
            ON l.listing_id = ls.listing_id
        WHERE l.listing_id = %s
    """

    start = time.perf_counter()
    try:
        cursor.execute(sql, (listing_id,))
        rows = cursor.fetchall()
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("query_failed", listing_id=listing_id, error=str(e)[:200])
        return QueryResult(success=False, query_type="listing_detail",
                           error=str(e)[:500])

    data = _rows_to_dicts(cursor, rows)
    warnings = []

    # Tag URL status warnings -- reads warn_statuses from config
    url_cfg = cfg.get("url_filtering", {})
    warn_statuses = set(url_cfg.get("warn_statuses", ["flagged"]))
    if data and data[0].get("url_status") in warn_statuses:
        warnings.append(
            "This listing's URL has been flagged by users as potentially broken. "
            "The listing data is still valid but the source link may not work."
        )

    log.info("query_complete", listing_id=listing_id, found=len(data), ms=ms)

    return QueryResult(
        success=True,
        query_type="listing_detail",
        data=data,
        total_count=len(data),
        duration_ms=ms,
        sql_executed=sql.strip(),
        warnings=warnings,
    )


# -- Search Listings ---------------------------------------------

def search_listings(
    cursor,
    *,
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
    min_livability_score: Optional[int] = None,
    has_photo: Optional[bool] = None,
    is_current: bool = True,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> QueryResult:
    """Filtered listing search with scoring data.

    Joins RAW.LISTINGS with SCORECARDS.LISTING_SUMMARY to expose
    safety_score and livability_score in results. All filters are
    optional -- omitted filters are not applied.

    Args:
        cursor: Snowflake cursor.
        min_price / max_price: Budget range.
        beds_min / beds_max: Bedroom count range.
        baths_min: Minimum bathrooms.
        city: Exact city match (case-insensitive).
        neighborhood: Exact neighborhood match (case-insensitive).
        zip_code: Exact zip code match.
        min_sqft: Minimum square footage.
        min_safety_score: Minimum safety percentile (0-100).
        min_livability_score: Minimum livability percentile (0-100).
        has_photo: If True, only listings with primary_photo_url.
        is_current: If True, only active listings (default).
        sort_by: Column to sort by (validated against allowed list).
        sort_order: "asc" or "desc".
        limit: Max results to return.
        offset: Pagination offset.

    Returns:
        QueryResult with matching listings.
    """
    cfg = _cfg()
    search_cfg = cfg.get("search", {})
    log = logger.bind(service="listing_queries", query="search")

    if not cfg.get("enabled", True):
        return QueryResult(success=False, query_type="listing_search",
                           error="listing_queries service is disabled")

    # Validate and apply defaults
    allowed_sorts = search_cfg.get("allowed_sort_fields", ["safety_score"])
    default_sort = search_cfg.get("default_sort", "safety_score")
    default_order = search_cfg.get("default_sort_order", "desc")
    max_results = search_cfg.get("max_results", 50)
    default_results = search_cfg.get("default_results", 20)

    sort_by = sort_by if sort_by in allowed_sorts else default_sort
    sort_order = sort_order if sort_order in ("asc", "desc") else default_order
    limit = _clamp(limit or default_results, 1, max_results)

    # Build WHERE clauses
    conditions = []
    params = []

    if is_current:
        conditions.append("l.is_current = TRUE")

    # URL filtering
    url_clause, _ = _url_filter_clause("l")
    if url_clause:
        conditions.append(url_clause)

    if min_price is not None:
        conditions.append("l.price >= %s")
        params.append(min_price)
    if max_price is not None:
        conditions.append("l.price <= %s")
        params.append(max_price)

    if beds_min is not None:
        conditions.append("l.beds >= %s")
        params.append(beds_min)
    if beds_max is not None:
        conditions.append("l.beds <= %s")
        params.append(beds_max)

    if baths_min is not None:
        conditions.append("l.baths >= %s")
        params.append(baths_min)

    if city:
        conditions.append("LOWER(l.city) = LOWER(%s)")
        params.append(city)
    if neighborhood:
        conditions.append("LOWER(l.neighborhood) = LOWER(%s)")
        params.append(neighborhood)
    if zip_code:
        conditions.append("l.zip_code = %s")
        params.append(zip_code)

    if min_sqft is not None:
        conditions.append("l.sqft >= %s")
        params.append(min_sqft)

    if min_safety_score is not None:
        conditions.append("ls.safety_score >= %s")
        params.append(min_safety_score)
    if min_livability_score is not None:
        conditions.append("ls.livability_score >= %s")
        params.append(min_livability_score)

    if has_photo:
        conditions.append("l.primary_photo_url IS NOT NULL")

    # Coordinates must exist for spatial queries downstream
    conditions.append("l.lat IS NOT NULL")

    where_str = " AND ".join(conditions) if conditions else "TRUE"

    # Determine sort table prefix
    sort_prefix = "ls" if sort_by in ("safety_score", "livability_score") else "l"

    sql = f"""
        SELECT
            l.listing_id, l.source, l.source_url,
            l.price, l.beds, l.baths, l.sqft,
            l.street, l.unit, l.city, l.zip_code, l.neighborhood,
            l.lat, l.lon,
            l.primary_photo_url, l.days_on_mls, l.style, l.list_date,
            l.is_current,
            COALESCE(l.url_status, 'active') AS url_status,
            ls.safety_score, ls.livability_score,
            ls.nearest_stops, ls.last_scored_at
        FROM RAW.LISTINGS l
        LEFT JOIN SCORECARDS.LISTING_SUMMARY ls
            ON l.listing_id = ls.listing_id
        WHERE {where_str}
        ORDER BY {sort_prefix}.{sort_by} {sort_order} NULLS LAST
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])

    start = time.perf_counter()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("query_failed", error=str(e)[:200])
        return QueryResult(success=False, query_type="listing_search",
                           error=str(e)[:500])

    data = _rows_to_dicts(cursor, rows)

    # Tag flagged URLs in results -- reads warn_statuses from config
    warnings = []
    url_cfg = cfg.get("url_filtering", {})
    warn_statuses = set(url_cfg.get("warn_statuses", ["flagged"]))
    flagged_count = sum(1 for d in data if d.get("url_status") in warn_statuses)
    if flagged_count > 0:
        warnings.append(
            f"{flagged_count} listing(s) have URLs flagged as potentially broken."
        )

    log.info("query_complete", results=len(data), filters=len(conditions),
             sort=f"{sort_by} {sort_order}", ms=ms)

    return QueryResult(
        success=True,
        query_type="listing_search",
        data=data,
        total_count=len(data),
        duration_ms=ms,
        sql_executed=sql.strip(),
        warnings=warnings,
    )


# -- Compare Listings --------------------------------------------

def compare_listings(
    cursor,
    listing_ids: list[str],
) -> QueryResult:
    """Side-by-side comparison of 2-N listings.

    Returns full detail for each listing plus the latest LOCATION_SCORECARD
    row (if available) for trend data. Designed for the Chat Agent to
    present a comparison table to the user.

    Args:
        cursor: Snowflake cursor.
        listing_ids: List of listing IDs to compare.

    Returns:
        QueryResult with one dict per listing, ordered by input list.
    """
    cfg = _cfg()
    compare_cfg = cfg.get("compare", {})
    log = logger.bind(service="listing_queries", query="compare")

    if not cfg.get("enabled", True):
        return QueryResult(success=False, query_type="listing_compare",
                           error="listing_queries service is disabled")

    max_listings = compare_cfg.get("max_listings", 10)
    include_scorecard = compare_cfg.get("include_scorecard_latest", True)

    if not listing_ids:
        return QueryResult(success=False, query_type="listing_compare",
                           error="No listing IDs provided")

    if len(listing_ids) > max_listings:
        return QueryResult(
            success=False, query_type="listing_compare",
            error=f"Too many listings: {len(listing_ids)} (max {max_listings})"
        )

    # Parameterized IN clause
    placeholders = ", ".join(["%s"] * len(listing_ids))

    # Base columns from LISTINGS + LISTING_SUMMARY
    sql = f"""
        SELECT
            l.listing_id, l.source, l.source_url,
            l.price, l.beds, l.baths, l.sqft,
            l.street, l.unit, l.city, l.zip_code, l.neighborhood,
            l.lat, l.lon,
            l.primary_photo_url, l.days_on_mls, l.style, l.list_date,
            l.is_current, l.first_seen_at,
            COALESCE(l.url_status, 'active') AS url_status,
            ls.safety_score, ls.livability_score,
            ls.safety_metadata, ls.livability_metadata,
            ls.nearest_stops, ls.last_scored_at
    """

    # Optionally join latest scorecard for raw metrics
    if include_scorecard:
        sql += """,
            sc.score_date AS latest_score_date,
            sc.crime_count, sc.violent_count, sc.crime_trend,
            sc.complaint_count,
            sc.citizen_incidents_48h, sc.citizen_nighttime_48h,
            sc.nearby_transit_stops, sc.nearby_amenity_count,
            sc.current_price, sc.scoring_metadata
        """

    sql += f"""
        FROM RAW.LISTINGS l
        LEFT JOIN SCORECARDS.LISTING_SUMMARY ls
            ON l.listing_id = ls.listing_id
    """

    if include_scorecard:
        # Latest scorecard via window function
        sql += f"""
        LEFT JOIN (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY listing_id ORDER BY score_date DESC
            ) AS rn
            FROM SCORECARDS.LOCATION_SCORECARD
            WHERE listing_id IN ({placeholders})
        ) sc ON l.listing_id = sc.listing_id AND sc.rn = 1
        """

    sql += f"""
        WHERE l.listing_id IN ({placeholders})
    """

    # Parameters: scorecard subquery IDs + main WHERE IDs
    params = list(listing_ids) + list(listing_ids) if include_scorecard else list(listing_ids)

    start = time.perf_counter()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("query_failed", listing_ids=listing_ids, error=str(e)[:200])
        return QueryResult(success=False, query_type="listing_compare",
                           error=str(e)[:500])

    data = _rows_to_dicts(cursor, rows)

    # Reorder to match input listing_ids order
    data_by_id = {d["listing_id"]: d for d in data}
    ordered = [data_by_id[lid] for lid in listing_ids if lid in data_by_id]

    # Warn about missing listings
    warnings = []
    missing = [lid for lid in listing_ids if lid not in data_by_id]
    if missing:
        warnings.append(f"Listings not found: {', '.join(missing)}")

    flagged = [d["listing_id"] for d in ordered
               if d.get("url_status") in set(
                   cfg.get("url_filtering", {}).get("warn_statuses", ["flagged"])
               )]
    if flagged:
        warnings.append(
            f"Flagged URLs on: {', '.join(flagged)}"
        )

    log.info("query_complete", requested=len(listing_ids),
             found=len(ordered), ms=ms)

    return QueryResult(
        success=True,
        query_type="listing_compare",
        data=ordered,
        total_count=len(ordered),
        duration_ms=ms,
        sql_executed=sql.strip(),
        warnings=warnings,
    )


# -- Scorecard History -------------------------------------------

def scorecard_history(
    cursor,
    listing_id: str,
    *,
    days: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> QueryResult:
    """LOCATION_SCORECARD time series for a single listing.

    Returns one row per day over the requested range. This is the
    temporal intelligence -- the watch period's daily snapshots showing
    how safety, livability, and other metrics change over time.

    Args:
        cursor: Snowflake cursor.
        listing_id: The listing to query.
        days: Lookback days from today (overridden by start_date/end_date).
        start_date: Explicit start date (YYYY-MM-DD).
        end_date: Explicit end date (YYYY-MM-DD). Defaults to today.

    Returns:
        QueryResult with one dict per scorecard day, ordered chronologically.
    """
    cfg = _cfg()
    sc_cfg = cfg.get("scorecard_history", {})
    log = logger.bind(service="listing_queries", query="scorecard_history")

    if not cfg.get("enabled", True):
        return QueryResult(success=False, query_type="scorecard_history",
                           error="listing_queries service is disabled")

    max_days = sc_cfg.get("max_days", 90)
    default_days = sc_cfg.get("default_days", 14)

    # Resolve date range
    params = [listing_id]
    date_filter = ""

    if start_date and end_date:
        date_filter = "AND sc.score_date BETWEEN %s AND %s"
        params.extend([start_date, end_date])
    elif start_date:
        date_filter = "AND sc.score_date >= %s"
        params.append(start_date)
    else:
        lookback = _clamp(days or default_days, 1, max_days)
        date_filter = f"AND sc.score_date >= DATEADD(day, -{lookback}, CURRENT_DATE())"

    sql = f"""
        SELECT
            sc.listing_id, sc.score_date,
            sc.crime_count, sc.violent_count, sc.crime_trend,
            sc.complaint_count,
            sc.citizen_incidents_48h, sc.citizen_nighttime_48h,
            sc.nearby_transit_stops, sc.nearby_amenity_count,
            sc.listing_active, sc.current_price,
            sc.safety_score, sc.livability_score,
            sc.scoring_metadata,
            sc.pipeline_run_id
        FROM SCORECARDS.LOCATION_SCORECARD sc
        WHERE sc.listing_id = %s
        {date_filter}
        ORDER BY sc.score_date ASC
    """

    start = time.perf_counter()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("query_failed", listing_id=listing_id, error=str(e)[:200])
        return QueryResult(success=False, query_type="scorecard_history",
                           error=str(e)[:500])

    data = _rows_to_dicts(cursor, rows)
    log.info("query_complete", listing_id=listing_id, days=len(data), ms=ms)

    return QueryResult(
        success=True,
        query_type="scorecard_history",
        data=data,
        total_count=len(data),
        duration_ms=ms,
        sql_executed=sql.strip(),
    )


# -- Route Scorecard History -------------------------------------

def route_scorecard_history(
    cursor,
    route_id: str,
    *,
    days: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> QueryResult:
    """ROUTE_SCORECARD time series for a single route.

    Returns one row per day showing corridor safety metrics over time.
    Used by the Report Generator to analyze commute safety trends
    across the watch period.

    Args:
        cursor: Snowflake cursor.
        route_id: The configured route ID.
        days: Lookback days from today.
        start_date: Explicit start date (YYYY-MM-DD).
        end_date: Explicit end date (YYYY-MM-DD).

    Returns:
        QueryResult with one dict per scorecard day, ordered chronologically.
    """
    cfg = _cfg()
    rsc_cfg = cfg.get("route_scorecard_history", {})
    log = logger.bind(service="listing_queries", query="route_scorecard_history")

    if not cfg.get("enabled", True):
        return QueryResult(success=False, query_type="route_scorecard_history",
                           error="listing_queries service is disabled")

    max_days = rsc_cfg.get("max_days", 90)
    default_days = rsc_cfg.get("default_days", 14)

    # Resolve date range
    params = [route_id]
    date_filter = ""

    if start_date and end_date:
        date_filter = "AND rs.score_date BETWEEN %s AND %s"
        params.extend([start_date, end_date])
    elif start_date:
        date_filter = "AND rs.score_date >= %s"
        params.append(start_date)
    else:
        lookback = _clamp(days or default_days, 1, max_days)
        date_filter = f"AND rs.score_date >= DATEADD(day, -{lookback}, CURRENT_DATE())"

    sql = f"""
        SELECT
            rs.route_id, rs.listing_id, rs.score_date,
            rs.crime_count, rs.violent_count, rs.shooting_count,
            rs.crimes_at_dep_hour,
            rs.citizen_incidents, rs.citizen_nighttime,
            rs.scoring_metadata,
            rs.pipeline_run_id
        FROM SCORECARDS.ROUTE_SCORECARD rs
        WHERE rs.route_id = %s
        {date_filter}
        ORDER BY rs.score_date ASC
    """

    start = time.perf_counter()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("query_failed", route_id=route_id, error=str(e)[:200])
        return QueryResult(success=False, query_type="route_scorecard_history",
                           error=str(e)[:500])

    data = _rows_to_dicts(cursor, rows)
    log.info("query_complete", route_id=route_id, days=len(data), ms=ms)

    return QueryResult(
        success=True,
        query_type="route_scorecard_history",
        data=data,
        total_count=len(data),
        duration_ms=ms,
        sql_executed=sql.strip(),
    )


# -- Lookup by URL -----------------------------------------------

def get_listing_by_url(
    cursor,
    source_url: str,
) -> QueryResult:
    """Find a listing by its source URL.

    Used in the URL validation flow -- when a user reports a broken link,
    the agent needs to find which listing_id corresponds to the URL.

    Args:
        cursor: Snowflake cursor.
        source_url: The URL to search for.

    Returns:
        QueryResult with matching listing(s). May return multiple if
        the same URL was ingested from different pipeline runs (deduped
        by listing_id in practice due to MERGE).
    """
    log = logger.bind(service="listing_queries", query="by_url")

    sql = """
        SELECT
            l.listing_id, l.source, l.source_url,
            l.price, l.beds, l.baths,
            l.street, l.city, l.neighborhood,
            l.is_current,
            COALESCE(l.url_status, 'active') AS url_status
        FROM RAW.LISTINGS l
        WHERE l.source_url = %s
    """

    start = time.perf_counter()
    try:
        cursor.execute(sql, (source_url,))
        rows = cursor.fetchall()
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("query_failed", url=source_url[:100], error=str(e)[:200])
        return QueryResult(success=False, query_type="listing_by_url",
                           error=str(e)[:500])

    data = _rows_to_dicts(cursor, rows)
    log.info("query_complete", url=source_url[:80], found=len(data), ms=ms)

    return QueryResult(
        success=True,
        query_type="listing_by_url",
        data=data,
        total_count=len(data),
        duration_ms=ms,
        sql_executed=sql.strip(),
    )


# -- User's Bookmarked Listings (read path) ----------------------

def get_bookmarked_listings(
    cursor,
    user_id: str,
    *,
    active_only: bool = True,
    include_scores: bool = True,
) -> QueryResult:
    """Retrieve a user's bookmarked listings with current scores.

    Joins BOOKMARKED_LISTINGS → LISTINGS → LISTING_SUMMARY to give
    the Chat Agent everything it needs to discuss the user's watch list.

    Args:
        cursor: Snowflake cursor.
        user_id: The user whose bookmarks to retrieve.
        active_only: If True, only return active bookmarks within watch period.
        include_scores: If True, join LISTING_SUMMARY for scores.

    Returns:
        QueryResult with bookmarked listings and their current state.
    """
    log = logger.bind(service="listing_queries", query="bookmarked")

    select_cols = [
        "bl.id AS bookmark_id", "bl.listing_id", "bl.notes",
        "bl.is_active AS bookmark_active", "bl.added_at", "bl.watch_end",
        "l.source", "l.source_url",
        "l.price", "l.beds", "l.baths", "l.sqft",
        "l.street", "l.city", "l.neighborhood",
        "l.lat", "l.lon", "l.primary_photo_url",
        "l.is_current",
        "COALESCE(l.url_status, 'active') AS url_status",
    ]

    if include_scores:
        select_cols.extend([
            "ls.safety_score", "ls.livability_score",
            "ls.nearest_stops", "ls.last_scored_at",
        ])

    select_str = ", ".join(select_cols)

    joins = """
        FROM USER_DATA.BOOKMARKED_LISTINGS bl
        JOIN RAW.LISTINGS l ON bl.listing_id = l.listing_id
    """

    if include_scores:
        joins += """
        LEFT JOIN SCORECARDS.LISTING_SUMMARY ls
            ON l.listing_id = ls.listing_id
        """

    conditions = ["bl.user_id = %s"]
    params = [user_id]

    if active_only:
        conditions.append("bl.is_active = TRUE")
        # Only include bookmarks still within watch period
        conditions.append(
            "(bl.watch_end IS NULL OR bl.watch_end >= CURRENT_TIMESTAMP())"
        )

    # URL filtering
    url_clause, _ = _url_filter_clause("l")
    if url_clause:
        conditions.append(url_clause)

    where_str = " AND ".join(conditions)

    sql = f"""
        SELECT {select_str}
        {joins}
        WHERE {where_str}
        ORDER BY bl.added_at DESC
    """

    start = time.perf_counter()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("query_failed", user_id=user_id, error=str(e)[:200])
        return QueryResult(success=False, query_type="bookmarked_listings",
                           error=str(e)[:500])

    data = _rows_to_dicts(cursor, rows)

    warnings = []
    expired = [d for d in data if d.get("watch_end") and
               str(d["watch_end"]) < datetime.now(timezone.utc).isoformat()]
    if expired:
        warnings.append(
            f"{len(expired)} bookmark(s) have expired watch periods."
        )

    log.info("query_complete", user_id=user_id, bookmarks=len(data), ms=ms)

    return QueryResult(
        success=True,
        query_type="bookmarked_listings",
        data=data,
        total_count=len(data),
        duration_ms=ms,
        sql_executed=sql.strip(),
        warnings=warnings,
    )


# -- User's Configured Routes (read path) ------------------------

def get_configured_routes(
    cursor,
    user_id: str,
    *,
    listing_id: Optional[str] = None,
    active_only: bool = True,
) -> QueryResult:
    """Retrieve a user's configured commute routes.

    Optionally filtered to a specific listing. Returns waypoints,
    transit lines, duration, and waypoint_scores for map rendering.

    Args:
        cursor: Snowflake cursor.
        user_id: The user whose routes to retrieve.
        listing_id: Optionally filter to routes for this listing.
        active_only: If True, only return active routes.

    Returns:
        QueryResult with route configurations.
    """
    log = logger.bind(service="listing_queries", query="routes")

    conditions = ["cr.user_id = %s"]
    params = [user_id]

    if listing_id:
        conditions.append("cr.listing_id = %s")
        params.append(listing_id)

    if active_only:
        conditions.append("cr.is_active = TRUE")

    where_str = " AND ".join(conditions)

    sql = f"""
        SELECT
            cr.id AS route_id, cr.listing_id,
            cr.dest_label, cr.dest_address, cr.dest_lat, cr.dest_lon,
            cr.departure_hour, cr.travel_mode,
            cr.duration_min, cr.distance_text, cr.transit_lines,
            cr.waypoints, cr.waypoint_scores,
            cr.is_active, cr.computed_at
        FROM USER_DATA.CONFIGURED_ROUTES cr
        WHERE {where_str}
        ORDER BY cr.computed_at DESC
    """

    start = time.perf_counter()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("query_failed", user_id=user_id, error=str(e)[:200])
        return QueryResult(success=False, query_type="configured_routes",
                           error=str(e)[:500])

    data = _rows_to_dicts(cursor, rows)
    log.info("query_complete", user_id=user_id, routes=len(data), ms=ms)

    return QueryResult(
        success=True,
        query_type="configured_routes",
        data=data,
        total_count=len(data),
        duration_ms=ms,
        sql_executed=sql.strip(),
    )