"""Listings coverage diagnostic.

Reports listing density per Boston ZIP/neighborhood, so we can see
exactly where the current HomeHarvest config is missing coverage.
Does NOT touch Snowflake for writes — reads RAW.LISTINGS and
compares against config/spatial.yml's ZIP→neighborhood map.

Usage (from project root):

    python -m scripts.listings_coverage

    # Also attempt a HomeHarvest fetch per ZIP and report what the
    # API *could* return vs what's in your RAW table:
    python -m scripts.listings_coverage --probe

    # Probe only specific ZIPs:
    python -m scripts.listings_coverage --probe --zips 02124 02126 02136

    # Machine-readable:
    python -m scripts.listings_coverage --json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import yaml

from app.config import get_settings
from app.core.config_loader import CONFIG_DIR


# ── Color output ────────────────────────────────────────────────────

C = {
    "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "cyan": "\033[36m", "gray": "\033[90m",
}


def c(color: str, text) -> str:
    return f"{C.get(color, '')}{text}{C['reset']}"


def bar(n: int, maximum: int, width: int = 24) -> str:
    if maximum <= 0:
        return ""
    filled = int(round(n * width / maximum))
    return "█" * filled + "░" * (width - filled)


# ── Snowflake helpers ───────────────────────────────────────────────

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


def _load_zip_map() -> dict:
    """Load ZIP → neighborhood from config/spatial.yml."""
    with open(CONFIG_DIR / "spatial.yml", encoding="utf-8") as f:
        spatial = yaml.safe_load(f) or {}
    return spatial.get("zip_to_neighborhood", {})


def _query_coverage(cursor) -> dict:
    """Count active listings per ZIP and per neighborhood."""
    cursor.execute("""
        SELECT
            zip_code,
            neighborhood,
            city,
            COUNT(*) AS n,
            SUM(IFF(primary_photo_url IS NOT NULL, 1, 0)) AS with_photo,
            AVG(price)::INT AS avg_price
        FROM RAW.LISTINGS
        WHERE is_current = TRUE
        GROUP BY zip_code, neighborhood, city
        ORDER BY n DESC
    """)
    rows = cursor.fetchall()
    cols = [d[0].lower() for d in cursor.description]
    return [dict(zip(cols, r)) for r in rows]


# ── HomeHarvest probe ───────────────────────────────────────────────

def _probe_zip(zip_code: str, past_days: int = 30) -> dict:
    """Fetch a ZIP from HomeHarvest, return counts without writing anywhere."""
    try:
        from homeharvest import scrape_property
    except ImportError:
        return {"error": "homeharvest not installed"}

    try:
        t0 = time.perf_counter()
        df = scrape_property(
            location=zip_code,
            listing_type="for_rent",
            past_days=past_days,
        )
        ms = int((time.perf_counter() - t0) * 1000)

        if df is None or df.empty:
            return {"available": 0, "ms": ms}

        in_zip = df[df["zip_code"].astype(str) == zip_code]
        neighborhoods = (
            df["neighborhoods"].dropna().unique().tolist()
            if "neighborhoods" in df.columns else []
        )
        cities = df["city"].dropna().unique().tolist() if "city" in df.columns else []

        return {
            "available": len(df),
            "in_exact_zip": len(in_zip),
            "ms": ms,
            "distinct_zips": df["zip_code"].nunique() if "zip_code" in df.columns else 0,
            "sample_cities": cities[:3],
            "sample_neighborhoods": neighborhoods[:3],
        }
    except Exception as e:
        return {"error": str(e)[:200]}


# ── Report printer ──────────────────────────────────────────────────

def _print_coverage(zip_map: dict, coverage: list[dict]):
    # Aggregate by ZIP and by neighborhood
    by_zip = {row["zip_code"]: row for row in coverage if row["zip_code"]}
    by_neighborhood = defaultdict(lambda: {"n": 0, "zips": set()})
    for row in coverage:
        nb = row["neighborhood"] or "(unassigned)"
        by_neighborhood[nb]["n"] += row["n"]
        if row["zip_code"]:
            by_neighborhood[nb]["zips"].add(row["zip_code"])

    max_neighborhood = max((v["n"] for v in by_neighborhood.values()), default=1)
    max_zip = max((row["n"] for row in coverage), default=1)

    # Totals
    total_active = sum(r["n"] for r in coverage)
    boston_zips = set(zip_map.keys())
    covered_zips = set(by_zip.keys()) & boston_zips
    missing_zips = boston_zips - set(by_zip.keys())

    print()
    print(c("bold", "  Vicinity Listings Coverage Diagnostic"))
    print(c("dim", f"  Total active listings: {total_active:,}"))
    print(c("dim", f"  Boston ZIPs in spatial.yml: {len(boston_zips)}"))
    print(c("dim", f"  Boston ZIPs with coverage:  {len(covered_zips)}"))
    print(c("dim", f"  Boston ZIPs missing:        {len(missing_zips)}"))
    print()

    # ── By neighborhood ─────────────────────────────────────────────
    print(c("bold", "  Listings by Boston neighborhood:"))
    print()

    sorted_nbs = sorted(by_neighborhood.items(), key=lambda x: -x[1]["n"])
    max_nb_len = max((len(nb) for nb, _ in sorted_nbs), default=20)
    max_nb_len = min(max_nb_len, 28)

    for nb, data in sorted_nbs:
        count = data["n"]
        if count == 0:
            continue
        nb_disp = nb if len(nb) <= max_nb_len else nb[:max_nb_len - 1] + "…"
        color = "green" if count >= 100 else "yellow" if count >= 30 else "red"
        zip_count = len(data["zips"])
        print(f"  {nb_disp.ljust(max_nb_len)}  "
              f"{c(color, f'{count:>4}')}  "
              f"{c('dim', bar(count, max_neighborhood, 20))}  "
              f"{c('dim', f'({zip_count} zips)')}")

    # ── By ZIP ──────────────────────────────────────────────────────
    print()
    print(c("bold", "  Coverage by Boston ZIP (from spatial.yml):"))
    print()

    for zip_code in sorted(boston_zips):
        neighborhood = zip_map[zip_code]
        row = by_zip.get(zip_code)
        n = row["n"] if row else 0

        status = (
            c("green", "✓") if n >= 50
            else c("yellow", "~") if n >= 10
            else c("red", "✗")
        )
        count_str = c("dim", "0") if n == 0 else f"{n:>4}"

        print(f"  {status}  {zip_code}  {neighborhood.ljust(24)}  "
              f"{count_str}  {c('dim', bar(n, max_zip, 16))}")

    # ── Missing ZIPs callout ────────────────────────────────────────
    if missing_zips:
        print()
        print(c("red", f"  Under-covered ZIPs ({len(missing_zips)}):"))
        for z in sorted(missing_zips):
            print(c("red", f"    {z}  {zip_map[z]}"))

    # ── Listings outside Boston ─────────────────────────────────────
    non_boston = [r for r in coverage
                  if r["zip_code"] and r["zip_code"] not in boston_zips]
    if non_boston:
        print()
        print(c("yellow", f"  Active listings OUTSIDE Boston ZIPs: {sum(r['n'] for r in non_boston):,}"))
        for r in sorted(non_boston, key=lambda x: -x["n"])[:10]:
            city = (r["city"] or "?")[:30]
            print(c("gray", f"    {r['zip_code']}  {city.ljust(30)}  {r['n']:>4}"))


def _print_probe(zip_map: dict, probes: dict):
    print()
    print(c("bold", "  HomeHarvest live probe:"))
    print(c("dim", "  (what the API returns vs what's in RAW.LISTINGS)"))
    print()

    for zip_code in sorted(probes.keys()):
        result = probes[zip_code]
        neighborhood = zip_map.get(zip_code, "?")
        if "error" in result:
            print(c("red", f"  {zip_code}  {neighborhood}  ERROR: {result['error']}"))
            continue

        avail = result.get("available", 0)
        in_zip = result.get("in_exact_zip", 0)
        ms = result.get("ms", 0)
        distinct = result.get("distinct_zips", 0)

        print(f"  {zip_code}  {neighborhood.ljust(24)}  "
              f"returned={c('bold', str(avail))}  "
              f"in_zip={c('cyan', str(in_zip))}  "
              f"distinct_zips={distinct}  "
              f"{c('dim', f'{ms}ms')}")
        if result.get("sample_cities"):
            print(c("gray", f"       cities: {', '.join(result['sample_cities'])}"))


# ── CLI ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Listings coverage diagnostic")
    parser.add_argument("--probe", action="store_true",
                        help="Probe HomeHarvest per-ZIP (slow, ~30s per zip)")
    parser.add_argument("--zips", nargs="+", default=None,
                        help="Only probe these specific ZIPs")
    parser.add_argument("--past-days", type=int, default=30,
                        help="past_days for HomeHarvest probe (default 30)")
    parser.add_argument("--delay", type=float, default=3.0,
                        help="Seconds between HomeHarvest probes")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    zip_map = _load_zip_map()
    if not zip_map:
        print(c("red", "No zip_to_neighborhood map in config/spatial.yml"))
        sys.exit(1)

    conn = _connect()
    try:
        cursor = conn.cursor()
        coverage = _query_coverage(cursor)
        cursor.close()
    finally:
        conn.close()

    # Probe
    probes = {}
    if args.probe:
        zips_to_probe = args.zips if args.zips else sorted(zip_map.keys())
        print(c("dim", f"  Probing {len(zips_to_probe)} ZIPs "
                       f"(~{args.delay * len(zips_to_probe):.0f}s total)..."))
        for zip_code in zips_to_probe:
            probes[zip_code] = _probe_zip(zip_code, args.past_days)
            if args.delay > 0:
                time.sleep(args.delay)

    if args.json:
        by_zip = {row["zip_code"]: row for row in coverage if row["zip_code"]}
        out = {
            "totals": {
                "active_listings": sum(r["n"] for r in coverage),
                "boston_zips_in_map": len(zip_map),
                "zips_with_coverage": len(set(by_zip.keys()) & set(zip_map.keys())),
            },
            "coverage": coverage,
            "zip_map": zip_map,
            "probes": probes,
        }
        print(json.dumps(out, indent=2, default=str))
    else:
        _print_coverage(zip_map, coverage)
        if probes:
            _print_probe(zip_map, probes)


if __name__ == "__main__":
    main()