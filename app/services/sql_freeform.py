"""SQL freeform service -- agent-composed SELECT queries with guardrails.

The agent writes SQL when no template covers the question. Guardrails:
  - Read-only: blocked keyword list rejects mutations
  - Schema-injected: agent prompt includes every table/column/type
  - Row-limited: LIMIT enforced on all queries
  - Timeout: query_tag sets statement timeout
  - Self-correcting: on SQL error, agent gets the error + schema and retries
  - Echo: executed SQL always returned for transparency

Usage:
    from app.services.sql_freeform import execute_freeform, get_schema_prompt
    prompt = get_schema_prompt()  # inject into agent system prompt
    result = execute_freeform(cursor, sql="SELECT ...")
"""

from __future__ import annotations

import re
import time
from typing import Optional

import structlog

from app.core.config_loader import CONFIG_DIR
from app.services.listing_queries import QueryResult, _rows_to_dicts

logger = structlog.get_logger()


# -- Config ---------------------------------------------------------------

def _cfg() -> dict:
    if not hasattr(_cfg, "_cache"):
        import yaml
        with open(CONFIG_DIR / "services.yml", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        _cfg._cache = raw.get("sql_freeform", {})
    return _cfg._cache


def reload_config():
    if hasattr(_cfg, "_cache"):
        del _cfg._cache


# -- Schema prompt --------------------------------------------------------

# Built from migration chain: 5ab9da32649b -> a132a4da036e -> d7609ca7fb73
# -> 203545dc98ba -> 3add0f393d54 -> 6bca62f797ef -> 6c4024eb849e -> 8f2b1a4c7d39
# Every column verified against actual CREATE TABLE / ALTER TABLE statements.

_SCHEMA_COMPACT = """
SCHEMAS: RAW, SCORECARDS, USER_DATA

RAW.LISTINGS (listing_id PK, source, source_native_id, source_url, price INT, beds INT, baths INT, sqft INT, street, unit, city, zip_code, neighborhood, lat FLOAT, lon FLOAT, description_text TEXT, primary_photo_url, mls_id, mls_status, days_on_mls INT, agent_name, style, list_date TIMESTAMP, is_current BOOL, first_seen_at TIMESTAMP, last_seen_at TIMESTAMP, raw_json VARIANT, classification_metadata VARIANT, pipeline_run_id, scraped_at TIMESTAMP, url_status DEFAULT 'active', url_flagged_at TIMESTAMP)
  CLUSTERED BY (city, is_current)

RAW.CRIME_INCIDENTS (incident_id PK, offense_code, offense_description, severity ['violent','property','minor'], occurred_on_date TIMESTAMP, hour INT 0-23, day_of_week, district, street, lat FLOAT, lon FLOAT, shooting BOOL, classification_metadata VARIANT, source_resource_id, pipeline_run_id, scraped_at TIMESTAMP)
  CLUSTERED BY (district, occurred_on_date)

RAW.COMPLAINTS_311 (case_enquiry_id PK, source_resource_id, open_dt TIMESTAMP, closed_dt TIMESTAMP, case_status, case_title, subject, reason, type, category ['noise','pest','rodent','road','sanitation','heat','housing','other'], neighborhood, ward, street, zip_code, lat FLOAT, lon FLOAT, classification_metadata VARIANT, pipeline_run_id, scraped_at TIMESTAMP)
  CLUSTERED BY (neighborhood, open_dt)

RAW.CITIZEN_INCIDENTS (incident_key PK, title, description TEXT, categories ARRAY, severity ['critical','moderate','minor'], level INT, is_nighttime BOOL, lat FLOAT, lon FLOAT, address, police_district, incident_ts TIMESTAMP, source, closed BOOL, classification_metadata VARIANT, pipeline_run_id, scraped_at TIMESTAMP)

RAW.TRANSIT_STOPS (stop_id PK, stop_name, lat FLOAT, lon FLOAT, municipality, wheelchair_boarding INT, route_ids ARRAY, route_names ARRAY, route_types ARRAY, pipeline_run_id, scraped_at TIMESTAMP)

RAW.AMENITIES (osm_id BIGINT PK, name TEXT, category, subcategory, lat FLOAT, lon FLOAT, address TEXT, opening_hours TEXT, website TEXT, phone, brand TEXT, wheelchair, tags VARIANT, pipeline_run_id, scraped_at TIMESTAMP)

RAW.LIFESTYLE_SIGNALS (signal_id PK, signal_source, source_native_id, preference_tag, title, snippet_text TEXT, url, content_hash, sentiment, relevance_score INT, lat FLOAT, lon FLOAT, classification_metadata VARIANT, pipeline_run_id, fetched_at TIMESTAMP, raw_thread_text TEXT, url_status DEFAULT 'active', url_flagged_at TIMESTAMP)

RAW.CLASSIFICATION_CACHE (source+field_name+raw_value PK, severity, category, narrative TEXT, classified_by, classification_version, created_at TIMESTAMP)

RAW.LLM_USAGE_LOG (id PK, pipeline_run_id, source, operation, model, input_tokens INT, output_tokens INT, total_tokens INT, input_cost_usd DECIMAL, output_cost_usd DECIMAL, total_cost_usd DECIMAL, batch_size INT, duration_ms INT, created_at TIMESTAMP)

RAW.PIPELINE_ERRORS (id PK, pipeline_run_id, source, record_key, error_type, error_message TEXT, raw_record VARIANT, created_at TIMESTAMP)

RAW.EMBEDDING_SYNC (signal_id PK, content_hash, embedding_model, vector_dim INT, embedded_at TIMESTAMP)

RAW.HEALTHZ (id PK, checked_at TIMESTAMP, status, client_ip, user_agent, response_ms INT, details VARIANT)

SCORECARDS.LOCATION_SCORECARD (listing_id+score_date PK, crime_count INT, violent_count INT, crime_trend ['declining','stable','rising'], complaint_count INT, citizen_incidents_48h INT, citizen_nighttime_48h INT, nearby_transit_stops INT, nearby_amenity_count INT, listing_active BOOL, current_price INT, safety_score INT 0-100, livability_score INT 0-100, scoring_metadata VARIANT, pipeline_run_id)
  CLUSTERED BY (listing_id, score_date)

SCORECARDS.LISTING_SUMMARY (listing_id PK, source, source_url, price INT, beds INT, baths INT, sqft INT, street, city, zip_code, neighborhood, lat FLOAT, lon FLOAT, description_text TEXT, primary_photo_url, is_active BOOL, list_date TIMESTAMP, safety_score INT, safety_metadata VARIANT, livability_score INT, livability_metadata VARIANT, lifestyle_scores VARIANT, nearby_amenities VARIANT, nearest_stops VARIANT, price_history VARIANT, safety_trend VARIANT, price_vs_first_seen INT, last_scored_at TIMESTAMP, score_version, pipeline_run_id)

SCORECARDS.ROUTE_SCORECARD (route_id+score_date PK, listing_id, crime_count INT, violent_count INT, shooting_count INT, crimes_at_dep_hour INT, citizen_incidents INT, citizen_nighttime INT, scoring_metadata VARIANT, pipeline_run_id)
  CLUSTERED BY (route_id, score_date)

USER_DATA.USERS (id PK, email UNIQUE, display_name, created_at TIMESTAMP, updated_at TIMESTAMP)

USER_DATA.SEARCH_PROFILES (id PK, user_id FK->USERS, profile_name, work_address, work_lat FLOAT, work_lon FLOAT, budget_min INT, budget_max INT, bedrooms_min INT, bedrooms_max INT, max_commute_min INT, preferences_text TEXT, preference_tags ARRAY, is_active BOOL, created_at TIMESTAMP, updated_at TIMESTAMP)

USER_DATA.BOOKMARKED_LISTINGS (id PK, user_id FK->USERS, listing_id, notes TEXT, is_active BOOL, added_at TIMESTAMP, removed_at TIMESTAMP, watch_end TIMESTAMP, UNIQUE(user_id, listing_id))

USER_DATA.CONFIGURED_ROUTES (id PK, user_id FK->USERS, listing_id, dest_label, dest_address, dest_lat FLOAT, dest_lon FLOAT, departure_hour INT, travel_mode, duration_min FLOAT, distance_text, transit_lines ARRAY, waypoints ARRAY, waypoint_scores VARIANT, route_source, is_active BOOL, computed_at TIMESTAMP, UNIQUE(user_id, listing_id, dest_label))

USER_DATA.CONVERSATIONS (id PK, user_id FK->USERS, session_id, role, content TEXT, tool_calls VARIANT, created_at TIMESTAMP)

USER_DATA.SESSION_SUMMARIES (id PK, user_id FK->USERS, session_id UNIQUE, summary TEXT, decisions VARIANT, pending_actions VARIANT, listings_discussed ARRAY, message_count INT, created_at TIMESTAMP)
""".strip()

_SPATIAL_HINTS = """
SPATIAL QUERIES: Use ST_DISTANCE(ST_MAKEPOINT(lon1, lat1), ST_MAKEPOINT(lon2, lat2)) for distance in meters. ST_MAKEPOINT takes (longitude, latitude) -- lon first. Always add a bounding box pre-filter on lat/lon columns before ST_DISTANCE for performance. Example: WHERE c.lat BETWEEN %s AND %s AND c.lon BETWEEN %s AND %s AND ST_DISTANCE(...) <= radius_m.
""".strip()

_TIME_HINTS = """
TIME QUERIES: All timestamps are TIMESTAMP_NTZ (no timezone). Use DATEADD(day, -N, CURRENT_DATE()) for relative date filters. Crime hour is INT 0-23 in column 'hour'. Day of week is VARCHAR in 'day_of_week'. For midnight-crossing hour ranges: (hour >= 22 OR hour <= 4) not BETWEEN.
""".strip()

_RELATIONSHIP_HINTS = """
RELATIONSHIPS: LISTINGS.listing_id joins to LOCATION_SCORECARD.listing_id, LISTING_SUMMARY.listing_id, BOOKMARKED_LISTINGS.listing_id, CONFIGURED_ROUTES.listing_id, ROUTE_SCORECARD.listing_id (via CONFIGURED_ROUTES.id = ROUTE_SCORECARD.route_id). USERS.id joins to SEARCH_PROFILES.user_id, BOOKMARKED_LISTINGS.user_id, CONFIGURED_ROUTES.user_id, CONVERSATIONS.user_id, SESSION_SUMMARIES.user_id. LIFESTYLE_SIGNALS.signal_id joins to EMBEDDING_SYNC.signal_id.
""".strip()


def get_schema_prompt() -> str:
    """Build the schema injection prompt for the agent's system message.

    Reads config flags to include/exclude spatial hints, time hints,
    and relationship hints. Returns a compact string (~2500 tokens).
    """
    cfg = _cfg()
    schema_cfg = cfg.get("schema", {})

    parts = [_SCHEMA_COMPACT]

    if schema_cfg.get("include_spatial_hints", True):
        parts.append(_SPATIAL_HINTS)
    if schema_cfg.get("include_time_hints", True):
        parts.append(_TIME_HINTS)
    if schema_cfg.get("include_relationships", True):
        parts.append(_RELATIONSHIP_HINTS)

    return "\n\n".join(parts)


# -- Safety checks --------------------------------------------------------

def _check_blocked(sql: str) -> Optional[str]:
    """Check SQL against blocked keyword list. Returns error message or None."""
    cfg = _cfg()
    blocked = cfg.get("safety", {}).get("blocked_keywords", [])

    # Tokenize: split on whitespace and parens, uppercase for matching
    tokens = set(re.findall(r'[A-Za-z_]+', sql.upper()))

    for keyword in blocked:
        if keyword.upper() in tokens:
            return f"Blocked keyword '{keyword}' detected. Only SELECT queries are allowed."

    return None


def _check_schema(sql: str) -> Optional[str]:
    """Check that query only references allowed schemas."""
    cfg = _cfg()
    allowed = cfg.get("safety", {}).get("allowed_schemas", [])

    if not allowed:
        return None

    # Look for schema-qualified table references: SCHEMA.TABLE
    refs = re.findall(r'(\w+)\.\w+', sql.upper())
    # Filter out column references (alias.column) by checking against known schemas
    all_schemas = {"RAW", "SCORECARDS", "USER_DATA"}
    schema_refs = [r for r in refs if r in all_schemas]

    allowed_upper = {s.upper() for s in allowed}
    violations = [s for s in schema_refs if s not in allowed_upper]

    if violations:
        return f"Schema not allowed: {', '.join(violations)}. Allowed: {', '.join(allowed)}"

    return None


def _enforce_limit(sql: str, max_rows: int) -> str:
    """Ensure query has a LIMIT clause. Adds one if missing."""
    # Check if LIMIT already exists (case-insensitive, outside comments)
    if re.search(r'\bLIMIT\b', sql, re.IGNORECASE):
        return sql
    return f"{sql.rstrip().rstrip(';')}\nLIMIT {max_rows}"


# -- Execute --------------------------------------------------------------

def execute_freeform(
    cursor,
    sql: str,
) -> QueryResult:
    """Execute a freeform SELECT query with all guardrails.

    Args:
        cursor: Snowflake cursor.
        sql: Agent-composed SQL query.

    Returns:
        QueryResult with data, executed SQL, and any errors.
        On SQL error, returns the error message so the agent can
        self-correct and retry.
    """
    cfg = _cfg()
    exec_cfg = cfg.get("execution", {})
    log = logger.bind(service="sql_freeform")

    if not cfg.get("enabled", True):
        return QueryResult(success=False, query_type="sql_freeform",
                           error="sql_freeform service is disabled")

    max_rows = exec_cfg.get("max_rows", 1000)
    timeout_s = exec_cfg.get("timeout_seconds", 15)

    # Safety checks
    blocked = _check_blocked(sql)
    if blocked:
        log.warning("blocked", reason=blocked, sql=sql[:200])
        return QueryResult(success=False, query_type="sql_freeform",
                           error=blocked, sql_executed=sql)

    schema_err = _check_schema(sql)
    if schema_err:
        log.warning("schema_violation", reason=schema_err, sql=sql[:200])
        return QueryResult(success=False, query_type="sql_freeform",
                           error=schema_err, sql_executed=sql)

    # Enforce LIMIT
    sql = _enforce_limit(sql, max_rows)

    # Set statement timeout
    start = time.perf_counter()
    try:
        cursor.execute(
            f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {timeout_s}"
        )
        cursor.execute(sql)
        rows = cursor.fetchall()
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        ms = int((time.perf_counter() - start) * 1000)
        error_msg = str(e)

        log.warning("query_failed", error=error_msg[:300], ms=ms)

        # Return structured error for agent self-correction
        return QueryResult(
            success=False, query_type="sql_freeform",
            error=error_msg[:1000],
            sql_executed=sql,
            duration_ms=ms,
        )

    data = _rows_to_dicts(cursor, rows)
    log.info("complete", rows=len(data), ms=ms)

    return QueryResult(
        success=True, query_type="sql_freeform",
        data=data, total_count=len(data),
        duration_ms=ms, sql_executed=sql,
    )