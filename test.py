"""Vicinity schema audit — emit JSON showing what's actually populated.

Run:
    python -m scripts.audit_schema > audit.json

Paste audit.json contents back. No external deps beyond what the app uses.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager

from app.core.database import snowflake_cursor


def _rows(cursor, sql):
    cursor.execute(sql)
    cols = [d[0].lower() for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _one(cursor, sql):
    r = _rows(cursor, sql)
    return r[0] if r else {}


@contextmanager
def _section(name, store):
    try:
        yield (lambda v: store.__setitem__(name, v))
    except Exception as e:
        store[name] = {"_error": str(e)[:400]}


def audit():
    out = {}

    with snowflake_cursor() as c:

        with _section("crime_freshness", out) as set_:
            set_(_one(c, """
                SELECT
                  MIN(occurred_on_date)::STRING  AS earliest,
                  MAX(occurred_on_date)::STRING  AS latest,
                  DATEDIFF('day', MAX(occurred_on_date), CURRENT_DATE()) AS days_stale,
                  COUNT(*)                       AS total,
                  SUM(IFF(occurred_on_date >= DATEADD(day, -30,  CURRENT_DATE()), 1, 0)) AS last_30d,
                  SUM(IFF(occurred_on_date >= DATEADD(day, -90,  CURRENT_DATE()), 1, 0)) AS last_90d,
                  SUM(IFF(occurred_on_date >= DATEADD(day, -365, CURRENT_DATE()), 1, 0)) AS last_365d,
                  SUM(IFF(severity = 'violent',  1, 0)) AS violent_total,
                  SUM(IFF(severity = 'property', 1, 0)) AS property_total,
                  SUM(IFF(shooting,              1, 0)) AS shootings_total
                FROM RAW.CRIME_INCIDENTS
            """))

        with _section("crime_severity_mix", out) as set_:
            set_(_rows(c, """
                SELECT severity, COUNT(*) AS n
                FROM RAW.CRIME_INCIDENTS
                GROUP BY severity ORDER BY n DESC
            """))

        with _section("crime_bbox", out) as set_:
            set_(_one(c, """
                SELECT
                  MIN(lat) AS min_lat, MAX(lat) AS max_lat,
                  MIN(lon) AS min_lon, MAX(lon) AS max_lon,
                  SUM(IFF(lat IS NULL OR lon IS NULL, 1, 0)) AS null_geo,
                  COUNT(*) AS total
                FROM RAW.CRIME_INCIDENTS
            """))

        with _section("listing_summary_fill", out) as set_:
            set_(_one(c, """
                SELECT
                  COUNT(*) AS total,
                  SUM(IFF(safety_score       IS NOT NULL, 1, 0)) AS has_safety,
                  SUM(IFF(livability_score   IS NOT NULL, 1, 0)) AS has_livability,
                  SUM(IFF(safety_metadata    IS NOT NULL, 1, 0)) AS has_safety_meta,
                  SUM(IFF(livability_metadata IS NOT NULL, 1, 0)) AS has_livability_meta,
                  SUM(IFF(nearest_stops      IS NOT NULL, 1, 0)) AS has_stops,
                  SUM(IFF(lifestyle_scores   IS NOT NULL, 1, 0)) AS has_lifestyle,
                  SUM(IFF(nearby_amenities   IS NOT NULL, 1, 0)) AS has_nearby_amenities,
                  SUM(IFF(price_history      IS NOT NULL, 1, 0)) AS has_price_history,
                  SUM(IFF(safety_trend       IS NOT NULL, 1, 0)) AS has_safety_trend,
                  SUM(IFF(description_text   IS NOT NULL AND LENGTH(description_text) > 50, 1, 0)) AS has_desc,
                  SUM(IFF(primary_photo_url  IS NOT NULL, 1, 0)) AS has_photo,
                  AVG(LENGTH(description_text))::INT AS avg_desc_len,
                  MAX(last_scored_at)::STRING AS latest_score
                FROM SCORECARDS.LISTING_SUMMARY
            """))

        with _section("safety_metadata_fields", out) as set_:
            set_(_rows(c, """
                SELECT
                  listing_id,
                  safety_metadata:percentile::INT         AS percentile,
                  safety_metadata:confidence::FLOAT       AS confidence,
                  LEFT(safety_metadata:interpretation::STRING, 180) AS interpretation,
                  safety_metadata:crime_count::INT        AS crimes,
                  safety_metadata:violent_count::INT      AS violent,
                  safety_metadata:shooting_count::INT     AS shootings,
                  safety_metadata:citizen_48h::INT        AS citizen_48h,
                  safety_metadata:citizen_nighttime_48h::INT AS citizen_night_48h,
                  safety_metadata:citizen_critical_48h::INT  AS citizen_critical_48h,
                  safety_metadata:yoy_change_pct::FLOAT   AS yoy_pct,
                  safety_metadata:hourly_distribution IS NOT NULL   AS has_hourly,
                  safety_metadata:dow_distribution    IS NOT NULL   AS has_dow,
                  safety_metadata:monthly_series      IS NOT NULL   AS has_monthly,
                  safety_metadata:community_perception IS NOT NULL  AS has_community
                FROM SCORECARDS.LISTING_SUMMARY
                WHERE safety_metadata IS NOT NULL
                LIMIT 3
            """))

        with _section("livability_metadata_fields", out) as set_:
            set_(_rows(c, """
                SELECT
                  listing_id,
                  livability_metadata:percentile::INT               AS percentile,
                  livability_metadata:confidence::FLOAT             AS confidence,
                  livability_metadata:complaint_count_total::INT    AS complaints,
                  livability_metadata:effective_complaint_score::FLOAT AS eff_score,
                  livability_metadata:noise_count::INT              AS noise,
                  livability_metadata:pest_count::INT               AS pest,
                  livability_metadata:heat_count::INT               AS heat,
                  livability_metadata:housing_count::INT            AS housing,
                  livability_metadata:infra_count::INT              AS infra,
                  livability_metadata:essentials_found::INT         AS ess_found,
                  livability_metadata:essentials_total::INT         AS ess_total,
                  livability_metadata:essentials_present            AS ess_present,
                  livability_metadata:essentials_missing            AS ess_missing,
                  livability_metadata:total_amenities::INT          AS total_amenities,
                  livability_metadata:noise_perception IS NOT NULL  AS has_noise_perception
                FROM SCORECARDS.LISTING_SUMMARY
                WHERE livability_metadata IS NOT NULL
                LIMIT 3
            """))

        with _section("lifestyle_overlay_in_scorecard", out) as set_:
            # This lives in LOCATION_SCORECARD only, not LISTING_SUMMARY
            set_(_rows(c, """
                SELECT
                  listing_id,
                  score_date::STRING AS score_date,
                  scoring_metadata:lifestyle_overlay IS NOT NULL AS has_overlay,
                  OBJECT_KEYS(scoring_metadata:lifestyle_overlay) AS overlay_keys
                FROM SCORECARDS.LOCATION_SCORECARD
                WHERE score_date >= DATEADD(day, -7, CURRENT_DATE())
                  AND scoring_metadata:lifestyle_overlay IS NOT NULL
                ORDER BY score_date DESC
                LIMIT 5
            """))

        with _section("amenities_fill", out) as set_:
            set_(_one(c, """
                SELECT
                  COUNT(*)                              AS total,
                  COUNT(DISTINCT subcategory)           AS n_subcats,
                  COUNT(DISTINCT category)              AS n_cats,
                  SUM(IFF(name          IS NOT NULL, 1, 0)) AS has_name,
                  SUM(IFF(address       IS NOT NULL, 1, 0)) AS has_address,
                  SUM(IFF(opening_hours IS NOT NULL, 1, 0)) AS has_hours,
                  SUM(IFF(website       IS NOT NULL, 1, 0)) AS has_website,
                  SUM(IFF(phone         IS NOT NULL, 1, 0)) AS has_phone,
                  SUM(IFF(brand         IS NOT NULL, 1, 0)) AS has_brand,
                  SUM(IFF(tags          IS NOT NULL, 1, 0)) AS has_tags
                FROM RAW.AMENITIES
            """))

        with _section("amenities_top_subcats", out) as set_:
            set_(_rows(c, """
                SELECT subcategory, category, COUNT(*) AS n
                FROM RAW.AMENITIES
                GROUP BY subcategory, category
                ORDER BY n DESC LIMIT 30
            """))

        with _section("complaints_freshness", out) as set_:
            set_(_one(c, """
                SELECT
                  MAX(open_dt)::STRING AS latest,
                  DATEDIFF('day', MAX(open_dt), CURRENT_DATE()) AS days_stale,
                  COUNT(*) AS total,
                  SUM(IFF(open_dt >= DATEADD(day, -30,  CURRENT_DATE()), 1, 0)) AS last_30d,
                  SUM(IFF(open_dt >= DATEADD(day, -90,  CURRENT_DATE()), 1, 0)) AS last_90d
                FROM RAW.COMPLAINTS_311
            """))

        with _section("complaints_by_category", out) as set_:
            set_(_rows(c, """
                SELECT category, COUNT(*) AS n
                FROM RAW.COMPLAINTS_311
                GROUP BY category ORDER BY n DESC
            """))

        with _section("citizen_signal", out) as set_:
            set_(_one(c, """
                SELECT
                  MAX(incident_ts)::STRING AS latest,
                  DATEDIFF('day', MAX(incident_ts), CURRENT_DATE()) AS days_stale,
                  COUNT(*) AS total,
                  SUM(IFF(severity = 'critical', 1, 0)) AS critical,
                  SUM(IFF(is_nighttime,          1, 0)) AS nighttime
                FROM RAW.CITIZEN_INCIDENTS
            """))

        with _section("lifestyle_signals_fill", out) as set_:
            set_(_rows(c, """
                SELECT
                  signal_source,
                  COUNT(*) AS n,
                  SUM(IFF(raw_thread_text IS NOT NULL AND LENGTH(raw_thread_text) > 200, 1, 0)) AS has_full_thread,
                  SUM(IFF(snippet_text    IS NOT NULL, 1, 0)) AS has_snippet,
                  SUM(IFF(sentiment       IS NOT NULL, 1, 0)) AS has_sentiment,
                  COUNT(DISTINCT preference_tag) AS n_tags,
                  MAX(fetched_at)::STRING AS latest
                FROM RAW.LIFESTYLE_SIGNALS
                GROUP BY signal_source
                ORDER BY n DESC
            """))

        with _section("lifestyle_sample", out) as set_:
            set_(_rows(c, """
                SELECT
                  signal_source,
                  preference_tag,
                  LEFT(title, 140) AS title,
                  sentiment,
                  relevance_score,
                  LEFT(snippet_text, 240) AS snippet
                FROM RAW.LIFESTYLE_SIGNALS
                WHERE title IS NOT NULL
                ORDER BY fetched_at DESC
                LIMIT 5
            """))

        with _section("transit_routes", out) as set_:
            set_(_rows(c, """
                SELECT rt.value::INT AS route_type, COUNT(*) AS stops
                FROM RAW.TRANSIT_STOPS, LATERAL FLATTEN(route_types) rt
                GROUP BY route_type ORDER BY stops DESC
            """))

        with _section("listings_overview", out) as set_:
            set_(_one(c, """
                SELECT
                  COUNT(*) AS total,
                  SUM(IFF(is_current, 1, 0))                      AS active,
                  SUM(IFF(primary_photo_url IS NOT NULL, 1, 0))   AS with_photo,
                  SUM(IFF(description_text  IS NOT NULL, 1, 0))   AS with_desc,
                  SUM(IFF(days_on_mls       IS NOT NULL, 1, 0))   AS with_days_mls,
                  SUM(IFF(mls_status        IS NOT NULL, 1, 0))   AS with_mls_status,
                  SUM(IFF(agent_name        IS NOT NULL, 1, 0))   AS with_agent,
                  COUNT(DISTINCT source)                          AS sources,
                  COUNT(DISTINCT neighborhood)                    AS neighborhoods,
                  MIN(price) AS price_min,
                  MAX(price) AS price_max,
                  AVG(price)::INT AS price_avg
                FROM RAW.LISTINGS
                WHERE is_current = TRUE
            """))

        with _section("neighborhood_distribution", out) as set_:
            set_(_rows(c, """
                SELECT neighborhood, COUNT(*) AS n
                FROM RAW.LISTINGS
                WHERE is_current = TRUE AND neighborhood IS NOT NULL
                GROUP BY neighborhood
                ORDER BY n DESC LIMIT 20
            """))

    json.dump(out, sys.stdout, default=str, indent=2)


if __name__ == "__main__":
    audit()