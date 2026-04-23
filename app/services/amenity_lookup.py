"""Amenity lookup service -- stored, live Overpass, and Google Places queries.

Three data paths:
  search_stored_amenities: RAW.AMENITIES via Snowflake ST_DISTANCE
  search_overpass_live:    On-demand Overpass QL with exact tags
                           → falls back to Google Places on Overpass failure
  _search_google_places:   Google Places Nearby Search (fallback only)

The agent decides which public function to call. Stored for broad "what's
nearby" questions. Live for specific preferences where the Organizer has
already expanded user text into Overpass tags via LLM. Google Places is
never called directly — it activates automatically when Overpass times out
or errors.

Usage:
    from app.services.amenity_lookup import search_stored_amenities
    with snowflake_cursor() as cursor:
        result = search_stored_amenities(cursor, lat=42.35, lon=-71.06,
                                         subcategory="pharmacy")

    from app.services.amenity_lookup import search_overpass_live
    result = search_overpass_live(lat=42.35, lon=-71.06,
                                  tags={"amenity": "restaurant", "cuisine": "korean"})
"""

from __future__ import annotations

import math
import os
import time
from typing import Optional

import httpx
import structlog

from app.core.config_loader import CONFIG_DIR
from app.core.cache import get_cache
from app.services.listing_queries import QueryResult, _rows_to_dicts, _clamp

logger = structlog.get_logger()

_BOSTON_LAT_RAD = math.radians(42.35)


# -- Config ---------------------------------------------------------------

def _cfg() -> dict:
    if not hasattr(_cfg, "_cache"):
        import yaml
        with open(CONFIG_DIR / "services.yml", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        _cfg._cache = raw.get("amenity_lookup", {})
    return _cfg._cache


def reload_config():
    if hasattr(_cfg, "_cache"):
        del _cfg._cache


def _bbox_deltas(radius_m: int) -> tuple[float, float]:
    dlat = radius_m / 111_000
    dlon = radius_m / (111_000 * math.cos(_BOSTON_LAT_RAD))
    return dlat, dlon


# -- Stored amenities (Snowflake) ----------------------------------------

def search_stored_amenities(
    cursor,
    lat: float,
    lon: float,
    *,
    subcategory: Optional[str] = None,
    category: Optional[str] = None,
    name_contains: Optional[str] = None,
    radius_m: Optional[int] = None,
    limit: Optional[int] = None,
) -> QueryResult:
    """Query RAW.AMENITIES within radius of a point.

    Args:
        cursor: Snowflake cursor.
        lat, lon: Center point.
        subcategory: Exact subcategory match (e.g. "pharmacy", "cafe").
        category: Exact OSM category match (e.g. "amenity", "shop", "leisure").
        name_contains: Case-insensitive name search.
        radius_m: Search radius in meters.
        limit: Max results.
    """
    cfg = _cfg()
    stored = cfg.get("stored", {})
    log = logger.bind(service="amenity_lookup", query="stored")

    if not cfg.get("enabled", True) or not stored.get("enabled", True):
        return QueryResult(success=False, query_type="stored_amenities",
                           error="Stored amenity lookup is disabled")

    radius_m = _clamp(radius_m or stored.get("default_radius_m", 800),
                      1, stored.get("max_radius_m", 3000))
    limit = _clamp(limit or stored.get("max_results", 100),
                   1, stored.get("max_results", 100))

    dlat, dlon = _bbox_deltas(radius_m)

    conditions = [
        "a.lat BETWEEN %s AND %s",
        "a.lon BETWEEN %s AND %s",
        f"ST_DISTANCE(ST_MAKEPOINT(%s, %s), ST_MAKEPOINT(a.lon, a.lat)) <= {radius_m}",
        "a.lat IS NOT NULL",
    ]
    params = [
        lat - dlat, lat + dlat,
        lon - dlon, lon + dlon,
        lon, lat,
    ]

    if subcategory:
        conditions.append("LOWER(a.subcategory) = LOWER(%s)")
        params.append(subcategory)
    if category:
        conditions.append("LOWER(a.category) = LOWER(%s)")
        params.append(category)
    if name_contains:
        conditions.append("LOWER(a.name) LIKE %s")
        params.append(f"%{name_contains.lower()}%")

    where = " AND ".join(conditions)

    sql = f"""
        SELECT
            a.osm_id, a.name, a.category, a.subcategory,
            a.lat, a.lon, a.address, a.opening_hours,
            a.website, a.phone, a.brand, a.tags,
            ROUND(ST_DISTANCE(
                ST_MAKEPOINT(%s, %s), ST_MAKEPOINT(a.lon, a.lat)
            )) AS distance_m
        FROM RAW.AMENITIES a
        WHERE {where}
        ORDER BY distance_m ASC
        LIMIT {limit}
    """
    params = [lon, lat] + params

    start = time.perf_counter()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("query_failed", error=str(e)[:200])
        return QueryResult(success=False, query_type="stored_amenities",
                           error=str(e)[:500])

    data = _rows_to_dicts(cursor, rows)
    log.info("complete", results=len(data), radius_m=radius_m, ms=ms)

    return QueryResult(
        success=True, query_type="stored_amenities",
        data=data, total_count=len(data), duration_ms=ms,
        sql_executed=sql.strip(),
    )


# -- OSM tag → Google Places type mapping ---------------------------------

# Maps common OSM tag values to Google Places API types.
# Used when Overpass fails and we fall back to Google Places.
# Reference: https://developers.google.com/maps/documentation/places/web-service/supported_types
_OSM_TO_PLACES_TYPE = {
    # amenity values
    "restaurant": "restaurant",
    "cafe": "cafe",
    "bar": "bar",
    "pub": "bar",
    "fast_food": "restaurant",
    "pharmacy": "pharmacy",
    "bank": "bank",
    "post_office": "post_office",
    "library": "library",
    "hospital": "hospital",
    "clinic": "doctor",
    "dentist": "dentist",
    "veterinary": "veterinary_care",
    "school": "school",
    "university": "university",
    "place_of_worship": "church",
    "cinema": "movie_theater",
    "theatre": "movie_theater",
    "parking": "parking",
    "fuel": "gas_station",
    "car_wash": "car_wash",
    # leisure values
    "fitness_centre": "gym",
    "gym": "gym",
    "swimming_pool": "swimming_pool",
    "park": "park",
    "playground": "park",
    "sports_centre": "gym",
    # shop values
    "supermarket": "supermarket",
    "convenience": "convenience_store",
    "bakery": "bakery",
    "butcher": "store",
    "clothes": "clothing_store",
    "hairdresser": "hair_care",
    "laundry": "laundry",
    "dry_cleaning": "laundry",
    "electronics": "electronics_store",
    "hardware": "hardware_store",
    "bookshop": "book_store",
    "florist": "florist",
    "pet": "pet_store",
}


def _osm_tags_to_places_params(tags: dict) -> dict | None:
    """Convert OSM tag dict to Google Places Nearby Search params.

    Args:
        tags: e.g. {"amenity": "restaurant", "cuisine": "korean"}

    Returns:
        Dict with "type" and optionally "keyword", or None if no mapping.
    """
    places_type = None
    keyword_parts = []

    for key, value in tags.items():
        if key in ("amenity", "leisure", "shop"):
            mapped = _OSM_TO_PLACES_TYPE.get(value)
            if mapped:
                places_type = mapped
            else:
                # Unknown OSM value — use it as a keyword
                keyword_parts.append(value.replace("_", " "))
        elif key == "cuisine":
            keyword_parts.append(value.replace("_", " "))
        elif key == "sport":
            keyword_parts.append(value.replace("_", " "))
        elif key == "name":
            keyword_parts.append(value)
        else:
            keyword_parts.append(f"{value}".replace("_", " "))

    if not places_type and not keyword_parts:
        return None

    params = {}
    if places_type:
        params["type"] = places_type
    if keyword_parts:
        params["keyword"] = " ".join(keyword_parts)

    return params


# -- Google Places Nearby Search (fallback) --------------------------------

def _search_google_places(
    lat: float,
    lon: float,
    tags: dict,
    radius_m: int,
    limit: int,
) -> QueryResult | None:
    """Google Places Nearby Search as fallback when Overpass fails.

    Uses the same GOOGLE_MAPS_API key used by routes.py for geocoding
    and directions. Returns None if Places is disabled or no key is set.
    Results are cached in Redis with the same TTL as Overpass results.
    """
    cfg = _cfg()
    gp = cfg.get("google_places", {})
    log = logger.bind(service="amenity_lookup", query="google_places")

    if not gp.get("enabled", False):
        log.debug("google_places_disabled")
        return None

    # Get API key — same env var as routes.py
    api_key = os.environ.get("GOOGLE_MAPS_API", "").strip()
    if not api_key:
        log.debug("google_places_no_key")
        return None

    # Convert OSM tags to Places params
    places_params = _osm_tags_to_places_params(tags)
    if not places_params:
        log.debug("google_places_no_mapping", tags=tags)
        return None

    radius_m = _clamp(radius_m, 1, gp.get("max_radius_m", 3000))
    limit = _clamp(limit, 1, gp.get("max_results", 20))
    cache_ttl = gp.get("cache_ttl", 86400)

    # Check cache
    cache = get_cache()
    cache_key = f"gplaces:{lat:.4f},{lon:.4f}:{places_params}:{radius_m}"
    if cache.enabled:
        from app.core.cache import _MISS
        hit = cache.get(cache_key)
        if hit is not _MISS and hit is not None:
            log.info("cache_hit", tags=tags)
            return QueryResult(
                success=True, query_type="google_places",
                data=hit, total_count=len(hit),
            )

    # Build request
    request_params = {
        "location": f"{lat},{lon}",
        "radius": radius_m,
        "key": api_key,
        **places_params,
    }

    start = time.perf_counter()
    try:
        resp = httpx.get(
            "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
            params=request_params,
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        ms = int((time.perf_counter() - start) * 1000)
        log.error("query_failed", error=str(e)[:200], ms=ms)
        return None

    ms = int((time.perf_counter() - start) * 1000)
    status = raw.get("status", "UNKNOWN")

    if status not in ("OK", "ZERO_RESULTS"):
        log.error("api_error", status=status,
                  error=raw.get("error_message", "")[:200])
        return None

    # Parse results
    data = []
    for place in raw.get("results", [])[:limit]:
        loc = place.get("geometry", {}).get("location", {})
        p_lat = loc.get("lat")
        p_lon = loc.get("lng")
        if not p_lat or not p_lon:
            continue

        dist = _haversine_m(lat, lon, p_lat, p_lon)

        data.append({
            "place_id": place.get("place_id", ""),
            "name": place.get("name", ""),
            "lat": p_lat,
            "lon": p_lon,
            "address": place.get("vicinity", ""),
            "opening_hours": "Open now" if place.get("opening_hours", {}).get("open_now") else "",
            "rating": place.get("rating"),
            "user_ratings_total": place.get("user_ratings_total"),
            "price_level": place.get("price_level"),
            "types": place.get("types", []),
            "distance_m": round(dist),
            "source": "google_places",
        })

    data.sort(key=lambda x: x["distance_m"])

    # Cache
    if cache.enabled and data:
        cache.set(cache_key, data, ttl=cache_ttl)

    log.info("complete", results=len(data), tags=tags,
             radius_m=radius_m, ms=ms, source="google_places")

    return QueryResult(
        success=True, query_type="google_places",
        data=data, total_count=len(data), duration_ms=ms,
    )


# -- Live Overpass with Google Places fallback -----------------------------

def search_overpass_live(
    lat: float,
    lon: float,
    *,
    tags: dict,
    radius_m: Optional[int] = None,
    limit: Optional[int] = None,
) -> QueryResult:
    """Live Overpass QL query for exact OSM tags near a point.

    Falls back to Google Places Nearby Search if Overpass fails (timeout,
    503, rate limit). Redis cache is checked first for both paths.

    Args:
        lat, lon: Center point.
        tags: Exact OSM tag filters, e.g. {"amenity": "restaurant", "cuisine": "korean"}.
        radius_m: Search radius in meters.
        limit: Max results.
    """
    cfg = _cfg()
    ov = cfg.get("overpass", {})
    log = logger.bind(service="amenity_lookup", query="overpass_live")

    if not cfg.get("enabled", True) or not ov.get("enabled", True):
        return QueryResult(success=False, query_type="overpass_live",
                           error="Live Overpass lookup is disabled")

    if not tags:
        return QueryResult(success=False, query_type="overpass_live",
                           error="No tags provided")

    radius_m = _clamp(radius_m or ov.get("default_radius_m", 800),
                      1, ov.get("max_radius_m", 3000))
    limit = _clamp(limit or ov.get("max_results", 50),
                   1, ov.get("max_results", 50))
    base_url = ov.get("base_url", "https://overpass-api.de/api/interpreter")
    timeout = ov.get("timeout", 30)
    cache_ttl = ov.get("cache_ttl", 86400)

    # Build Overpass QL
    tag_filters = "".join(f'["{k}"="{v}"]' for k, v in tags.items())
    query = (
        f'[out:json][timeout:{timeout}];'
        f'('
        f'  node{tag_filters}(around:{radius_m},{lat},{lon});'
        f'  way{tag_filters}(around:{radius_m},{lat},{lon});'
        f');'
        f'out center {limit};'
    )

    # Check cache
    cache = get_cache()
    cache_key = f"overpass:{lat:.4f},{lon:.4f}:{tag_filters}:{radius_m}"
    if cache.enabled:
        from app.core.cache import _MISS
        hit = cache.get(cache_key)
        if hit is not _MISS and hit is not None:
            log.info("cache_hit", tags=tags)
            return QueryResult(
                success=True, query_type="overpass_live",
                data=hit, total_count=len(hit),
            )

    start = time.perf_counter()
    try:
        resp = httpx.post(
            base_url,
            data={"data": query},
            timeout=timeout,
            headers={"User-Agent": "VicinityBot/1.0"},
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        ms = int((time.perf_counter() - start) * 1000)
        log.warning("overpass_failed_trying_google_places",
                    error=str(e)[:200], ms=ms)
        # ── Fallback: Google Places Nearby Search ────────────
        places_result = _search_google_places(lat, lon, tags, radius_m, limit)
        if places_result and places_result.success:
            return places_result
        # Both failed — return the original Overpass error
        return QueryResult(success=False, query_type="overpass_live",
                           error=str(e)[:500])

    ms = int((time.perf_counter() - start) * 1000)

    # Parse results
    data = []
    for elem in raw.get("elements", []):
        elem_lat = elem.get("lat") or elem.get("center", {}).get("lat")
        elem_lon = elem.get("lon") or elem.get("center", {}).get("lon")
        if not elem_lat or not elem_lon:
            continue

        t = elem.get("tags", {})
        dist = _haversine_m(lat, lon, elem_lat, elem_lon)

        data.append({
            "osm_id": elem.get("id"),
            "name": t.get("name", ""),
            "lat": elem_lat,
            "lon": elem_lon,
            "address": _build_address(t),
            "opening_hours": t.get("opening_hours", ""),
            "phone": t.get("phone", ""),
            "website": t.get("website", ""),
            "cuisine": t.get("cuisine", ""),
            "distance_m": round(dist),
        })

    data.sort(key=lambda x: x["distance_m"])

    # Also try Google Places if Overpass returned zero results
    if not data:
        log.info("overpass_empty_trying_google_places", tags=tags)
        places_result = _search_google_places(lat, lon, tags, radius_m, limit)
        if places_result and places_result.success and places_result.data:
            return places_result

    # Cache Overpass results
    if cache.enabled and data:
        cache.set(cache_key, data, ttl=cache_ttl)

    log.info("complete", results=len(data), tags=tags,
             radius_m=radius_m, ms=ms)

    return QueryResult(
        success=True, query_type="overpass_live",
        data=data, total_count=len(data), duration_ms=ms,
    )


# -- Helpers --------------------------------------------------------------

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _build_address(tags: dict) -> str:
    parts = []
    for key in ("addr:housenumber", "addr:street", "addr:city", "addr:postcode"):
        val = tags.get(key, "").strip()
        if val:
            parts.append(val)
    return ", ".join(parts)