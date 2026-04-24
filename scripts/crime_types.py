"""Diagnostic: top offense types near a listing.

Answers: "Why are crimes being committed near this address?"
by aggregating offense_description over the full crime table.

Usage (from project root):

    python -m scripts.crime_types --listing-id 2550941bb066179c
    python -m scripts.crime_types --lat 42.34 --lon -71.10
    python -m scripts.crime_types --lat 42.34 --lon -71.10 --radius 500 --days 90
    python -m scripts.crime_types --listing-id 2550941bb066179c --json

Flags:
    --listing-id   Look up lat/lon from RAW.LISTINGS
    --lat / --lon  Use coordinates directly
    --radius       Meters (default 500 — matches safety score scope)
    --days         Lookback window (default = all history)
    --top          How many offense types to show (default 15)
    --json         Machine-readable output

What you'll see:
    - Total incidents in scope + severity mix
    - Top N offense descriptions with counts and percentages
    - Time-of-day peaks per offense (for top 5)
    - Day-of-week peaks per offense (for top 5)
    - Date range of incidents
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from app.config import get_settings


# ─── Connection helper ──────────────────────────────────────────────

def _connect():
    import snowflake.connector
    s = get_settings()
    return snowflake.connector.connect(
        account=s.snowflake_account,
        user=s.snowflake_user,
        password=s.snowflake_password.get_secret_value(),
        database=s.snowflake_database,
        warehouse=s.snowflake_warehouse,
        role=s.snowflake_role,
    )


# ─── Color output ───────────────────────────────────────────────────

C = {
    "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
    "gray": "\033[90m",
}


def c(color: str, text) -> str:
    return f"{C.get(color, '')}{text}{C['reset']}"


def bar(pct: float, width: int = 30) -> str:
    filled = int(round(pct * width / 100))
    return "█" * filled + "░" * (width - filled)


# ─── Listing resolver ───────────────────────────────────────────────

def _resolve_listing(cursor, listing_id: str) -> tuple:
    cursor.execute(
        "SELECT lat, lon, street, neighborhood FROM RAW.LISTINGS WHERE listing_id = %s",
        (listing_id,),
    )
    row = cursor.fetchone()
    if not row:
        print(c("red", f"\nListing {listing_id} not found in RAW.LISTINGS"))
        sys.exit(1)
    return row  # (lat, lon, street, neighborhood)


# ─── Main query ─────────────────────────────────────────────────────

def _query(cursor, lat: float, lon: float, radius_m: int,
           days: Optional[int], top: int) -> dict:
    """Run the diagnostic aggregations. Returns a dict structured for printing or JSON."""
    lat_span = (radius_m / 111000.0) * 1.2
    lon_span = (radius_m / (111000.0 * 0.74)) * 1.2

    day_clause = ""
    params_base = [
        lat - lat_span, lat + lat_span,
        lon - lon_span, lon + lon_span,
        lat, lon, radius_m,
    ]
    if days:
        day_clause = f"AND occurred_on_date >= DATEADD(day, -{int(days)}, CURRENT_DATE())"

    # Summary stats + date range + severity mix
    sql_summary = f"""
        WITH scoped AS (
            SELECT occurred_on_date, severity
            FROM RAW.CRIME_INCIDENTS
            WHERE lat IS NOT NULL
              AND lat BETWEEN %s AND %s
              AND lon BETWEEN %s AND %s
              AND HAVERSINE(%s, %s, lat, lon) * 1000 <= %s
              {day_clause}
        )
        SELECT
            COUNT(*)                                     AS total,
            SUM(IFF(severity = 'violent',   1, 0))       AS violent,
            SUM(IFF(severity = 'property',  1, 0))       AS property,
            SUM(IFF(severity = 'minor',     1, 0))       AS minor,
            SUM(IFF(severity = 'non_crime', 1, 0))       AS non_crime,
            MIN(occurred_on_date)::STRING                AS earliest,
            MAX(occurred_on_date)::STRING                AS latest
        FROM scoped
    """
    cursor.execute(sql_summary, params_base)
    srow = cursor.fetchone()
    summary = {
        "total": srow[0] or 0,
        "violent": srow[1] or 0,
        "property": srow[2] or 0,
        "minor": srow[3] or 0,
        "non_crime": srow[4] or 0,
        "earliest": srow[5],
        "latest": srow[6],
    }

    if summary["total"] == 0:
        return {"summary": summary, "top_offenses": [], "peaks": []}

    # Top N offenses with severity breakdown
    sql_top = f"""
        WITH scoped AS (
            SELECT offense_description, severity
            FROM RAW.CRIME_INCIDENTS
            WHERE lat IS NOT NULL
              AND lat BETWEEN %s AND %s
              AND lon BETWEEN %s AND %s
              AND HAVERSINE(%s, %s, lat, lon) * 1000 <= %s
              {day_clause}
              AND offense_description IS NOT NULL
        )
        SELECT
            offense_description,
            COUNT(*)                                 AS cnt,
            MODE(severity)                           AS dominant_severity,
            SUM(IFF(severity = 'violent',   1, 0))   AS violent,
            SUM(IFF(severity = 'property',  1, 0))   AS property,
            SUM(IFF(severity = 'minor',     1, 0))   AS minor
        FROM scoped
        GROUP BY offense_description
        ORDER BY cnt DESC
        LIMIT {int(top)}
    """
    cursor.execute(sql_top, params_base)
    top_offenses = [
        {
            "offense": r[0],
            "count": r[1],
            "pct": round(100 * r[1] / summary["total"], 1),
            "severity": r[2],
            "violent": r[3],
            "property": r[4],
            "minor": r[5],
        }
        for r in cursor.fetchall()
    ]

    # Time-of-day and day-of-week peak for the top 5 offenses
    peaks = []
    for entry in top_offenses[:5]:
        sql_peak = f"""
            SELECT
                HOUR(occurred_on_date)    AS hr,
                DAYNAME(occurred_on_date) AS dow,
                COUNT(*)                  AS cnt
            FROM RAW.CRIME_INCIDENTS
            WHERE lat IS NOT NULL
              AND lat BETWEEN %s AND %s
              AND lon BETWEEN %s AND %s
              AND HAVERSINE(%s, %s, lat, lon) * 1000 <= %s
              {day_clause}
              AND offense_description = %s
            GROUP BY hr, dow
            ORDER BY cnt DESC
            LIMIT 3
        """
        cursor.execute(sql_peak, params_base + [entry["offense"]])
        rows = cursor.fetchall()
        peaks.append({
            "offense": entry["offense"],
            "top_times": [
                {"hour": r[0], "dow": r[1], "count": r[2]} for r in rows
            ],
        })

    return {"summary": summary, "top_offenses": top_offenses, "peaks": peaks}


# ─── Pretty printer ─────────────────────────────────────────────────

def _print_report(result: dict, lat: float, lon: float, radius_m: int,
                  days: Optional[int], listing_meta: Optional[tuple]):
    s = result["summary"]
    top = result["top_offenses"]
    peaks = result["peaks"]

    print()
    print(c("bold", "  Crime Type Diagnostic"))
    if listing_meta:
        print(c("dim", f"  Listing:  {listing_meta[2]} ({listing_meta[3] or 'n/a'})"))
    print(c("dim", f"  Center:   {lat:.5f}, {lon:.5f}"))
    print(c("dim", f"  Radius:   {radius_m}m"))
    print(c("dim", f"  Window:   {'last ' + str(days) + ' days' if days else 'all history'}"))
    if s["earliest"] and s["latest"]:
        print(c("dim", f"  Range:    {s['earliest']} → {s['latest']}"))
    print()

    if s["total"] == 0:
        print(c("yellow", "  No incidents in scope."))
        return

    print(c("bold", f"  Total incidents:  {s['total']:,}"))
    sev_parts = []
    if s["violent"]:   sev_parts.append(c("red", f"{s['violent']} violent"))
    if s["property"]:  sev_parts.append(c("yellow", f"{s['property']} property"))
    if s["minor"]:     sev_parts.append(c("blue", f"{s['minor']} minor"))
    if s["non_crime"]: sev_parts.append(c("gray", f"{s['non_crime']} non-crime"))
    print("  Severity mix:     " + " · ".join(sev_parts))
    print()

    # Top offenses table
    print(c("bold", f"  Top offense types ({len(top)} shown):"))
    print()

    max_offense_len = max((len(o["offense"]) for o in top), default=20)
    max_offense_len = min(max_offense_len, 44)

    for o in top:
        name = o["offense"]
        if len(name) > max_offense_len:
            name = name[:max_offense_len - 1] + "…"
        name_padded = name.ljust(max_offense_len)

        sev = o["severity"] or "—"
        sev_color = {"violent": "red", "property": "yellow", "minor": "blue",
                     "non_crime": "gray"}.get(sev, "reset")

        count_str = f"{o['count']:>4}"
        pct_str = f"{o['pct']:>4.1f}%"
        bar_str = bar(o["pct"], width=20)

        print(f"  {c('cyan', name_padded)}  "
              f"{c(sev_color, sev.ljust(9))}  "
              f"{c('bold', count_str)}  "
              f"{c('dim', pct_str)}  "
              f"{c('dim', bar_str)}")

    # Time peaks for top 5
    if peaks:
        print()
        print(c("bold", "  Peak times for top offenses:"))
        print()
        for p in peaks:
            name = p["offense"]
            if len(name) > 44:
                name = name[:43] + "…"
            print(f"  {c('cyan', name)}")
            if not p["top_times"]:
                print(c("dim", "    (no timing data)"))
                continue
            for t in p["top_times"]:
                hr_label = f"{t['hour']:02d}:00"
                print(c("dim", f"    {hr_label} on {t['dow']:<9}  {t['count']:>3} incidents"))
            print()


# ─── CLI entry ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Diagnose crime types near a location")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--listing-id", type=str, help="Look up lat/lon from RAW.LISTINGS")
    src.add_argument("--lat", type=float, help="Latitude (use with --lon)")
    parser.add_argument("--lon", type=float, help="Longitude (required with --lat)")
    parser.add_argument("--radius", type=int, default=500,
                        help="Radius in meters (default 500)")
    parser.add_argument("--days", type=int, default=None,
                        help="Lookback window. Omit for all history.")
    parser.add_argument("--top", type=int, default=15,
                        help="Number of offense types to show (default 15)")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args()

    if args.lat is not None and args.lon is None:
        parser.error("--lon is required with --lat")

    conn = _connect()
    try:
        cursor = conn.cursor()
        listing_meta = None
        if args.listing_id:
            listing_meta = _resolve_listing(cursor, args.listing_id)
            lat, lon = listing_meta[0], listing_meta[1]
            if lat is None or lon is None:
                print(c("red", f"Listing {args.listing_id} has no geocoordinates"))
                sys.exit(1)
        else:
            lat, lon = args.lat, args.lon

        result = _query(cursor, lat, lon, args.radius, args.days, args.top)
        cursor.close()

        if args.json:
            out = {
                "center": {"lat": lat, "lon": lon},
                "radius_m": args.radius,
                "days": args.days,
                "listing": {
                    "listing_id": args.listing_id,
                    "street": listing_meta[2] if listing_meta else None,
                    "neighborhood": listing_meta[3] if listing_meta else None,
                } if listing_meta else None,
                **result,
            }
            print(json.dumps(out, indent=2, default=str))
        else:
            _print_report(result, lat, lon, args.radius, args.days, listing_meta)

    finally:
        conn.close()


if __name__ == "__main__":
    main()