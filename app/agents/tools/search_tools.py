"""Search Supervisor + Report Generator tools.

Search Supervisor (2 tools):
    search_and_filter  -> find listings matching criteria
    score_listing      -> multi-dimension scoring for one listing

Report Generator (1 tool):
    compile_evidence   -> gather all watch period data for comparison report

The ranking and report writing are done by the LLM in the agent node,
not as separate tools. The tools fetch data, the agent synthesizes.
"""

from __future__ import annotations

import json
from typing import Optional

from langchain_core.tools import tool

from app.agents.tools.read_tools import _get_cursor


# =====================================================================
# Search Supervisor Tools
# =====================================================================

@tool
def search_and_filter(
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
    limit: Optional[int] = None,
) -> str:
    """Search apartment listings matching user criteria.

    Queries the Vicinity database for active listings with scores.
    Returns listings sorted by the specified field with safety and
    livability scores included.

    Available sort_by values: safety_score, livability_score, price,
    beds, sqft, list_date, days_on_mls.

    Args:
        min_price, max_price: Budget range.
        beds_min, beds_max: Bedroom range.
        baths_min: Minimum bathrooms.
        city: City filter (e.g. "Boston", "Cambridge", "Somerville").
        neighborhood: Neighborhood filter (e.g. "Allston", "South End").
        zip_code: Zip code filter.
        min_sqft: Minimum square footage.
        min_safety_score: Minimum safety percentile (0-100).
        sort_by: Sort field (default: safety_score).
        limit: Max results (default: 20, max: 50).

    Returns JSON with matching listings including scores and source URLs.
    """
    from app.services.listing_queries import search_listings

    cursor = _get_cursor()
    try:
        result = search_listings(
            cursor,
            min_price=min_price, max_price=max_price,
            beds_min=beds_min, beds_max=beds_max,
            baths_min=baths_min, city=city,
            neighborhood=neighborhood, zip_code=zip_code,
            min_sqft=min_sqft, min_safety_score=min_safety_score,
            sort_by=sort_by, limit=limit,
        )
        return json.dumps(result.to_dict(), default=str)
    finally:
        cursor.close()


@tool
def score_listing(
    listing_id: str,
    include_crimes: bool = True,
    include_complaints: bool = True,
    include_amenities: bool = True,
    crime_radius_m: int = 500,
    complaint_radius_m: int = 500,
    amenity_radius_m: int = 800,
) -> str:
    """Score a single listing across safety, livability, and amenities.

    Fetches listing detail, then runs crime, complaint, and amenity
    queries around the listing's coordinates. Returns a combined
    profile the Search Supervisor uses to rank candidates.

    Args:
        listing_id: The listing to score.
        include_crimes: Run crime query (default true).
        include_complaints: Run complaint summary (default true).
        include_amenities: Run amenity count (default true).
        crime_radius_m: Crime search radius (default 500m).
        complaint_radius_m: Complaint search radius (default 500m).
        amenity_radius_m: Amenity search radius (default 800m).

    Returns JSON with listing details + crime counts + complaint
    breakdown + nearby amenity count.
    """
    from app.services.listing_queries import get_listing_detail
    from app.services.crime_queries import crimes_near_point
    from app.services.complaint_queries import complaint_summary
    from app.services.amenity_lookup import search_stored_amenities

    cursor = _get_cursor()
    try:
        # Listing detail
        listing = get_listing_detail(cursor, listing_id)
        if not listing.success or not listing.data:
            return json.dumps({"success": False, "error": f"Listing {listing_id} not found"})

        ld = listing.data[0]
        lat, lon = ld.get("lat"), ld.get("lon")
        if not lat or not lon:
            return json.dumps({"success": False, "error": "Listing has no coordinates"})

        profile = {"listing": ld}

        # Run crime, complaint, and amenity queries in parallel —
        # each gets its own cursor since Snowflake cursors aren't thread-safe.
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _run_crimes():
            c = _get_cursor()
            try:
                r = crimes_near_point(c, lat, lon, radius_m=crime_radius_m)
                return "safety", {"crime_count": r.total_count, "data": r.data[:5]}
            finally:
                c.close()

        def _run_complaints():
            c = _get_cursor()
            try:
                r = complaint_summary(c, lat, lon, radius_m=complaint_radius_m)
                return "livability", {
                    "complaint_categories": r.data,
                    "total_complaints": sum(d.get("count", 0) for d in r.data),
                }
            finally:
                c.close()

        def _run_amenities():
            c = _get_cursor()
            try:
                r = search_stored_amenities(c, lat, lon, radius_m=amenity_radius_m)
                return "amenities", {"total_nearby": r.total_count, "sample": r.data[:10]}
            finally:
                c.close()

        tasks = []
        if include_crimes:
            tasks.append(_run_crimes)
        if include_complaints:
            tasks.append(_run_complaints)
        if include_amenities:
            tasks.append(_run_amenities)

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(fn) for fn in tasks]
            for future in as_completed(futures):
                try:
                    key, data = future.result()
                    profile[key] = data
                except Exception:
                    pass  # non-fatal — partial profile is still useful

        return json.dumps({"success": True, "profile": profile}, default=str)
    finally:
        cursor.close()


# =====================================================================
# Report Generator Tool
# =====================================================================

@tool
def compile_evidence(
    user_id: str,
    days: int = 14,
) -> str:
    """Compile all watch period evidence for a comparison report.

    Gathers scorecard history, route scorecard history, and narrative
    evidence for every active bookmark. The Report Generator uses this
    data to analyze tradeoffs and produce a recommendation.

    Args:
        user_id: The user whose bookmarked listings to compile.
        days: Lookback days for scorecard history (default 14 = watch period).

    Returns JSON with per-listing evidence bundles: scorecard trends,
    route safety trends, and relevant narratives.
    """
    from app.services.listing_queries import (
        get_bookmarked_listings, scorecard_history,
        route_scorecard_history, get_configured_routes,
    )
    from app.services.pinecone_search import search_narratives

    cursor = _get_cursor()
    try:
        # Get all active bookmarks
        bookmarks = get_bookmarked_listings(cursor, user_id)
        if not bookmarks.success or not bookmarks.data:
            return json.dumps({"success": False, "error": "No active bookmarks found"})

        evidence = []

        for bm in bookmarks.data:
            lid = bm["listing_id"]
            bundle = {
                "listing_id": lid,
                "listing": {
                    "street": bm.get("street"),
                    "neighborhood": bm.get("neighborhood"),
                    "price": bm.get("price"),
                    "beds": bm.get("beds"),
                    "safety_score": bm.get("safety_score"),
                    "livability_score": bm.get("livability_score"),
                    "source_url": bm.get("source_url"),
                    "watch_end": bm.get("watch_end"),
                },
            }

            # Scorecard history
            sc = scorecard_history(cursor, lid, days=days)
            if sc.success:
                bundle["scorecard_trend"] = sc.data

            # Route scorecards
            routes = get_configured_routes(cursor, user_id, listing_id=lid)
            if routes.success and routes.data:
                bundle["route_trends"] = []
                for r in routes.data:
                    rsc = route_scorecard_history(cursor, r["route_id"], days=days)
                    if rsc.success:
                        bundle["route_trends"].append({
                            "dest_label": r["dest_label"],
                            "duration_min": r["duration_min"],
                            "transit_lines": r.get("transit_lines"),
                            "scorecard": rsc.data,
                        })

            # Narrative evidence
            hood = bm.get("neighborhood", "")
            if hood:
                narratives = search_narratives(
                    f"safety and livability near {hood}",
                    filters={"neighborhoods": [hood]},
                    top_k=5,
                )
                if narratives.success:
                    bundle["narratives"] = narratives.data

            evidence.append(bundle)

        return json.dumps({
            "success": True,
            "listings_compiled": len(evidence),
            "evidence": evidence,
        }, default=str)
    finally:
        cursor.close()


# =====================================================================
# Tool lists for binding
# =====================================================================

SEARCH_SUPERVISOR_TOOLS = [
    search_and_filter,
    score_listing,
]

REPORT_GENERATOR_TOOLS = [
    compile_evidence,
]