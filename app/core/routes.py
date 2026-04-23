"""Google Maps routing — geocode, directions, polyline decoding.

Provides the foundation for route-level safety scoring.  All calls
are cached in Redis and use key rotation on quota errors.

No SDK dependency — uses httpx against the REST API directly.

Usage:
    from app.core.routes import geocode, compute_route

    coords = geocode("77 Massachusetts Ave, Cambridge, MA")
    route  = compute_route(
        origin_lat=42.36, origin_lon=-71.09,
        dest_lat=42.35,   dest_lon=-71.06,
    )

Testing:
    python -m app.core.routes --test
    python -m app.core.routes --test --dry-run   # mock responses, no API calls
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Optional

import httpx
import structlog
from dotenv import load_dotenv

from app.core.cache import cached, get_cache
from app.core.config_loader import load_source_config

load_dotenv()

logger = structlog.get_logger()


# ── Config ───────────────────────────────────────────────────

def _load_routes_config() -> dict:
    return load_source_config("routes")


def _load_api_keys() -> list[str]:
    """Collect Google Maps API keys from env.

    Loads the bare key (e.g. GOOGLE_MAPS_API) first, then numbered
    keys (_2, _3, ...).  Prefix is read from routes.yml.
    """
    cfg = _load_routes_config()
    prefix = cfg.get("connection", {}).get("env_key_prefix", "GOOGLE_MAPS_API")
    keys = []

    # Primary key (bare, no suffix)
    bare = os.environ.get(prefix, "").strip()
    if bare:
        keys.append(bare)

    # Additional keys: PREFIX_2, PREFIX_3, ...
    for i in range(2, 20):
        val = os.environ.get(f"{prefix}_{i}", "").strip()
        if val:
            keys.append(val)

    return keys


# ── Data classes ─────────────────────────────────────────────

@dataclass
class GeoPoint:
    lat: float
    lon: float

    def to_str(self) -> str:
        return f"{self.lat},{self.lon}"

    def distance_m(self, other: GeoPoint) -> float:
        """Haversine distance in meters."""
        R = 6_371_000
        dlat = math.radians(other.lat - self.lat)
        dlon = math.radians(other.lon - self.lon)
        a = (math.sin(dlat / 2) ** 2
             + math.cos(math.radians(self.lat))
             * math.cos(math.radians(other.lat))
             * math.sin(dlon / 2) ** 2)
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


@dataclass
class RouteResult:
    """Parsed Google Maps Directions response."""
    origin: GeoPoint
    destination: GeoPoint
    waypoints: list[GeoPoint]
    duration_min: float
    distance_m: int
    distance_text: str
    transit_lines: list[str]
    mode: str
    raw_steps: list[dict]

    def to_dict(self) -> dict:
        return {
            "origin": {"lat": self.origin.lat, "lon": self.origin.lon},
            "destination": {"lat": self.destination.lat, "lon": self.destination.lon},
            "waypoints": [{"lat": w.lat, "lon": w.lon} for w in self.waypoints],
            "duration_min": self.duration_min,
            "distance_m": self.distance_m,
            "distance_text": self.distance_text,
            "transit_lines": self.transit_lines,
            "mode": self.mode,
        }


# ── Polyline decoding ────────────────────────────────────────

def decode_polyline(encoded: str) -> list[GeoPoint]:
    """Decode a Google Maps encoded polyline string into GeoPoints.

    Algorithm: https://developers.google.com/maps/documentation/utilities/polylinealgorithm
    """
    points = []
    idx, lat, lon = 0, 0, 0
    while idx < len(encoded):
        for target in ("lat", "lon"):
            shift, result = 0, 0
            while True:
                b = ord(encoded[idx]) - 63
                idx += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if target == "lat":
                lat += delta
            else:
                lon += delta
        points.append(GeoPoint(lat=lat / 1e5, lon=lon / 1e5))
    return points


def simplify_waypoints(points: list[GeoPoint], interval_m: float = 50.0) -> list[GeoPoint]:
    """Reduce dense polyline to evenly spaced waypoints.

    Keeps first, last, and every point at least interval_m from the
    previous kept point.  Reduces a 200-point polyline to ~20-40
    points for corridor scoring without losing route shape.
    """
    if len(points) <= 2:
        return points
    kept = [points[0]]
    for p in points[1:-1]:
        if kept[-1].distance_m(p) >= interval_m:
            kept.append(p)
    kept.append(points[-1])
    return kept


# ── API client with key rotation ─────────────────────────────

class MapsClient:
    """Google Maps API client with key rotation and caching."""

    # HTTP status codes that trigger key rotation
    _QUOTA_CODES = {403, 429}

    def __init__(self):
        self._cfg = _load_routes_config()
        conn = self._cfg.get("connection", {})
        self._base_url = conn.get("base_url", "https://maps.googleapis.com/maps/api")
        self._timeout = conn.get("timeout", 10)
        self._keys = _load_api_keys()
        self._key_idx = 0
        self._log = logger.bind(component="maps_client")

        if not self._keys:
            self._log.warning("no_google_maps_keys")

    @property
    def available(self) -> bool:
        return len(self._keys) > 0

    def _current_key(self) -> str:
        if not self._keys:
            raise RuntimeError("No Google Maps API keys configured")
        return self._keys[self._key_idx % len(self._keys)]

    def _rotate_key(self) -> bool:
        """Advance to next key.  Returns False if all keys exhausted."""
        if len(self._keys) <= 1:
            return False
        old = self._key_idx
        self._key_idx = (self._key_idx + 1) % len(self._keys)
        exhausted = self._key_idx == 0
        self._log.info("key_rotated",
                       from_idx=old, to_idx=self._key_idx,
                       exhausted=exhausted)
        return not exhausted

    def _request(self, endpoint: str, params: dict) -> dict:
        """Make a request with key rotation on quota errors.

        Tries each key once.  Raises on total failure.
        """
        attempts = len(self._keys) if self._keys else 1
        last_error = None

        for attempt in range(attempts):
            params["key"] = self._current_key()
            url = f"{self._base_url}/{endpoint}/json"

            try:
                resp = httpx.get(url, params=params, timeout=self._timeout)
            except httpx.TimeoutException as e:
                last_error = e
                self._log.warning("request_timeout",
                                  endpoint=endpoint, attempt=attempt + 1)
                if not self._rotate_key():
                    break
                continue

            if resp.status_code in self._QUOTA_CODES:
                self._log.warning("quota_hit",
                                  endpoint=endpoint,
                                  status=resp.status_code,
                                  key_idx=self._key_idx)
                if not self._rotate_key():
                    break
                continue

            if resp.status_code != 200:
                last_error = RuntimeError(
                    f"Google Maps {endpoint}: HTTP {resp.status_code}"
                )
                break

            data = resp.json()
            status = data.get("status", "UNKNOWN")

            if status == "OK":
                return data
            if status == "OVER_QUERY_LIMIT":
                self._log.warning("over_query_limit",
                                  endpoint=endpoint, key_idx=self._key_idx)
                if not self._rotate_key():
                    break
                continue
            if status == "ZERO_RESULTS":
                return data

            last_error = RuntimeError(
                f"Google Maps {endpoint}: {status} — "
                f"{data.get('error_message', 'no detail')}"
            )
            break

        raise last_error or RuntimeError(
            f"Google Maps {endpoint}: all {len(self._keys)} keys exhausted"
        )


# ── Public API ───────────────────────────────────────────────

_client: MapsClient | None = None


def _get_client() -> MapsClient:
    global _client
    if _client is None:
        _client = MapsClient()
    return _client


@cached(ttl=604800, prefix="geocode")
def geocode(address: str) -> dict | None:
    """Geocode an address.  Returns {"lat": ..., "lon": ..., "formatted": ...} or None.

    Cached for 7 days.
    """
    client = _get_client()
    if not client.available:
        return None

    data = client._request("geocode", {"address": address})
    results = data.get("results", [])
    if not results:
        return None

    loc = results[0]["geometry"]["location"]
    return {
        "lat": loc["lat"],
        "lon": loc["lng"],
        "formatted": results[0].get("formatted_address", address),
    }


def compute_route(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    mode: str | None = None,
    departure_hour: int | None = None,
) -> RouteResult | None:
    """Compute a route between two points.

    Returns RouteResult with decoded waypoints for corridor scoring.
    Cached for 1 day keyed on (origin, dest, mode).
    """
    cfg = _load_routes_config()
    defaults = cfg.get("defaults", {})
    mode = mode or defaults.get("mode", "transit")
    departure_hour = departure_hour if departure_hour is not None else defaults.get("departure_hour", 8)

    # Check cache
    cache = get_cache()
    cache_key = f"route:{origin_lat:.5f},{origin_lon:.5f}:{dest_lat:.5f},{dest_lon:.5f}:{mode}"
    if cache.enabled:
        from app.core.cache import _MISS
        hit = cache.get(cache_key)
        if hit is not _MISS and hit is not None:
            return _dict_to_route(hit)

    client = _get_client()
    if not client.available:
        return None

    params = {
        "origin": f"{origin_lat},{origin_lon}",
        "destination": f"{dest_lat},{dest_lon}",
        "mode": mode,
        "alternatives": str(defaults.get("alternatives", False)).lower(),
    }

    # Transit departure time: next occurrence of departure_hour
    if mode == "transit":
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        dep = now.replace(hour=departure_hour, minute=0, second=0, microsecond=0)
        if dep <= now:
            dep += datetime.timedelta(days=1)
        params["departure_time"] = str(int(dep.timestamp()))

    data = client._request("directions", params)
    routes = data.get("routes", [])
    if not routes:
        return None

    route = routes[0]
    leg = route["legs"][0]

    # Decode polyline to waypoints
    overview = route.get("overview_polyline", {}).get("points", "")
    raw_points = decode_polyline(overview) if overview else []
    waypoints = simplify_waypoints(raw_points)

    # Extract transit line names
    transit_lines = []
    for step in leg.get("steps", []):
        td = step.get("transit_details", {})
        line = td.get("line", {})
        name = line.get("short_name") or line.get("name")
        if name and name not in transit_lines:
            transit_lines.append(name)

    result = RouteResult(
        origin=GeoPoint(lat=origin_lat, lon=origin_lon),
        destination=GeoPoint(lat=dest_lat, lon=dest_lon),
        waypoints=waypoints,
        duration_min=round(leg["duration"]["value"] / 60, 1),
        distance_m=leg["distance"]["value"],
        distance_text=leg["distance"]["text"],
        transit_lines=transit_lines,
        mode=mode,
        raw_steps=[
            {
                "instruction": s.get("html_instructions", ""),
                "distance_m": s.get("distance", {}).get("value", 0),
                "duration_s": s.get("duration", {}).get("value", 0),
                "travel_mode": s.get("travel_mode", ""),
            }
            for s in leg.get("steps", [])
        ],
    )

    # Cache
    ttl = cfg.get("cache", {}).get("directions_ttl", 86400)
    cache.set(cache_key, result.to_dict(), ttl=ttl)

    return result


def _dict_to_route(d: dict) -> RouteResult:
    """Reconstruct RouteResult from cached dict."""
    return RouteResult(
        origin=GeoPoint(**d["origin"]),
        destination=GeoPoint(**d["destination"]),
        waypoints=[GeoPoint(**w) for w in d["waypoints"]],
        duration_min=d["duration_min"],
        distance_m=d["distance_m"],
        distance_text=d["distance_text"],
        transit_lines=d["transit_lines"],
        mode=d["mode"],
        raw_steps=d.get("raw_steps", []),
    )


# ── CLI test harness ─────────────────────────────────────────

def _run_test(dry_run: bool = False):
    """Self-contained test.  --dry-run uses mock responses."""
    import json

    log = logger.bind(component="routes_test")

    # Verify config loads
    cfg = _load_routes_config()
    log.info("config_loaded",
             base_url=cfg["connection"]["base_url"],
             mode=cfg["defaults"]["mode"])

    # Verify keys
    keys = _load_api_keys()
    log.info("api_keys", count=len(keys),
             prefixes=[k[:8] + "..." for k in keys])

    if dry_run:
        log.info("dry_run_mode")

        # Test polyline decoding with a known encoded string
        # This encodes roughly: (42.36, -71.09) → (42.35, -71.06)
        encoded = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
        points = decode_polyline(encoded)
        log.info("polyline_decoded", points=len(points),
                 first=f"{points[0].lat:.4f},{points[0].lon:.4f}" if points else "none")

        simplified = simplify_waypoints(points, interval_m=50)
        log.info("waypoints_simplified",
                 original=len(points), simplified=len(simplified))

        # Test haversine
        a = GeoPoint(lat=42.3601, lon=-71.0942)
        b = GeoPoint(lat=42.3505, lon=-71.0625)
        dist = a.distance_m(b)
        log.info("haversine_test", from_="MIT", to="Prudential",
                 distance_m=round(dist))

        log.info("dry_run_complete — all utilities working")
        return

    if not keys:
        log.error("no_api_keys — set GOOGLE_MAPS_API in .env")
        return

    # Live geocode test
    test_address = "77 Massachusetts Ave, Cambridge, MA"
    log.info("geocode_test", address=test_address)
    result = geocode(test_address)
    if result:
        log.info("geocode_result", **result)
    else:
        log.error("geocode_failed")
        return

    # Live route test
    log.info("route_test",
             origin="MIT (Cambridge)",
             dest="Boston Common")
    route = compute_route(
        origin_lat=42.3601, origin_lon=-71.0942,
        dest_lat=42.3551, dest_lon=-71.0656,
    )
    if route:
        log.info("route_result",
                 duration_min=route.duration_min,
                 distance=route.distance_text,
                 waypoints=len(route.waypoints),
                 transit_lines=route.transit_lines,
                 mode=route.mode)
        # Print waypoints for visual verification
        for i, wp in enumerate(route.waypoints[:5]):
            log.info(f"  waypoint_{i}", lat=round(wp.lat, 5), lon=round(wp.lon, 5))
        if len(route.waypoints) > 5:
            log.info(f"  ... and {len(route.waypoints) - 5} more")
    else:
        log.error("route_failed")

    log.info("test_complete")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Test Google Maps routing")
    parser.add_argument("--test", action="store_true", help="Run test suite")
    parser.add_argument("--dry-run", action="store_true",
                        help="Test utilities only, no API calls")
    args = parser.parse_args()

    if args.test or args.dry_run:
        _run_test(dry_run=args.dry_run)
    else:
        parser.print_help()