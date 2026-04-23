"""Test: Google Places fallback for amenity lookup.

Verifies the full chain: OSM tag mapping, API key loading, live
Google Places Nearby Search, Redis caching, and the Overpass→Places
fallback path.

Run from project root:
    python test_google_places.py
    python test_google_places.py --dry-run    # skip live API calls
    python test_google_places.py --verbose     # show full response data

Prerequisites:
    - GOOGLE_MAPS_API set in .env
    - Places API enabled in Google Cloud Console
    - config/sources/services.yml has google_places.enabled: true
    - Redis running (optional — tests degrade gracefully without it)
"""

import os
import sys
import json
import time
import argparse

from dotenv import load_dotenv

load_dotenv()


def _header(text: str):
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print(f"{'=' * 60}\n")


def _pass(msg: str):
    print(f"  ✓ {msg}")


def _fail(msg: str):
    print(f"  ✗ FAIL: {msg}")


def _info(msg: str):
    print(f"    {msg}")


def run_tests(dry_run: bool = False, verbose: bool = False):
    failures = 0

    # =================================================================
    # Test 1: API key is set
    # =================================================================
    _header("Test 1: API key detection")

    key = os.environ.get("GOOGLE_MAPS_API", "").strip()
    if key:
        _pass(f"GOOGLE_MAPS_API found ({key[:8]}...{key[-4:]})")
    else:
        _fail("GOOGLE_MAPS_API not set in .env")
        print("\n  Set GOOGLE_MAPS_API in your .env file.")
        print("  This is the same key used for geocoding and directions.")
        print("  Enable 'Places API' in Google Cloud Console.")
        failures += 1

    # Also check numbered keys
    for i in range(2, 5):
        extra = os.environ.get(f"GOOGLE_MAPS_API_{i}", "").strip()
        if extra:
            _info(f"GOOGLE_MAPS_API_{i} also found")

    # =================================================================
    # Test 2: Config loads correctly
    # =================================================================
    _header("Test 2: Config loading")

    try:
        from app.services.amenity_lookup import _cfg, reload_config
        reload_config()
        cfg = _cfg()

        gp = cfg.get("google_places", {})
        ov = cfg.get("overpass", {})

        _pass(f"amenity_lookup config loaded")
        _info(f"overpass.enabled: {ov.get('enabled')}")
        _info(f"google_places.enabled: {gp.get('enabled')}")
        _info(f"google_places.default_radius_m: {gp.get('default_radius_m')}")
        _info(f"google_places.max_results: {gp.get('max_results')}")

        if not gp.get("enabled"):
            _fail("google_places.enabled is false in services.yml")
            _info("Set google_places.enabled: true in config/sources/services.yml")
            failures += 1
        else:
            _pass("google_places is enabled")

    except Exception as e:
        _fail(f"Config load failed: {e}")
        failures += 1

    # =================================================================
    # Test 3: OSM → Google Places type mapping
    # =================================================================
    _header("Test 3: OSM tag → Google Places mapping")

    from app.services.amenity_lookup import _osm_tags_to_places_params

    test_cases = [
        ({"amenity": "restaurant", "cuisine": "korean"}, "restaurant", "korean"),
        ({"leisure": "fitness_centre"}, "gym", None),
        ({"amenity": "cafe"}, "cafe", None),
        ({"amenity": "pharmacy"}, "pharmacy", None),
        ({"shop": "supermarket"}, "supermarket", None),
        ({"shop": "laundry"}, "laundry", None),
        ({"amenity": "bar"}, "bar", None),
        ({"leisure": "park"}, "park", None),
        ({"amenity": "library"}, "library", None),
        # Unmapped — should use keyword fallback
        ({"amenity": "hookah_lounge"}, None, "hookah lounge"),
        ({"cuisine": "ethiopian"}, None, "ethiopian"),
    ]

    for tags, expected_type, expected_kw_substr in test_cases:
        result = _osm_tags_to_places_params(tags)
        tag_str = str(tags)

        if result is None:
            if expected_type is None and expected_kw_substr is None:
                _pass(f"{tag_str} → None (expected)")
            else:
                _fail(f"{tag_str} → None (expected type={expected_type})")
                failures += 1
            continue

        got_type = result.get("type")
        got_kw = result.get("keyword", "")

        ok = True
        if expected_type and got_type != expected_type:
            _fail(f"{tag_str} → type={got_type} (expected {expected_type})")
            ok = False
            failures += 1
        if expected_kw_substr and expected_kw_substr not in got_kw:
            _fail(f"{tag_str} → keyword='{got_kw}' (expected '{expected_kw_substr}' in it)")
            ok = False
            failures += 1
        if ok:
            parts = []
            if got_type:
                parts.append(f"type={got_type}")
            if got_kw:
                parts.append(f"keyword='{got_kw}'")
            _pass(f"{tag_str} → {', '.join(parts)}")

    # =================================================================
    # Test 4: Redis cache check
    # =================================================================
    _header("Test 4: Redis availability")

    try:
        from app.core.cache import get_cache
        cache = get_cache()
        if cache.enabled:
            healthy, latency = cache.ping()
            if healthy:
                _pass(f"Redis connected ({latency}ms)")
            else:
                _info("Redis configured but ping failed — cache will be skipped")
        else:
            _info("Redis not configured — fallback results won't be cached")
    except Exception as e:
        _info(f"Redis check failed: {e} — non-fatal")

    # =================================================================
    # Test 5: Live Google Places API call
    # =================================================================
    _header("Test 5: Live Google Places Nearby Search")

    if dry_run:
        _info("--dry-run: skipping live API call")
    elif not key:
        _info("No API key — skipping live test")
    elif not gp.get("enabled"):
        _info("google_places disabled — skipping live test")
    else:
        from app.services.amenity_lookup import _search_google_places

        # Test: gyms near Mission Hill (the exact failure case)
        test_lat, test_lon = 42.327243, -71.104445
        test_tags = {"leisure": "fitness_centre"}

        _info(f"Searching: gyms near ({test_lat}, {test_lon}), radius=2000m")
        start = time.perf_counter()

        result = _search_google_places(
            test_lat, test_lon, test_tags,
            radius_m=2000, limit=10,
        )

        elapsed = int((time.perf_counter() - start) * 1000)

        if result is None:
            _fail(f"_search_google_places returned None ({elapsed}ms)")
            failures += 1
        elif not result.success:
            _fail(f"API error: {result.error} ({elapsed}ms)")
            failures += 1
        elif not result.data:
            _info(f"Zero results from Google Places ({elapsed}ms) — area may genuinely have no gyms")
        else:
            _pass(f"Found {len(result.data)} gyms in {elapsed}ms")
            for i, place in enumerate(result.data[:5]):
                _info(f"  {i+1}. {place['name']} — {place['distance_m']}m — "
                      f"rating {place.get('rating', 'n/a')} "
                      f"({place.get('user_ratings_total', 0)} reviews)")
                if verbose:
                    _info(f"     address: {place.get('address', '')}")
                    _info(f"     types: {place.get('types', [])}")
                    _info(f"     place_id: {place.get('place_id', '')}")

        # Test: Korean restaurants near Allston
        _info("")
        test_lat2, test_lon2 = 42.3519, -71.1316
        test_tags2 = {"amenity": "restaurant", "cuisine": "korean"}

        _info(f"Searching: Korean restaurants near Allston ({test_lat2}, {test_lon2})")
        start = time.perf_counter()

        result2 = _search_google_places(
            test_lat2, test_lon2, test_tags2,
            radius_m=1500, limit=10,
        )

        elapsed2 = int((time.perf_counter() - start) * 1000)

        if result2 and result2.success and result2.data:
            _pass(f"Found {len(result2.data)} Korean restaurants in {elapsed2}ms")
            for i, place in enumerate(result2.data[:3]):
                _info(f"  {i+1}. {place['name']} — {place['distance_m']}m")
        elif result2 and result2.success:
            _info(f"Zero results for Korean restaurants ({elapsed2}ms)")
        else:
            _fail(f"Korean restaurant search failed ({elapsed2}ms)")
            failures += 1

    # =================================================================
    # Test 6: Full fallback chain (simulate Overpass failure)
    # =================================================================
    _header("Test 6: Overpass → Google Places fallback chain")

    if dry_run:
        _info("--dry-run: skipping fallback test")
    elif not key:
        _info("No API key — skipping fallback test")
    elif not gp.get("enabled"):
        _info("google_places disabled — skipping fallback test")
    else:
        from app.services.amenity_lookup import search_overpass_live, reload_config
        from unittest.mock import patch

        # Simulate Overpass 504 by patching httpx.post to raise
        def _fake_overpass_timeout(*args, **kwargs):
            raise Exception("Server error '504 Gateway Timeout' (simulated)")

        _info("Simulating Overpass 504 timeout...")
        start = time.perf_counter()

        with patch("app.services.amenity_lookup.httpx.post", side_effect=_fake_overpass_timeout):
            result = search_overpass_live(
                42.327243, -71.104445,
                tags={"leisure": "fitness_centre"},
                radius_m=2000, limit=10,
            )

        elapsed = int((time.perf_counter() - start) * 1000)

        if result.success and result.data:
            _pass(f"Fallback worked: {len(result.data)} results via Google Places ({elapsed}ms)")
            _info(f"query_type: {result.query_type}")
            for i, place in enumerate(result.data[:3]):
                _info(f"  {i+1}. {place['name']} — {place['distance_m']}m")
        elif result.success:
            _info(f"Fallback fired but zero results ({elapsed}ms) — may be area-specific")
        else:
            _fail(f"Fallback failed: {result.error} ({elapsed}ms)")
            _info("Check that google_places.enabled is true and GOOGLE_MAPS_API is set")
            failures += 1

    # =================================================================
    # Test 7: Overpass success path still works (no fallback triggered)
    # =================================================================
    _header("Test 7: Overpass success path (no fallback)")

    if dry_run:
        _info("--dry-run: skipping live Overpass test")
    else:
        from app.services.amenity_lookup import search_overpass_live

        _info("Querying Overpass for pharmacies near Boston Common...")
        start = time.perf_counter()

        result = search_overpass_live(
            42.3551, -71.0656,
            tags={"amenity": "pharmacy"},
            radius_m=1000, limit=5,
        )

        elapsed = int((time.perf_counter() - start) * 1000)

        if result.success:
            _pass(f"Overpass returned {len(result.data)} pharmacies in {elapsed}ms")
            if result.query_type == "overpass_live":
                _pass("Used Overpass directly (no fallback)")
            elif result.query_type == "google_places":
                _info("Overpass failed, used Google Places fallback")
            for i, place in enumerate(result.data[:3]):
                _info(f"  {i+1}. {place.get('name', 'unnamed')} — {place['distance_m']}m")
        else:
            _info(f"Overpass also failed ({elapsed}ms): {result.error}")
            _info("This is expected if Overpass is down — the fallback test (Test 6) covers this")

    # =================================================================
    # Summary
    # =================================================================
    _header("RESULTS")

    if failures == 0:
        print("  ALL TESTS PASSED ✓")
    else:
        print(f"  {failures} FAILURE(S)")

    return failures


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test Google Places fallback")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip live API calls, test config and mapping only")
    parser.add_argument("--verbose", action="store_true",
                        help="Show full response data")
    args = parser.parse_args()

    failures = run_tests(dry_run=args.dry_run, verbose=args.verbose)
    sys.exit(1 if failures else 0)