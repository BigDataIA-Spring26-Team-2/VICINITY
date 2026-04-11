"""Joint listings runner — HomeHarvest primary, Craigslist fallback.

Runs HomeHarvest → checks record count → runs Craigslist if below threshold.
Designed for Airflow DAG invocation with a single command.

Usage:
    python -m scripts.run_listings --mode full --dry-run
    python -m scripts.run_listings --mode incremental
    python -m scripts.run_listings --mode incremental --fallback-threshold 50
    python -m scripts.run_listings --force-craigslist --limit 100
    python -m scripts.run_listings --skip-craigslist
    python -m scripts.run_listings --location "Boston, MA" --past-days 3
    python -m scripts.run_listings --force-craigslist --no-headless --delay 5
"""

import sys
import argparse

import structlog

from app.pipelines.ingest_listings import ListingsPipeline
from app.pipelines.ingest_listings_craigslist import CraigslistPipeline

logger = structlog.get_logger()


# ── Argument mapping ─────────────────────────────────────────

def _build_homeharvest_args(args: argparse.Namespace) -> argparse.Namespace:
    """Map runner args to HomeHarvest pipeline args."""
    return argparse.Namespace(
        mode=args.mode,
        dry_run=args.dry_run,
        start_date=args.start_date,
        end_date=args.end_date,
        limit=args.limit,
        min_price=args.min_price,
        max_price=args.max_price,
        skip_deactivation=args.skip_deactivation,
        location=args.location,
        past_days=args.past_days,
    )


def _build_craigslist_args(args: argparse.Namespace) -> argparse.Namespace:
    """Map runner args to Craigslist pipeline args."""
    return argparse.Namespace(
        mode=args.mode,
        dry_run=args.dry_run,
        start_date=args.start_date,
        end_date=args.end_date,
        limit=args.limit,
        min_price=args.min_price,
        max_price=args.max_price,
        delay=args.delay,
        no_headless=args.no_headless,
    )


# ── Runner ───────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Run listings pipelines with Craigslist fallback",
    )

    # Shared pipeline args
    parser.add_argument(
        "--mode", choices=["full", "incremental"], default="incremental",
        help="full: backfill. incremental: only new records.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Extract and validate only. No writes to Snowflake.",
    )
    parser.add_argument("--start-date", type=str, default=None)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max listings per pipeline.",
    )
    parser.add_argument(
        "--min-price", type=int, default=None,
        help="Filter: minimum listing price.",
    )
    parser.add_argument(
        "--max-price", type=int, default=None,
        help="Filter: maximum listing price.",
    )

    # HomeHarvest-specific args
    parser.add_argument(
        "--skip-deactivation", action="store_true",
        help="Skip marking unseen HomeHarvest listings as inactive.",
    )
    parser.add_argument(
        "--location", type=str, default=None,
        help="HomeHarvest: override locations. Single city only.",
    )
    parser.add_argument(
        "--past-days", type=int, default=None,
        help="HomeHarvest: fetch listings from last N days.",
    )

    # Craigslist-specific args
    parser.add_argument(
        "--delay", type=float, default=None,
        help="Craigslist: override delay between fetches (seconds).",
    )
    parser.add_argument(
        "--no-headless", action="store_true",
        help="Craigslist: run browser in visible mode (debugging).",
    )

    # Runner-specific args
    parser.add_argument(
        "--fallback-threshold", type=int, default=50,
        help="Run Craigslist if HomeHarvest loads fewer than N records.",
    )
    parser.add_argument(
        "--force-craigslist", action="store_true",
        help="Skip HomeHarvest, run Craigslist only.",
    )
    parser.add_argument(
        "--skip-craigslist", action="store_true",
        help="Never run Craigslist, even if HomeHarvest fails.",
    )

    args = parser.parse_args()
    log = logger.bind(runner="listings")

    hh_result = None
    cl_result = None

    # ── HomeHarvest ──────────────────────────────────────────

    if not args.force_craigslist:
        log.info("running_homeharvest")
        try:
            hh = ListingsPipeline()
            hh_result = hh.run(_build_homeharvest_args(args))
            log.info("homeharvest_complete",
                     status=hh_result.status,
                     loaded=hh_result.records_loaded)
        except Exception as e:
            log.error("homeharvest_failed", error=str(e))

    # ── Fallback decision ────────────────────────────────────

    run_fallback = False

    if args.force_craigslist:
        run_fallback = True
        log.info("forced_craigslist")
    elif args.skip_craigslist:
        run_fallback = False
    elif hh_result is None or hh_result.status == "failed":
        run_fallback = True
        log.info("homeharvest_unavailable_triggering_fallback")
    elif hh_result.records_loaded < args.fallback_threshold:
        run_fallback = True
        log.info("below_threshold",
                 loaded=hh_result.records_loaded,
                 threshold=args.fallback_threshold)

    # ── Craigslist ───────────────────────────────────────────

    if run_fallback:
        log.info("running_craigslist")
        try:
            cl = CraigslistPipeline()
            cl_result = cl.run(_build_craigslist_args(args))
            log.info("craigslist_complete",
                     status=cl_result.status,
                     loaded=cl_result.records_loaded)
        except Exception as e:
            log.error("craigslist_failed", error=str(e))

    # ── Summary ──────────────────────────────────────────────

    hh_loaded = hh_result.records_loaded if hh_result else 0
    cl_loaded = cl_result.records_loaded if cl_result else 0

    both_failed = (
        (args.force_craigslist or hh_result is None or hh_result.status == "failed")
        and (cl_result is None or cl_result.status == "failed")
    )

    log.info("listings_run_complete",
             homeharvest_loaded=hh_loaded,
             craigslist_loaded=cl_loaded,
             total_loaded=hh_loaded + cl_loaded)

    sys.exit(1 if both_failed else 0)


if __name__ == "__main__":
    main()
