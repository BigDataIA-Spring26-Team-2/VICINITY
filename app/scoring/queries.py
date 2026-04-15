"""Scoring SQL queries — one function per dimension.

Each function takes a cursor + ScoringConfig, executes the spatial
query, and returns {listing_id: {metric_dict}} for downstream
percentile ranking and metadata assembly.

Two-tier complaint matching: ST_DISTANCE for records with coordinates,
neighborhood/zip fallback for records without.

All queries use bbox pre-filter then exact ST_DISTANCE for performance.
"""

import json
import time
from collections import defaultdict

import structlog

from app.scoring.config import ScoringConfig

logger = structlog.get_logger()

# Batch dimensions computed by the nightly pipeline
BATCH_DIMENSIONS = ("safety", "livability", "transit")


# ── Safety ───────────────────────────────────────────────────

def query_safety(cursor, cfg: ScoringConfig) -> dict[str, dict]:
    """Crime counts + citizen incidents per listing.

    Returns {listing_id: {crime_count, violent_count, shooting_count,
    offense_types, citizen_total, citizen_nighttime, citizen_critical,
    street, neighborhood, zip_code, price, beds, baths, lat, lon}}.
    """
    dlat, dlon = cfg.bbox_deltas("safety")

    sql = f"""
        SELECT
            l.listing_id, l.street, l.neighborhood, l.zip_code,
            l.price, l.beds, l.baths, l.lat, l.lon,
            COUNT(c.incident_id) AS crime_count,
            COUNT(CASE WHEN c.severity = 'violent' THEN 1 END) AS violent_count,
            COUNT(CASE WHEN c.shooting = TRUE THEN 1 END) AS shooting_count,
            COUNT(DISTINCT c.offense_description) AS offense_types
        FROM RAW.LISTINGS l
        LEFT JOIN RAW.CRIME_INCIDENTS c
            ON c.lat BETWEEN l.lat - {dlat} AND l.lat + {dlat}
            AND c.lon BETWEEN l.lon - {dlon} AND l.lon + {dlon}
            AND ST_DISTANCE(
                ST_MAKEPOINT(l.lon, l.lat),
                ST_MAKEPOINT(c.lon, c.lat)
            ) <= {cfg.safety_radius_m}
            AND c.occurred_on_date >= DATEADD(day, -{cfg.crime_window_days}, CURRENT_DATE())
        WHERE l.is_current = TRUE AND l.lat IS NOT NULL
        GROUP BY l.listing_id, l.street, l.neighborhood, l.zip_code,
                 l.price, l.beds, l.baths, l.lat, l.lon
    """

    log = logger.bind(dimension="safety")
    log.info("query_start", radius_m=cfg.safety_radius_m, window_days=cfg.crime_window_days)
    start = time.perf_counter()
    cursor.execute(sql)
    rows = cursor.fetchall()
    cols = [d[0].lower() for d in cursor.description]
    ms = int((time.perf_counter() - start) * 1000)
    log.info("query_complete", listings=len(rows), duration_ms=ms)

    results = {row[0]: dict(zip(cols, row)) for row in rows}
    _merge_citizen(cursor, cfg, results)
    return results


def _merge_citizen(cursor, cfg: ScoringConfig, results: dict[str, dict]):
    """Merge citizen incident counts into safety results."""
    dlat, dlon = cfg.bbox_deltas("safety")

    sql = f"""
        SELECT
            l.listing_id,
            COUNT(ci.incident_key) AS citizen_total,
            COUNT(CASE WHEN ci.is_nighttime = TRUE THEN 1 END) AS citizen_nighttime,
            COUNT(CASE WHEN ci.severity = 'critical' THEN 1 END) AS citizen_critical
        FROM RAW.LISTINGS l
        LEFT JOIN RAW.CITIZEN_INCIDENTS ci
            ON ci.lat BETWEEN l.lat - {dlat} AND l.lat + {dlat}
            AND ci.lon BETWEEN l.lon - {dlon} AND l.lon + {dlon}
            AND ST_DISTANCE(
                ST_MAKEPOINT(l.lon, l.lat),
                ST_MAKEPOINT(ci.lon, ci.lat)
            ) <= {cfg.safety_radius_m}
            AND ci.incident_ts >= DATEADD(hour, -{cfg.citizen_window_hours}, CURRENT_TIMESTAMP())
        WHERE l.is_current = TRUE AND l.lat IS NOT NULL
        GROUP BY l.listing_id
    """

    log = logger.bind(dimension="safety", sub="citizen")
    start = time.perf_counter()
    cursor.execute(sql)
    rows = cursor.fetchall()
    ms = int((time.perf_counter() - start) * 1000)
    log.info("citizen_query_complete", listings=len(rows), duration_ms=ms)

    citizen = {row[0]: {
        "citizen_total": row[1],
        "citizen_nighttime": row[2],
        "citizen_critical": row[3],
    } for row in rows}

    for lid, data in results.items():
        c = citizen.get(lid, {})
        data["citizen_total"] = c.get("citizen_total", 0)
        data["citizen_nighttime"] = c.get("citizen_nighttime", 0)
        data["citizen_critical"] = c.get("citizen_critical", 0)


# ── Livability ───────────────────────────────────────────────

def query_livability(cursor, cfg: ScoringConfig) -> dict[str, dict]:
    """311 complaints (two-tier) + essentials per listing.

    Returns {listing_id: {complaint_count, noise_count, pest_count,
    infra_count, heat_count, housing_count, essentials_found,
    essentials_list, total_amenities}}.
    """
    dlat, dlon = cfg.bbox_deltas("livability")
    edlat, edlon = cfg.bbox_deltas("essentials")

    complaints = _query_complaints(cursor, cfg, dlat, dlon)
    essentials = _query_essentials(cursor, cfg, edlat, edlon)

    all_ids = set(complaints.keys()) | set(essentials.keys())
    empty_complaints = {
        "complaint_count": 0, "noise_count": 0, "pest_count": 0,
        "infra_count": 0, "heat_count": 0, "housing_count": 0,
    }
    empty_essentials = {
        "essentials_found": 0, "essentials_list": [], "total_amenities": 0,
    }

    merged = {}
    for lid in all_ids:
        merged[lid] = {
            **empty_complaints, **complaints.get(lid, {}),
            **empty_essentials, **essentials.get(lid, {}),
        }

    return merged


def _query_complaints(
    cursor, cfg: ScoringConfig, dlat: float, dlon: float,
) -> dict[str, dict]:
    """Two-tier complaint query with full category breakdown."""
    sql = f"""
        WITH geo_matches AS (
            SELECT l.listing_id, c.case_enquiry_id, c.category
            FROM RAW.LISTINGS l
            JOIN RAW.COMPLAINTS_311 c
                ON c.lat IS NOT NULL AND c.lat != 0
                AND c.lat BETWEEN l.lat - {dlat} AND l.lat + {dlat}
                AND c.lon BETWEEN l.lon - {dlon} AND l.lon + {dlon}
                AND ST_DISTANCE(
                    ST_MAKEPOINT(l.lon, l.lat),
                    ST_MAKEPOINT(c.lon, c.lat)
                ) <= {cfg.livability_radius_m}
                AND c.open_dt >= DATEADD(day, -{cfg.complaint_window_days}, CURRENT_DATE())
            WHERE l.is_current = TRUE AND l.lat IS NOT NULL
        ),
        area_matches AS (
            SELECT l.listing_id, c.case_enquiry_id, c.category
            FROM RAW.LISTINGS l
            JOIN RAW.COMPLAINTS_311 c
                ON (c.lat IS NULL OR c.lat = 0)
                AND (c.neighborhood = l.neighborhood
                     OR c.zip_code = l.zip_code)
                AND c.open_dt >= DATEADD(day, -{cfg.complaint_window_days}, CURRENT_DATE())
            WHERE l.is_current = TRUE
                AND (l.neighborhood IS NOT NULL OR l.zip_code IS NOT NULL)
        ),
        all_matches AS (
            SELECT * FROM geo_matches
            UNION
            SELECT * FROM area_matches
        )
        SELECT
            listing_id,
            COUNT(DISTINCT case_enquiry_id) AS complaint_count,
            COUNT(DISTINCT CASE WHEN category = 'noise'
                  THEN case_enquiry_id END) AS noise_count,
            COUNT(DISTINCT CASE WHEN category IN ('pest', 'rodent')
                  THEN case_enquiry_id END) AS pest_count,
            COUNT(DISTINCT CASE WHEN category IN ('road', 'sanitation')
                  THEN case_enquiry_id END) AS infra_count,
            COUNT(DISTINCT CASE WHEN category = 'heat'
                  THEN case_enquiry_id END) AS heat_count,
            COUNT(DISTINCT CASE WHEN category = 'housing'
                  THEN case_enquiry_id END) AS housing_count
        FROM all_matches
        GROUP BY listing_id
    """

    log = logger.bind(dimension="livability", sub="complaints")
    log.info("query_start", radius_m=cfg.livability_radius_m,
             window_days=cfg.complaint_window_days)
    start = time.perf_counter()
    cursor.execute(sql)
    rows = cursor.fetchall()
    ms = int((time.perf_counter() - start) * 1000)
    log.info("query_complete", listings=len(rows), duration_ms=ms)

    return {row[0]: {
        "complaint_count": row[1], "noise_count": row[2],
        "pest_count": row[3], "infra_count": row[4],
        "heat_count": row[5], "housing_count": row[6],
    } for row in rows}


def _query_essentials(
    cursor, cfg: ScoringConfig, dlat: float, dlon: float,
) -> dict[str, dict]:
    """Essentials coverage within walkable radius."""
    ess_in = ", ".join(f"'{e}'" for e in cfg.essentials)

    sql = f"""
        SELECT
            l.listing_id,
            COUNT(DISTINCT a.subcategory) AS essentials_found,
            ARRAY_AGG(DISTINCT a.subcategory) AS essentials_list,
            COUNT(*) AS total_amenities
        FROM RAW.LISTINGS l
        LEFT JOIN RAW.AMENITIES a
            ON a.lat BETWEEN l.lat - {dlat} AND l.lat + {dlat}
            AND a.lon BETWEEN l.lon - {dlon} AND l.lon + {dlon}
            AND ST_DISTANCE(
                ST_MAKEPOINT(l.lon, l.lat),
                ST_MAKEPOINT(a.lon, a.lat)
            ) <= {cfg.essentials_radius_m}
            AND a.subcategory IN ({ess_in})
        WHERE l.is_current = TRUE AND l.lat IS NOT NULL
        GROUP BY l.listing_id
    """

    log = logger.bind(dimension="livability", sub="essentials")
    log.info("query_start", radius_m=cfg.essentials_radius_m)
    start = time.perf_counter()
    cursor.execute(sql)
    rows = cursor.fetchall()
    ms = int((time.perf_counter() - start) * 1000)
    log.info("query_complete", listings=len(rows), duration_ms=ms)

    result = {}
    for row in rows:
        elist = row[2] if isinstance(row[2], list) else (
            json.loads(row[2]) if row[2] else [])
        result[row[0]] = {
            "essentials_found": row[1],
            "essentials_list": [str(e) for e in elist],
            "total_amenities": row[3],
        }
    return result


# ── Transit ──────────────────────────────────────────────────

def query_transit(cursor, cfg: ScoringConfig) -> dict[str, dict]:
    """Transit stop counts and line names per listing."""
    dlat, dlon = cfg.bbox_deltas("transit")

    sql = f"""
        SELECT
            l.listing_id,
            COUNT(DISTINCT t.stop_id) AS stop_count,
            ARRAY_AGG(DISTINCT t.stop_name) AS stop_names
        FROM RAW.LISTINGS l
        LEFT JOIN RAW.TRANSIT_STOPS t
            ON t.lat BETWEEN l.lat - {dlat} AND l.lat + {dlat}
            AND t.lon BETWEEN l.lon - {dlon} AND l.lon + {dlon}
            AND ST_DISTANCE(
                ST_MAKEPOINT(l.lon, l.lat),
                ST_MAKEPOINT(t.lon, t.lat)
            ) <= {cfg.transit_radius_m}
        WHERE l.is_current = TRUE AND l.lat IS NOT NULL
        GROUP BY l.listing_id
    """

    log = logger.bind(dimension="transit")
    log.info("query_start", radius_m=cfg.transit_radius_m)
    start = time.perf_counter()
    cursor.execute(sql)
    rows = cursor.fetchall()
    ms = int((time.perf_counter() - start) * 1000)
    log.info("query_complete", listings=len(rows), duration_ms=ms)

    result = {}
    for row in rows:
        stops = row[2] if isinstance(row[2], list) else (
            json.loads(row[2]) if row[2] else [])
        result[row[0]] = {
            "stop_count": row[1],
            "stop_names": [str(s) for s in stops],
        }
    return result


# ── Lifestyle Signal Overlay ─────────────────────────────────

def query_lifestyle_signals_by_neighborhood(
    cursor,
) -> dict[str, dict[str, dict]]:
    """Match lifestyle signals to neighborhoods.

    Flattens classification_metadata:neighborhoods_mentioned,
    groups by neighborhood + preference_tag + sentiment.

    Returns {neighborhood: {preference_tag: {
        "positive": N, "negative": N, "mixed": N, "neutral": N,
        "total": N, "sample_titles": [...]
    }}}.
    """
    sql = """
        SELECT
            n.value::STRING AS neighborhood,
            ls.preference_tag,
            ls.sentiment,
            ls.title
        FROM RAW.LIFESTYLE_SIGNALS ls,
             LATERAL FLATTEN(
                 input => ls.classification_metadata:neighborhoods_mentioned
             ) n
        WHERE ls.snippet_text IS NOT NULL
          AND ls.classification_metadata:neighborhoods_mentioned IS NOT NULL
          AND ARRAY_SIZE(ls.classification_metadata:neighborhoods_mentioned) > 0
    """

    log = logger.bind(dimension="lifestyle_overlay")
    log.info("query_start")
    start = time.perf_counter()

    try:
        cursor.execute(sql)
        rows = cursor.fetchall()
    except Exception as e:
        log.warning("lifestyle_query_failed", error=str(e)[:120])
        return {}

    ms = int((time.perf_counter() - start) * 1000)
    log.info("query_complete", rows=len(rows), duration_ms=ms)

    result: dict[str, dict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {
            "positive": 0, "negative": 0, "mixed": 0,
            "neutral": 0, "total": 0, "sample_titles": [],
        })
    )

    seen = set()
    for neighborhood, tag, sentiment, title in rows:
        hood = str(neighborhood)
        tag = str(tag)
        sent = str(sentiment or "neutral")
        key = (hood, tag, title)

        if key in seen:
            continue
        seen.add(key)

        entry = result[hood][tag]
        if sent in entry:
            entry[sent] += 1
        else:
            entry["neutral"] += 1
        entry["total"] += 1

        if len(entry["sample_titles"]) < 3:
            entry["sample_titles"].append(str(title or "")[:100])

    return {k: dict(v) for k, v in result.items()}


# ── Historical Series ────────────────────────────────────────

def query_monthly_crime_series(
    cursor, cfg: ScoringConfig,
) -> dict[str, dict[str, dict]]:
    """Monthly crime counts per listing, back to earliest data."""
    dlat, dlon = cfg.bbox_deltas("safety")

    sql = f"""
        SELECT
            l.listing_id,
            TO_CHAR(DATE_TRUNC('month', c.occurred_on_date), 'YYYY-MM') AS month,
            COUNT(*) AS total,
            COUNT(CASE WHEN c.severity = 'violent' THEN 1 END) AS violent
        FROM RAW.LISTINGS l
        JOIN RAW.CRIME_INCIDENTS c
            ON c.lat BETWEEN l.lat - {dlat} AND l.lat + {dlat}
            AND c.lon BETWEEN l.lon - {dlon} AND l.lon + {dlon}
            AND ST_DISTANCE(
                ST_MAKEPOINT(l.lon, l.lat),
                ST_MAKEPOINT(c.lon, c.lat)
            ) <= {cfg.safety_radius_m}
        WHERE l.is_current = TRUE
            AND l.lat IS NOT NULL
            AND c.occurred_on_date IS NOT NULL
        GROUP BY l.listing_id, DATE_TRUNC('month', c.occurred_on_date)
    """

    log = logger.bind(dimension="safety", sub="monthly_series")
    log.info("query_start")
    start = time.perf_counter()
    cursor.execute(sql)
    rows = cursor.fetchall()
    ms = int((time.perf_counter() - start) * 1000)
    log.info("query_complete", rows=len(rows), duration_ms=ms)

    series: dict[str, dict[str, dict]] = defaultdict(dict)
    for lid, month, total, violent in rows:
        series[lid][month] = {"total": total, "violent": violent}
    return dict(series)


def query_hourly_distribution(
    cursor, cfg: ScoringConfig,
) -> dict[str, dict[int, int]]:
    """24-bucket crime distribution per listing (last 6 months)."""
    dlat, dlon = cfg.bbox_deltas("safety")

    sql = f"""
        SELECT l.listing_id, c.hour, COUNT(*) AS cnt
        FROM RAW.LISTINGS l
        JOIN RAW.CRIME_INCIDENTS c
            ON c.lat BETWEEN l.lat - {dlat} AND l.lat + {dlat}
            AND c.lon BETWEEN l.lon - {dlon} AND l.lon + {dlon}
            AND ST_DISTANCE(
                ST_MAKEPOINT(l.lon, l.lat),
                ST_MAKEPOINT(c.lon, c.lat)
            ) <= {cfg.safety_radius_m}
            AND c.occurred_on_date >= DATEADD(month, -6, CURRENT_DATE())
        WHERE l.is_current = TRUE AND l.lat IS NOT NULL
            AND c.hour IS NOT NULL
        GROUP BY l.listing_id, c.hour
    """

    log = logger.bind(dimension="safety", sub="hourly")
    log.info("query_start")
    start = time.perf_counter()
    cursor.execute(sql)
    rows = cursor.fetchall()
    ms = int((time.perf_counter() - start) * 1000)
    log.info("query_complete", rows=len(rows), duration_ms=ms)

    dist: dict[str, dict[int, int]] = defaultdict(dict)
    for lid, hour, cnt in rows:
        dist[lid][int(hour)] = cnt
    return dict(dist)


def query_dow_distribution(
    cursor, cfg: ScoringConfig,
) -> dict[str, dict[str, int]]:
    """7-bucket day-of-week crime distribution per listing (last 6 months)."""
    dlat, dlon = cfg.bbox_deltas("safety")

    sql = f"""
        SELECT l.listing_id, c.day_of_week, COUNT(*) AS cnt
        FROM RAW.LISTINGS l
        JOIN RAW.CRIME_INCIDENTS c
            ON c.lat BETWEEN l.lat - {dlat} AND l.lat + {dlat}
            AND c.lon BETWEEN l.lon - {dlon} AND l.lon + {dlon}
            AND ST_DISTANCE(
                ST_MAKEPOINT(l.lon, l.lat),
                ST_MAKEPOINT(c.lon, c.lat)
            ) <= {cfg.safety_radius_m}
            AND c.occurred_on_date >= DATEADD(month, -6, CURRENT_DATE())
        WHERE l.is_current = TRUE AND l.lat IS NOT NULL
            AND c.day_of_week IS NOT NULL
        GROUP BY l.listing_id, c.day_of_week
    """

    log = logger.bind(dimension="safety", sub="dow")
    log.info("query_start")
    start = time.perf_counter()
    cursor.execute(sql)
    rows = cursor.fetchall()
    ms = int((time.perf_counter() - start) * 1000)
    log.info("query_complete", rows=len(rows), duration_ms=ms)

    dist: dict[str, dict[str, int]] = defaultdict(dict)
    for lid, dow, cnt in rows:
        dist[lid][str(dow)] = cnt
    return dict(dist)