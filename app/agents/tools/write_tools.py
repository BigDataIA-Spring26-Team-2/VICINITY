"""Organizer Agent write tools -- mutations and multi-step chains.

Five tools covering all write paths. The Organizer is the only agent
with write access to Snowflake USER_DATA tables.

Tool -> Service mapping:
    manage_profile      -> user_data.upsert_profile + routes.geocode
    manage_destinations -> routes.geocode + routes.compute_route
                        + route_scorer.score_corridor + user_data.save_configured_route
    manage_bookmarks    -> user_data.create_bookmark / remove_bookmark
    manage_conversations -> user_data.append_message + write_session_summary
    flag_data           -> url_health.flag_url
"""

from __future__ import annotations

import json
from typing import Optional

from langchain_core.tools import tool

from app.agents.tools.read_tools import _get_cursor



# =====================================================================
# Tool 1: manage_profile
# =====================================================================

@tool
def manage_profile(
    user_id: str,
    profile_name: Optional[str] = None,
    work_address: Optional[str] = None,
    budget_min: Optional[int] = None,
    budget_max: Optional[int] = None,
    bedrooms_min: Optional[int] = None,
    bedrooms_max: Optional[int] = None,
    max_commute_min: Optional[int] = None,
    preferences_text: Optional[str] = None,
    preference_tags: Optional[list[str]] = None,
) -> str:
    """Create or update the user's search profile.

    Saves preferences, budget, bedroom requirements, and work address.
    If work_address is provided, it is geocoded automatically.
    Deactivates any previous active profile for this user.

    Args:
        user_id: The user's ID.
        profile_name: Name for this profile (e.g. "Summer 2026 Search").
        work_address: Street address to geocode (e.g. "77 Massachusetts Ave, Cambridge, MA").
        budget_min: Minimum monthly rent.
        budget_max: Maximum monthly rent.
        bedrooms_min: Minimum bedrooms.
        bedrooms_max: Maximum bedrooms.
        max_commute_min: Maximum commute time in minutes.
        preferences_text: Free-text preferences (e.g. "Korean food, gym, quiet neighborhood").
        preference_tags: Structured tags from preference expansion (e.g. ["korean_food", "gym"]).

    Returns JSON with profile_id of the created profile.
    """
    from app.core.routes import geocode
    from app.services.user_data import upsert_profile

    work_lat, work_lon = None, None
    if work_address:
        geo = geocode(work_address)
        if geo:
            work_lat = geo["lat"]
            work_lon = geo["lon"]
            work_address = geo.get("formatted", work_address)

    cursor = _get_cursor()
    try:
        result = upsert_profile(
            cursor, user_id,
            profile_name=profile_name,
            work_address=work_address,
            work_lat=work_lat, work_lon=work_lon,
            budget_min=budget_min, budget_max=budget_max,
            bedrooms_min=bedrooms_min, bedrooms_max=bedrooms_max,
            max_commute_min=max_commute_min,
            preferences_text=preferences_text,
            preference_tags=preference_tags,
        )

        # Expand new preference tags into pipeline config files
        # so the next DAG run picks up relevant Reddit/News/Eventbrite content
        if result.success and preference_tags:
            try:
                from app.services.config_writer import expand_preference_tags
                expand_preference_tags(preference_tags)
            except Exception as e:
                # Config write failure is non-fatal — profile is already saved
                import structlog
                structlog.get_logger().warning(
                    "config_write_failed", tags=preference_tags,
                    error=str(e)[:200],
                )

        return json.dumps(result.to_dict(), default=str)
    finally:
        cursor.close()


# =====================================================================
# Tool 2: manage_destinations
# =====================================================================

@tool
def manage_destinations(
    user_id: str,
    listing_id: str,
    dest_label: str,
    dest_address: str,
    departure_hour: int = 8,
    travel_mode: str = "transit",
) -> str:
    """Compute and save a commute route from a listing to a destination.

    Full chain: geocode destination -> compute Google Maps route ->
    score the corridor for safety -> save everything to database.

    Call this when the user says "add my work address" or "compute
    commute from listing X to Y".

    Args:
        user_id: The user's ID.
        listing_id: The listing to compute the route from.
        dest_label: Short name for this destination (e.g. "Work", "Gym", "School").
        dest_address: Full address to geocode (e.g. "77 Massachusetts Ave, Cambridge, MA").
        departure_hour: Hour the user departs (24h format, default 8).
        travel_mode: One of "transit", "walking", "driving", "bicycling".

    Returns JSON with route_id, duration_min, transit_lines, waypoint count,
    and corridor safety summary (crime_count, violent_count, crimes_at_dep_hour).
    """
    from app.core.routes import geocode, compute_route
    from app.scoring.route_scorer import score_corridor
    from app.services.user_data import save_configured_route
    from app.services.listing_queries import get_listing_detail

    # Step 1: Get listing coordinates
    cursor = _get_cursor()
    try:
        listing = get_listing_detail(cursor, listing_id)
        if not listing.success or not listing.data:
            return json.dumps({"success": False, "error": f"Listing {listing_id} not found"})
        ld = listing.data[0]
        origin_lat, origin_lon = ld["lat"], ld["lon"]
        if not origin_lat or not origin_lon:
            return json.dumps({"success": False, "error": "Listing has no coordinates"})
    finally:
        cursor.close()

    # Step 2: Geocode destination
    geo = geocode(dest_address)
    if not geo:
        return json.dumps({"success": False, "error": f"Could not geocode: {dest_address}"})
    dest_lat, dest_lon = geo["lat"], geo["lon"]

    # Step 3: Compute route
    route = compute_route(
        origin_lat=origin_lat, origin_lon=origin_lon,
        dest_lat=dest_lat, dest_lon=dest_lon,
        mode=travel_mode, departure_hour=departure_hour,
    )
    if not route:
        return json.dumps({"success": False, "error": "Route computation failed"})

    # Step 4: Score corridor
    cursor = _get_cursor()
    try:
        waypoint_dicts = [{"lat": w.lat, "lon": w.lon} for w in route.waypoints]
        corridor = score_corridor(
            cursor, waypoint_dicts,
            departure_hour=departure_hour,
            include_series=False,
        )

        # Step 5: Save to database
        save_result = save_configured_route(
            cursor, user_id, listing_id, dest_label,
            dest_address=geo.get("formatted", dest_address),
            dest_lat=dest_lat, dest_lon=dest_lon,
            departure_hour=departure_hour,
            travel_mode=travel_mode,
            duration_min=route.duration_min,
            distance_text=route.distance_text,
            transit_lines=route.transit_lines,
            waypoints=waypoint_dicts,
            waypoint_scores=corridor.to_waypoint_variant(),
        )

        # Build response with both route and safety data
        response = save_result.to_dict()
        response["route_summary"] = {
            "duration_min": route.duration_min,
            "distance": route.distance_text,
            "transit_lines": route.transit_lines,
            "waypoints": len(waypoint_dicts),
        }
        response["corridor_safety"] = {
            "crime_count": corridor.crime_count,
            "violent_count": corridor.violent_count,
            "crimes_at_dep_hour": corridor.crimes_at_dep_hour,
            "citizen_incidents": corridor.citizen_incidents,
            "crime_trend": corridor.crime_trend,
        }
        return json.dumps(response, default=str)
    finally:
        cursor.close()


# =====================================================================
# Tool 3: manage_bookmarks
# =====================================================================

@tool
def manage_bookmarks(
    action: str,
    user_id: str,
    listing_id: str,
    notes: Optional[str] = None,
    watch_days: Optional[int] = None,
) -> str:
    """Add or remove a listing bookmark.

    action must be one of:
      "add"    - Bookmark a listing and start watching it. Optional notes and watch_days.
      "remove" - Remove a bookmark (soft delete).

    Args:
        action: "add" or "remove".
        user_id: The user's ID.
        listing_id: The listing to bookmark/unbookmark.
        notes: Optional notes about why the user is interested (add only).
        watch_days: How many days to watch this listing (default 14, max 30). Add only.

    Returns JSON confirming the action with bookmark_id and watch_end date.
    """
    from app.services.user_data import create_bookmark, remove_bookmark

    cursor = _get_cursor()
    try:
        if action == "add":
            result = create_bookmark(
                cursor, user_id, listing_id,
                notes=notes, watch_days=watch_days,
            )
        elif action == "remove":
            result = remove_bookmark(cursor, user_id, listing_id)
        else:
            return json.dumps({"success": False, "error": f"Unknown action: {action}"})

        return json.dumps(result.to_dict(), default=str)
    finally:
        cursor.close()


# =====================================================================
# Tool 4: manage_conversations
# =====================================================================

@tool
def manage_conversations(
    action: str,
    user_id: str,
    session_id: str,
    role: Optional[str] = None,
    content: Optional[str] = None,
    tool_calls: Optional[list[dict]] = None,
    summary: Optional[str] = None,
    decisions: Optional[list[dict]] = None,
    pending_actions: Optional[list[dict]] = None,
    listings_discussed: Optional[list[str]] = None,
    message_count: int = 0,
) -> str:
    """Save conversation messages or session summaries.

    action must be one of:
      "message" - Append a message to the conversation log. Requires role and content.
      "summary" - Write a session summary for cross-session continuity. Requires summary.

    Args:
        action: "message" or "summary".
        user_id: The user's ID.
        session_id: Current session ID.
        role: Message role - "user", "assistant", or "tool" (message only).
        content: Message content (message only).
        tool_calls: Tool calls made in this message (message only, optional).
        summary: Session summary text (summary only).
        decisions: Key decisions made in the session (summary only, optional).
        pending_actions: Actions still pending (summary only, optional).
        listings_discussed: Listing IDs discussed (summary only, optional).
        message_count: Total messages in session (summary only).

    Returns JSON confirming the write.
    """
    from app.services.user_data import append_message, write_session_summary

    cursor = _get_cursor()
    try:
        if action == "message":
            result = append_message(
                cursor, user_id, session_id, role, content,
                tool_calls=tool_calls,
            )
        elif action == "summary":
            result = write_session_summary(
                cursor, user_id, session_id, summary,
                decisions=decisions,
                pending_actions=pending_actions,
                listings_discussed=listings_discussed,
                message_count=message_count,
            )
        else:
            return json.dumps({"success": False, "error": f"Unknown action: {action}"})

        return json.dumps(result.to_dict(), default=str)
    finally:
        cursor.close()


# =====================================================================
# Tool 5: flag_data
# =====================================================================

@tool
def flag_data(
    listing_id: Optional[str] = None,
    signal_id: Optional[str] = None,
    url: str = "",
) -> str:
    """Flag a broken URL or stale data in the database.

    The Organizer calls this when the Chat Agent identifies a data
    quality issue. Auto-validates the URL via HEAD request and
    updates the status accordingly.

    Args:
        listing_id: Flag a listing URL (provide this OR signal_id).
        signal_id: Flag a lifestyle signal URL.
        url: The URL to check and flag.

    Returns JSON with the new url_status (active or confirmed_dead).
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
# Tool 6: update_pipeline_queries
# =====================================================================
# ADD THIS before the ORGANIZER_TOOLS list in write_tools.py

@tool
def update_pipeline_queries(
    tag: str,
    reddit_queries: Optional[list[str]] = None,
    google_news_queries: Optional[list[str]] = None,
    eventbrite_queries: Optional[list[str]] = None,
) -> str:
    """Add search queries for a new preference tag to the data pipelines.

    Call this when a user has a lifestyle interest or livability concern
    that is NOT already covered by existing pipeline queries. The next
    DAG run will pick up these queries and ingest relevant Reddit threads,
    news articles, and Eventbrite events into the system.

    DO NOT call this for common tags that already exist (safety, noise,
    transit, rent, korean_food, yoga, live_music, dining, fitness).
    Only call for genuinely new interests the user brings up.

    Generate queries that are specific to the user's neighborhood context:
      - Reddit: conversational phrasing, include neighborhood name.
        Example: ["bharatanatyam classes Allston Brighton", "Indian dance near Somerville"]
      - Google News: journalistic phrasing, include "Boston" + neighborhood.
        Example: ["Boston Allston bharatanatyam performance", "Indian dance classes Boston area"]
      - Eventbrite: URL search slugs, lowercase with hyphens, no city.
        Example: ["bharatanatyam", "indian-classical-dance"]

    For livability concerns (noise at specific times, construction, safety
    at night), generate queries that capture the concern specifically:
      - Reddit: ["late night noise Union Square", "construction noise Somerville"]
      - Google News: ["Boston Union Square noise complaints", "Somerville construction"]

    Args:
        tag: The preference tag to add (e.g. "bharatanatyam", "late_night_noise").
        reddit_queries: 2-3 Reddit search queries with neighborhood context.
        google_news_queries: 2-3 news search queries with "Boston" + neighborhood.
        eventbrite_queries: 1-2 Eventbrite URL slugs (lifestyle tags only, skip for livability).

    Returns JSON confirming which pipelines were updated.
    """
    from app.services.config_writer import write_queries_bulk

    pipeline_queries = {}
    if reddit_queries:
        pipeline_queries["reddit"] = {tag: reddit_queries}
    if google_news_queries:
        pipeline_queries["google_news"] = {tag: google_news_queries}
    if eventbrite_queries:
        pipeline_queries["eventbrite"] = {tag: eventbrite_queries}

    if not pipeline_queries:
        return json.dumps({"success": False, "error": "No queries provided for any pipeline"})

    result = write_queries_bulk(pipeline_queries)
    return json.dumps(result, default=str)


# =====================================================================
# Tool list for binding
# =====================================================================

ORGANIZER_TOOLS = [
    manage_profile,
    manage_destinations,
    manage_bookmarks,
    manage_conversations,
    flag_data,
    update_pipeline_queries,
]