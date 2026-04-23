"""User data service -- CRUD for profiles, bookmarks, conversations, sessions.

Read functions serve the Chat Agent (profile loading, bookmark listing).
Write functions serve the Organizer Agent exclusively.

Tables: USER_DATA.USERS, SEARCH_PROFILES, BOOKMARKED_LISTINGS,
        CONFIGURED_ROUTES, CONVERSATIONS, SESSION_SUMMARIES.

VARIANT column strategy (Snowflake driver limitation):
  Snowflake's Python connector does not support PARSE_JSON(%s) with bind
  parameters in VALUES clauses. The workaround:
    INSERT: INSERT INTO t (cols) SELECT col1, PARSE_JSON(col2) FROM VALUES (%s, %s)
    MERGE:  USING (SELECT col1, PARSE_JSON(col2) AS vc FROM VALUES (%s, %s)) AS src
  For NULL variant values, use literal NULL in the SELECT expression and
  omit the parameter from the VALUES tuple.

Usage:
    from app.services.user_data import get_active_profile, create_bookmark
    from app.services.user_data import create_user, authenticate_user, load_user_session
"""

from __future__ import annotations

import json
import uuid
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

from app.core.config_loader import CONFIG_DIR
from app.services.listing_queries import QueryResult, _rows_to_dicts, _clamp

logger = structlog.get_logger()


# -- Config ------------------------------------------------------

def _cfg() -> dict:
    if not hasattr(_cfg, "_cache"):
        import yaml
        with open(CONFIG_DIR / "services.yml", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        _cfg._cache = raw.get("user_data", {})
    return _cfg._cache


def reload_config():
    if hasattr(_cfg, "_cache"):
        del _cfg._cache


# =================================================================
# USERS
# =================================================================

def create_user(
    cursor,
    email: str,
    password: str,
    *,
    display_name: Optional[str] = None,
) -> QueryResult:
    """Register a new user. Hashes password, inserts into USERS."""
    from app.core.auth import hash_password

    log = logger.bind(service="user_data", op="create_user")

    if not email or not email.strip():
        return QueryResult(success=False, query_type="create_user",
                           error="Email is required")
    if not password or len(password) < 8:
        return QueryResult(success=False, query_type="create_user",
                           error="Password must be at least 8 characters")

    user_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    hashed = hash_password(password)

    start = time.perf_counter()
    try:
        cursor.execute(
            "INSERT INTO USER_DATA.USERS "
            "(id, email, display_name, password_hash, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (user_id, email.strip().lower(), display_name, hashed, now, now),
        )
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        error_msg = str(e)
        log.error("failed", email=email[:50], error=error_msg[:200])
        if "duplicate" in error_msg.lower() or "unique" in error_msg.lower():
            return QueryResult(success=False, query_type="create_user",
                               error="Email already registered")
        return QueryResult(success=False, query_type="create_user",
                           error=error_msg[:500])

    log.info("complete", user_id=user_id, email=email[:50], ms=ms)
    return QueryResult(
        success=True, query_type="create_user",
        data=[{"user_id": user_id, "email": email.strip().lower(),
               "display_name": display_name}],
        total_count=1, duration_ms=ms,
    )


def authenticate_user(cursor, email: str, password: str) -> QueryResult:
    """Verify email + password. Generic error on any failure."""
    from app.core.auth import verify_password

    log = logger.bind(service="user_data", op="authenticate")
    _INVALID = QueryResult(success=False, query_type="authenticate",
                           error="Invalid email or password")

    if not email or not password:
        return _INVALID

    start = time.perf_counter()
    try:
        cursor.execute(
            "SELECT id, email, display_name, password_hash "
            "FROM USER_DATA.USERS WHERE email = %s",
            (email.strip().lower(),),
        )
        row = cursor.fetchone()
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("query_failed", error=str(e)[:200])
        return QueryResult(success=False, query_type="authenticate",
                           error=str(e)[:500])

    if not row:
        log.info("user_not_found", email=email[:50], ms=ms)
        return _INVALID

    user_id, stored_email, display_name, password_hash = row

    if not password_hash:
        log.warning("no_password_hash", user_id=user_id)
        return _INVALID
    if not verify_password(password, password_hash):
        log.info("password_mismatch", user_id=user_id, ms=ms)
        return _INVALID

    log.info("authenticated", user_id=user_id, ms=ms)
    return QueryResult(
        success=True, query_type="authenticate",
        data=[{"user_id": user_id, "email": stored_email,
               "display_name": display_name}],
        total_count=1, duration_ms=ms,
    )


def get_user_by_id(cursor, user_id: str) -> QueryResult:
    """Look up a user by ID."""
    log = logger.bind(service="user_data", query="user_by_id")
    start = time.perf_counter()
    try:
        cursor.execute(
            "SELECT id, email, display_name, created_at, updated_at "
            "FROM USER_DATA.USERS WHERE id = %s", (user_id,))
        rows = cursor.fetchall()
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("query_failed", user_id=user_id, error=str(e)[:200])
        return QueryResult(success=False, query_type="user_by_id",
                           error=str(e)[:500])
    data = _rows_to_dicts(cursor, rows)
    log.info("complete", user_id=user_id, found=len(data), ms=ms)
    return QueryResult(success=True, query_type="user_by_id",
                       data=data, total_count=len(data), duration_ms=ms)


def get_user_by_email(cursor, email: str) -> QueryResult:
    """Look up a user by email."""
    log = logger.bind(service="user_data", query="user_by_email")
    start = time.perf_counter()
    try:
        cursor.execute(
            "SELECT id, email, display_name, created_at, updated_at "
            "FROM USER_DATA.USERS WHERE email = %s",
            (email.strip().lower(),))
        rows = cursor.fetchall()
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("query_failed", email=email[:50], error=str(e)[:200])
        return QueryResult(success=False, query_type="user_by_email",
                           error=str(e)[:500])
    data = _rows_to_dicts(cursor, rows)
    log.info("complete", email=email[:50], found=len(data), ms=ms)
    return QueryResult(success=True, query_type="user_by_email",
                       data=data, total_count=len(data), duration_ms=ms)


def load_user_session(cursor, user_id: str) -> dict:
    """Assemble complete UserContext for an authenticated session."""
    from app.services.listing_queries import get_bookmarked_listings

    log = logger.bind(service="user_data", op="load_session")

    user_result = get_user_by_id(cursor, user_id)
    if not user_result.success or not user_result.data:
        raise ValueError(f"User not found: {user_id}")
    user = user_result.data[0]

    profile_result = get_active_profile(cursor, user_id)
    profile = profile_result.data[0] if profile_result.data else {}

    bookmarks_result = get_bookmarked_listings(cursor, user_id)
    bookmarks = bookmarks_result.data if bookmarks_result.success else []

    summaries_result = get_recent_summaries(cursor, user_id)
    summaries = summaries_result.data if summaries_result.success else []

    context = {
        "user_id": user_id,
        "session_id": str(uuid.uuid4()),
        "email": user.get("email", ""),
        "display_name": user.get("display_name", ""),
        "profile_id": profile.get("id", ""),
        "work_address": profile.get("work_address", ""),
        "work_lat": profile.get("work_lat"),
        "work_lon": profile.get("work_lon"),
        "budget_min": profile.get("budget_min"),
        "budget_max": profile.get("budget_max"),
        "bedrooms_min": profile.get("bedrooms_min"),
        "bedrooms_max": profile.get("bedrooms_max"),
        "max_commute_min": profile.get("max_commute_min"),
        "preferences_text": profile.get("preferences_text", ""),
        "preference_tags": profile.get("preference_tags", []),
        "recent_summaries": summaries,
        "active_bookmarks": bookmarks,
    }

    log.info("session_loaded", user_id=user_id, has_profile=bool(profile),
             bookmarks=len(bookmarks), summaries=len(summaries))
    return context


# =================================================================
# PROFILES
# =================================================================

def get_active_profile(cursor, user_id: str) -> QueryResult:
    """Load the user's active search profile."""
    log = logger.bind(service="user_data", query="active_profile")
    sql = """
        SELECT
            id, user_id, profile_name,
            work_address, work_lat, work_lon,
            budget_min, budget_max,
            bedrooms_min, bedrooms_max,
            max_commute_min,
            preferences_text, preference_tags,
            is_active, created_at, updated_at
        FROM USER_DATA.SEARCH_PROFILES
        WHERE user_id = %s AND is_active = TRUE
        ORDER BY updated_at DESC
        LIMIT 1
    """
    start = time.perf_counter()
    try:
        cursor.execute(sql, (user_id,))
        rows = cursor.fetchall()
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("query_failed", user_id=user_id, error=str(e)[:200])
        return QueryResult(success=False, query_type="active_profile",
                           error=str(e)[:500])
    data = _rows_to_dicts(cursor, rows)
    log.info("complete", user_id=user_id, found=len(data), ms=ms)
    return QueryResult(success=True, query_type="active_profile",
                       data=data, total_count=len(data), duration_ms=ms,
                       sql_executed=sql.strip())


def upsert_profile(
    cursor,
    user_id: str,
    *,
    profile_name: Optional[str] = None,
    work_address: Optional[str] = None,
    work_lat: Optional[float] = None,
    work_lon: Optional[float] = None,
    budget_min: Optional[int] = None,
    budget_max: Optional[int] = None,
    bedrooms_min: Optional[int] = None,
    bedrooms_max: Optional[int] = None,
    max_commute_min: Optional[int] = None,
    preferences_text: Optional[str] = None,
    preference_tags: Optional[list[str]] = None,
) -> QueryResult:
    """Create or update a search profile. Deactivates previous profiles.

    Uses INSERT ... SELECT FROM VALUES pattern for VARIANT column.
    """
    cfg = _cfg()
    prof_cfg = cfg.get("profiles", {})
    log = logger.bind(service="user_data", op="upsert_profile")

    max_tags = prof_cfg.get("max_preference_tags", 20)
    if preference_tags and len(preference_tags) > max_tags:
        return QueryResult(success=False, query_type="upsert_profile",
                           error=f"Too many preference tags: {len(preference_tags)} (max {max_tags})")

    profile_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    start = time.perf_counter()
    try:
        # Deactivate existing profiles
        cursor.execute(
            "UPDATE USER_DATA.SEARCH_PROFILES SET is_active = FALSE, "
            "updated_at = %s WHERE user_id = %s AND is_active = TRUE",
            (now, user_id),
        )

        # Build INSERT with conditional VARIANT handling
        # column1=id, column2=user_id, ... column12=preferences_text,
        # column13=preference_tags (VARIANT via PARSE_JSON or NULL)
        if preference_tags is not None:
            tags_expr = "PARSE_JSON(column13)"
            tags_param = (json.dumps(preference_tags),)
        else:
            tags_expr = "NULL"
            tags_param = ()

        sql = (
            "INSERT INTO USER_DATA.SEARCH_PROFILES "
            "(id, user_id, profile_name, work_address, work_lat, work_lon, "
            " budget_min, budget_max, bedrooms_min, bedrooms_max, "
            " max_commute_min, preferences_text, preference_tags, "
            " is_active, created_at, updated_at) "
            "SELECT column1, column2, column3, column4, column5, column6, "
            f"  column7, column8, column9, column10, column11, column12, "
            f"  {tags_expr}, TRUE, column{13 if preference_tags is None else 14}, "
            f"  column{14 if preference_tags is None else 15} "
            "FROM VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            + ("%s, " if preference_tags is not None else "")
            + "%s, %s)"
        )
        params = (
            profile_id, user_id, profile_name,
            work_address, work_lat, work_lon,
            budget_min, budget_max, bedrooms_min, bedrooms_max,
            max_commute_min, preferences_text,
            *tags_param,
            now, now,
        )

        cursor.execute(sql, params)
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("failed", user_id=user_id, error=str(e)[:200])
        return QueryResult(success=False, query_type="upsert_profile",
                           error=str(e)[:500])

    log.info("complete", user_id=user_id, profile_id=profile_id, ms=ms)
    return QueryResult(success=True, query_type="upsert_profile",
                       data=[{"profile_id": profile_id}],
                       total_count=1, duration_ms=ms)


# =================================================================
# BOOKMARKS
# =================================================================

def create_bookmark(
    cursor,
    user_id: str,
    listing_id: str,
    *,
    notes: Optional[str] = None,
    watch_days: Optional[int] = None,
) -> QueryResult:
    """Bookmark a listing and set watch period. Uses MERGE."""
    cfg = _cfg()
    bm_cfg = cfg.get("bookmarks", {})
    log = logger.bind(service="user_data", op="create_bookmark")

    max_watch = bm_cfg.get("max_watch_days", 30)
    default_watch = bm_cfg.get("default_watch_days", 14)
    watch_days = _clamp(watch_days or default_watch, 1, max_watch)
    max_per_user = bm_cfg.get("max_per_user", 20)

    bookmark_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    watch_end = (now + timedelta(days=watch_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    start = time.perf_counter()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM USER_DATA.BOOKMARKED_LISTINGS "
            "WHERE user_id = %s AND is_active = TRUE", (user_id,))
        count = cursor.fetchone()[0]
        if count >= max_per_user:
            return QueryResult(success=False, query_type="create_bookmark",
                               error=f"Bookmark limit reached: {count}/{max_per_user}")

        # No VARIANT columns in BOOKMARKED_LISTINGS — standard VALUES works
        cursor.execute(
            "MERGE INTO USER_DATA.BOOKMARKED_LISTINGS AS tgt "
            "USING (SELECT %s AS user_id, %s AS listing_id) AS src "
            "ON tgt.user_id = src.user_id AND tgt.listing_id = src.listing_id "
            "WHEN MATCHED THEN UPDATE SET "
            "  is_active = TRUE, notes = %s, watch_end = %s, removed_at = NULL "
            "WHEN NOT MATCHED THEN INSERT "
            "  (id, user_id, listing_id, notes, is_active, added_at, watch_end) "
            "  VALUES (%s, %s, %s, %s, TRUE, %s, %s)",
            (user_id, listing_id, notes, watch_end,
             bookmark_id, user_id, listing_id, notes, now_str, watch_end),
        )
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("failed", user_id=user_id, listing_id=listing_id,
                  error=str(e)[:200])
        return QueryResult(success=False, query_type="create_bookmark",
                           error=str(e)[:500])

    log.info("complete", user_id=user_id, listing_id=listing_id,
             watch_days=watch_days, ms=ms)
    return QueryResult(
        success=True, query_type="create_bookmark",
        data=[{"bookmark_id": bookmark_id, "watch_end": watch_end}],
        total_count=1, duration_ms=ms)


def remove_bookmark(cursor, user_id: str, listing_id: str) -> QueryResult:
    """Soft-delete a bookmark."""
    log = logger.bind(service="user_data", op="remove_bookmark")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    start = time.perf_counter()
    try:
        cursor.execute(
            "UPDATE USER_DATA.BOOKMARKED_LISTINGS "
            "SET is_active = FALSE, removed_at = %s "
            "WHERE user_id = %s AND listing_id = %s AND is_active = TRUE",
            (now, user_id, listing_id))
        affected = cursor.rowcount
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("failed", error=str(e)[:200])
        return QueryResult(success=False, query_type="remove_bookmark",
                           error=str(e)[:500])
    if affected == 0:
        return QueryResult(success=False, query_type="remove_bookmark",
                           error="Bookmark not found or already removed")
    log.info("complete", user_id=user_id, listing_id=listing_id, ms=ms)
    return QueryResult(success=True, query_type="remove_bookmark",
                       total_count=affected, duration_ms=ms)


# =================================================================
# CONVERSATIONS
# =================================================================

def append_message(
    cursor,
    user_id: str,
    session_id: str,
    role: str,
    content: str,
    *,
    tool_calls: Optional[list[dict]] = None,
) -> QueryResult:
    """Append a message to the conversation log.

    Uses INSERT ... SELECT FROM VALUES for the VARIANT tool_calls column.
    """
    cfg = _cfg()
    conv_cfg = cfg.get("conversations", {})
    log = logger.bind(service="user_data", op="append_message")

    max_len = conv_cfg.get("max_message_length", 10000)
    content = content[:max_len]
    msg_id = str(uuid.uuid4())

    start = time.perf_counter()
    try:
        if tool_calls is not None:
            # VARIANT has data — use PARSE_JSON in SELECT
            cursor.execute(
                "INSERT INTO USER_DATA.CONVERSATIONS "
                "(id, user_id, session_id, role, content, tool_calls) "
                "SELECT column1, column2, column3, column4, column5, "
                "  PARSE_JSON(column6) "
                "FROM VALUES (%s, %s, %s, %s, %s, %s)",
                (msg_id, user_id, session_id, role, content,
                 json.dumps(tool_calls)),
            )
        else:
            # VARIANT is NULL — no PARSE_JSON, use literal NULL
            cursor.execute(
                "INSERT INTO USER_DATA.CONVERSATIONS "
                "(id, user_id, session_id, role, content, tool_calls) "
                "SELECT column1, column2, column3, column4, column5, NULL "
                "FROM VALUES (%s, %s, %s, %s, %s)",
                (msg_id, user_id, session_id, role, content),
            )
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("failed", error=str(e)[:200])
        return QueryResult(success=False, query_type="append_message",
                           error=str(e)[:500])

    log.info("complete", session_id=session_id, role=role, ms=ms)
    return QueryResult(success=True, query_type="append_message",
                       data=[{"message_id": msg_id}],
                       total_count=1, duration_ms=ms)


# =================================================================
# SESSION SUMMARIES
# =================================================================

def write_session_summary(
    cursor,
    user_id: str,
    session_id: str,
    summary: str,
    *,
    decisions: Optional[list[dict]] = None,
    pending_actions: Optional[list[dict]] = None,
    listings_discussed: Optional[list[str]] = None,
    message_count: int = 0,
) -> QueryResult:
    """Write or replace a session summary.

    Uses MERGE with PARSE_JSON in the USING subquery for VARIANT columns.
    """
    log = logger.bind(service="user_data", op="write_summary")
    summary_id = str(uuid.uuid4())

    # Build dynamic VARIANT expressions for the USING subquery.
    # For each VARIANT column: if data exists, SELECT PARSE_JSON(columnN),
    # otherwise SELECT NULL AS colname.
    # Track which columns get params and which are literal NULL.
    using_cols = [
        "column1 AS session_id",
        "column2 AS id",
        "column3 AS user_id",
        "column4 AS summary",
    ]
    params_list = [session_id, summary_id, user_id, summary]
    col_idx = 5

    # decisions
    if decisions is not None:
        using_cols.append(f"PARSE_JSON(column{col_idx}) AS decisions")
        params_list.append(json.dumps(decisions))
        col_idx += 1
    else:
        using_cols.append("NULL AS decisions")

    # pending_actions
    if pending_actions is not None:
        using_cols.append(f"PARSE_JSON(column{col_idx}) AS pending_actions")
        params_list.append(json.dumps(pending_actions))
        col_idx += 1
    else:
        using_cols.append("NULL AS pending_actions")

    # listings_discussed
    if listings_discussed is not None:
        using_cols.append(f"PARSE_JSON(column{col_idx}) AS listings_discussed")
        params_list.append(json.dumps(listings_discussed))
        col_idx += 1
    else:
        using_cols.append("NULL AS listings_discussed")

    # message_count (not VARIANT, always a param)
    using_cols.append(f"column{col_idx} AS message_count")
    params_list.append(message_count)
    col_idx += 1

    using_select = ", ".join(using_cols)
    placeholders = ", ".join(["%s"] * len(params_list))

    sql = (
        f"MERGE INTO USER_DATA.SESSION_SUMMARIES AS tgt "
        f"USING (SELECT {using_select} FROM VALUES ({placeholders})) AS src "
        f"ON tgt.session_id = src.session_id "
        f"WHEN MATCHED THEN UPDATE SET "
        f"  summary = src.summary, decisions = src.decisions, "
        f"  pending_actions = src.pending_actions, "
        f"  listings_discussed = src.listings_discussed, "
        f"  message_count = src.message_count "
        f"WHEN NOT MATCHED THEN INSERT "
        f"  (id, user_id, session_id, summary, decisions, "
        f"   pending_actions, listings_discussed, message_count) "
        f"  VALUES (src.id, src.user_id, src.session_id, src.summary, "
        f"          src.decisions, src.pending_actions, "
        f"          src.listings_discussed, src.message_count)"
    )

    start = time.perf_counter()
    try:
        cursor.execute(sql, tuple(params_list))
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("failed", error=str(e)[:200])
        return QueryResult(success=False, query_type="write_summary",
                           error=str(e)[:500])

    log.info("complete", session_id=session_id, ms=ms)
    return QueryResult(success=True, query_type="write_summary",
                       total_count=1, duration_ms=ms)


def get_recent_summaries(cursor, user_id: str) -> QueryResult:
    """Load most recent session summaries for context."""
    cfg = _cfg()
    sess_cfg = cfg.get("sessions", {})
    log = logger.bind(service="user_data", query="recent_summaries")
    max_load = sess_cfg.get("max_summaries_loaded", 3)

    sql = f"""
        SELECT
            session_id, summary, decisions, pending_actions,
            listings_discussed, message_count, created_at
        FROM USER_DATA.SESSION_SUMMARIES
        WHERE user_id = %s
        ORDER BY created_at DESC
        LIMIT {max_load}
    """
    start = time.perf_counter()
    try:
        cursor.execute(sql, (user_id,))
        rows = cursor.fetchall()
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("query_failed", user_id=user_id, error=str(e)[:200])
        return QueryResult(success=False, query_type="recent_summaries",
                           error=str(e)[:500])
    data = _rows_to_dicts(cursor, rows)
    log.info("complete", user_id=user_id, summaries=len(data), ms=ms)
    return QueryResult(success=True, query_type="recent_summaries",
                       data=data, total_count=len(data), duration_ms=ms,
                       sql_executed=sql.strip())


# =================================================================
# CONFIGURED ROUTES (write path -- read is in listing_queries.py)
# =================================================================

def save_configured_route(
    cursor,
    user_id: str,
    listing_id: str,
    dest_label: str,
    *,
    dest_address: Optional[str] = None,
    dest_lat: Optional[float] = None,
    dest_lon: Optional[float] = None,
    departure_hour: int = 8,
    travel_mode: str = "transit",
    duration_min: Optional[float] = None,
    distance_text: Optional[str] = None,
    transit_lines: Optional[list[str]] = None,
    waypoints: Optional[list[dict]] = None,
    waypoint_scores: Optional[list[dict]] = None,
) -> QueryResult:
    """Persist a computed route. Uses MERGE with VARIANT in USING subquery."""
    log = logger.bind(service="user_data", op="save_route")

    route_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build USING subquery dynamically for VARIANT columns
    using_cols = [
        "column1 AS user_id",
        "column2 AS listing_id",
        "column3 AS dest_label",
        "column4 AS id",
        "column5 AS dest_address",
        "column6 AS dest_lat",
        "column7 AS dest_lon",
        "column8 AS departure_hour",
        "column9 AS travel_mode",
        "column10 AS duration_min",
        "column11 AS distance_text",
    ]
    params_list = [
        user_id, listing_id, dest_label, route_id,
        dest_address, dest_lat, dest_lon,
        departure_hour, travel_mode, duration_min, distance_text,
    ]
    col_idx = 12

    # transit_lines (VARIANT)
    if transit_lines is not None:
        using_cols.append(f"PARSE_JSON(column{col_idx}) AS transit_lines")
        params_list.append(json.dumps(transit_lines))
        col_idx += 1
    else:
        using_cols.append("NULL AS transit_lines")

    # waypoints (VARIANT)
    if waypoints is not None:
        using_cols.append(f"PARSE_JSON(column{col_idx}) AS waypoints")
        params_list.append(json.dumps(waypoints))
        col_idx += 1
    else:
        using_cols.append("NULL AS waypoints")

    # waypoint_scores (VARIANT)
    if waypoint_scores is not None:
        using_cols.append(f"PARSE_JSON(column{col_idx}) AS waypoint_scores")
        params_list.append(json.dumps(waypoint_scores))
        col_idx += 1
    else:
        using_cols.append("NULL AS waypoint_scores")

    # computed_at (not VARIANT)
    using_cols.append(f"column{col_idx} AS computed_at")
    params_list.append(now)
    col_idx += 1

    using_select = ", ".join(using_cols)
    placeholders = ", ".join(["%s"] * len(params_list))

    sql = (
        f"MERGE INTO USER_DATA.CONFIGURED_ROUTES AS tgt "
        f"USING (SELECT {using_select} FROM VALUES ({placeholders})) AS src "
        f"ON tgt.user_id = src.user_id "
        f"   AND tgt.listing_id = src.listing_id "
        f"   AND tgt.dest_label = src.dest_label "
        f"WHEN MATCHED THEN UPDATE SET "
        f"  dest_address = src.dest_address, dest_lat = src.dest_lat, "
        f"  dest_lon = src.dest_lon, departure_hour = src.departure_hour, "
        f"  travel_mode = src.travel_mode, duration_min = src.duration_min, "
        f"  distance_text = src.distance_text, "
        f"  transit_lines = src.transit_lines, waypoints = src.waypoints, "
        f"  waypoint_scores = src.waypoint_scores, "
        f"  route_source = 'google_maps', is_active = TRUE, "
        f"  computed_at = src.computed_at "
        f"WHEN NOT MATCHED THEN INSERT "
        f"  (id, user_id, listing_id, dest_label, dest_address, dest_lat, "
        f"   dest_lon, departure_hour, travel_mode, duration_min, distance_text, "
        f"   transit_lines, waypoints, waypoint_scores, "
        f"   route_source, is_active, computed_at) "
        f"  VALUES (src.id, src.user_id, src.listing_id, src.dest_label, "
        f"          src.dest_address, src.dest_lat, src.dest_lon, "
        f"          src.departure_hour, src.travel_mode, src.duration_min, "
        f"          src.distance_text, src.transit_lines, src.waypoints, "
        f"          src.waypoint_scores, 'google_maps', TRUE, src.computed_at)"
    )

    start = time.perf_counter()
    try:
        cursor.execute(sql, tuple(params_list))
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("failed", error=str(e)[:200])
        return QueryResult(success=False, query_type="save_route",
                           error=str(e)[:500])

    log.info("complete", user_id=user_id, listing_id=listing_id,
             dest=dest_label, ms=ms)
    return QueryResult(success=True, query_type="save_route",
                       data=[{"route_id": route_id}],
                       total_count=1, duration_ms=ms)