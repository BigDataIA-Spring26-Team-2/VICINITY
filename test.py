#!/usr/bin/env python3
"""Craigslist rentals scraper and parser for VICINITY listings schema.

Loads locations from config/sources/listings.yml (homeharvest.locations) unless
--location is provided. Crawls Craigslist apartment rentals, fetches detail
pages, and emits JSON logs for every request, parse step, and record.

Usage:
  python -m test --max-pages 2 --limit 50
  python -m test --location "Boston, MA" --location "Cambridge, MA"
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib import request as urlrequest
from urllib.parse import urlencode, urlparse
from urllib.robotparser import RobotFileParser
import xml.etree.ElementTree as ET

try:
    import httpx  # type: ignore
except Exception:
    httpx = None

try:
    import yaml  # type: ignore
except Exception:
    yaml = None

ROOT = Path(__file__).resolve().parent
LISTINGS_CONFIG = ROOT / "config" / "sources" / "listings.yml"
SPATIAL_CONFIG = ROOT / "config" / "spatial.yml"

DEFAULT_LOCATIONS = ["Boston, MA", "Cambridge, MA"]
DEFAULT_CATEGORY = "apa"
DEFAULT_PAGE_SIZE = 120
DEFAULT_MAX_PAGES = 5
DEFAULT_TIMEOUT = 20.0
DEFAULT_MIN_DELAY = 2.5
DEFAULT_MAX_DELAY = 30.0
DEFAULT_JITTER = 0.35
DEFAULT_MAX_RETRIES = 4
DEFAULT_CLIENT_MODE = "auto"
DEFAULT_SCHEME = "https"
DEFAULT_RSS_FALLBACK = True

RETRY_STATUSES = {403, 408, 429, 500, 502, 503, 504}

DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

SCHEMA_FIELDS = [
    "property_id",
    "property_url",
    "list_price",
    "beds",
    "full_baths",
    "sqft",
    "street",
    "unit",
    "city",
    "zip_code",
    "latitude",
    "longitude",
    "text",
    "primary_photo",
    "mls_id",
    "mls_status",
    "days_on_mls",
    "agent_name",
    "style",
    "list_date",
]

REQUIRED_FIELDS = ["property_id", "property_url", "list_price", "latitude", "longitude"]

CITY_SITE_OVERRIDES = {
    "boston": "boston.craigslist.org",
    "cambridge": "boston.craigslist.org",
    "somerville": "boston.craigslist.org",
    "brookline": "boston.craigslist.org",
}

STATE_SITE_DEFAULTS = {"MA": "boston.craigslist.org"}

RUN_ID = "unknown"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(level: str, event: str, **fields: Any) -> None:
    parts = [utc_now(), level.upper(), event, f"run_id={RUN_ID}"]
    for key, value in sorted(fields.items()):
        text = repr(value) if isinstance(value, (dict, list, tuple)) else str(value)
        text = " ".join(text.split())
        parts.append(f"{key}={text}")
    print(" ".join(parts), flush=True)


def log_exception(event: str, exc: BaseException) -> None:
    log(
        "error",
        event,
        error_type=type(exc).__name__,
        error=str(exc),
        traceback=traceback.format_exc(),
    )


def dedupe_keep_order(items: Iterable[str]) -> List[str]:
    seen = set()
    output = []
    for item in items:
        value = str(item).strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output


def normalize_whitespace(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return " ".join(value.split())


def strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value)


def clean_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return normalize_whitespace(unescape(value))


def to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
    raw = str(value)
    digits = re.sub(r"[^0-9.]+", "", raw)
    if not digits:
        return None
    try:
        return int(float(digits))
    except (TypeError, ValueError):
        return None


def to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    raw = value.strip().replace("Z", "+00:00")
    if re.search(r"[+-]\d{4}$", raw):
        raw = raw[:-5] + raw[-5:-2] + ":" + raw[-2:]
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_datetime(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def days_since(value: Optional[datetime], now: datetime) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(0, int((now - value).total_seconds() // 86400))


def build_url(base_url: str, params: Optional[Dict[str, Any]] = None) -> str:
    if not params:
        return base_url
    query = urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
    return f"{base_url}?{query}" if query else base_url


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    if yaml is None:
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log("error", "yaml_load_failed", path=str(path), error=str(exc))
        return {}


def extract_locations_from_text(text: str) -> List[str]:
    locations: List[str] = []
    in_homeharvest = False
    in_locations = False
    indent_homeharvest = 0
    indent_locations = 0

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))

        if stripped.startswith("homeharvest:"):
            in_homeharvest = True
            in_locations = False
            indent_homeharvest = indent
            continue

        if in_homeharvest and indent <= indent_homeharvest:
            in_homeharvest = False
            in_locations = False

        if in_homeharvest and stripped.startswith("locations:"):
            in_locations = True
            indent_locations = indent
            continue

        if in_locations:
            if indent <= indent_locations:
                in_locations = False
                continue
            if stripped.startswith("-"):
                value = stripped[1:].strip().strip("\"").strip("'")
                if value:
                    locations.append(value)

    return locations


def load_locations(path: Path, overrides: Optional[List[str]]) -> List[str]:
    if overrides:
        return dedupe_keep_order(overrides)

    data = load_yaml(path)
    locations: List[str] = []
    if data:
        homeharvest = data.get("homeharvest", {})
        if isinstance(homeharvest, dict):
            raw = homeharvest.get("locations")
            if isinstance(raw, list):
                locations = [str(item).strip() for item in raw if str(item).strip()]

    if not locations and path.exists():
        text = path.read_text(encoding="utf-8", errors="ignore")
        locations = extract_locations_from_text(text)

    if not locations:
        locations = DEFAULT_LOCATIONS[:]

    return dedupe_keep_order(locations)


def load_bbox(path: Path) -> Optional[Dict[str, float]]:
    data = load_yaml(path)
    bbox = data.get("boston_bbox") if data else None
    if isinstance(bbox, dict):
        try:
            return {key: float(bbox[key]) for key in ("min_lat", "max_lat", "min_lon", "max_lon")}
        except Exception:
            return None

    if path.exists():
        text = path.read_text(encoding="utf-8", errors="ignore")
        values: Dict[str, float] = {}
        for key in ("min_lat", "max_lat", "min_lon", "max_lon"):
            match = re.search(rf"{key}\s*:\s*([\-0-9.]+)", text)
            if match:
                try:
                    values[key] = float(match.group(1))
                except ValueError:
                    continue
        if len(values) == 4:
            return values

    return None


def in_bbox(lat: Optional[float], lon: Optional[float], bbox: Optional[Dict[str, float]]) -> Optional[bool]:
    if lat is None or lon is None or bbox is None:
        return None
    return bbox["min_lat"] <= lat <= bbox["max_lat"] and bbox["min_lon"] <= lon <= bbox["max_lon"]


def parse_location(location: str) -> Tuple[str, str]:
    parts = [part.strip() for part in location.split(",") if part.strip()]
    city = parts[0] if parts else location.strip()
    state = ""
    if len(parts) > 1:
        state = parts[1].split()[0].upper()
    return city, state


def normalize_base_url(site: str, scheme: str) -> str:
    site = site.strip()
    scheme = (scheme or "https").strip().lower()
    if not site:
        return f"{scheme}://boston.craigslist.org"
    if site.startswith("http://") or site.startswith("https://"):
        return site.rstrip("/")
    return f"{scheme}://{site.strip('/')}"


def resolve_base_site(location: str, override: Optional[str], scheme: str) -> str:
    if override:
        return normalize_base_url(override, scheme)
    city, state = parse_location(location)
    if city.lower() in CITY_SITE_OVERRIDES:
        return normalize_base_url(CITY_SITE_OVERRIDES[city.lower()], scheme)
    if state in STATE_SITE_DEFAULTS:
        return normalize_base_url(STATE_SITE_DEFAULTS[state], scheme)
    return f"{scheme}://boston.craigslist.org"


class RateLimiter:
    def __init__(self, min_delay: float, max_delay: float, jitter: float) -> None:
        self._min_delay = min_delay
        self._max_delay = max_delay
        self._jitter = jitter
        self._next_allowed: Dict[str, float] = {}

    def wait(self, host: str) -> None:
        now = time.time()
        target = self._next_allowed.get(host, now)
        if now < target:
            delay = target - now
            log("info", "rate_limit_sleep", host=host, delay_s=round(delay, 2))
            time.sleep(delay)

    def mark_request(self, host: str) -> None:
        delay = self._min_delay * (1 + random.random() * self._jitter)
        self._next_allowed[host] = time.time() + delay

    def backoff(self, host: str, attempt: int, retry_after: Optional[float] = None) -> float:
        if retry_after is not None:
            delay = retry_after
        else:
            delay = min(self._max_delay, self._min_delay * (2 ** (attempt - 1)))
        delay = delay * (1 + random.random() * self._jitter)
        self._next_allowed[host] = time.time() + delay
        return delay


class UserAgentPool:
    def __init__(self, agents: List[str]) -> None:
        self._agents = agents[:]
        self._pos = 0

    def next(self) -> str:
        if not self._agents:
            return DEFAULT_USER_AGENTS[0]
        agent = self._agents[self._pos % len(self._agents)]
        self._pos += 1
        return agent


@dataclass
class HttpResponse:
    ok: bool
    status: Optional[int]
    url: str
    text: str
    headers: Dict[str, str]
    duration_ms: Optional[int]
    error: Optional[str]


class Fetcher:
    def __init__(
        self,
        timeout: float,
        max_retries: int,
        limiter: RateLimiter,
        user_agents: List[str],
        log_html: bool,
        log_html_on_error: bool,
        client_mode: str,
        connect_timeout: Optional[float],
        read_timeout: Optional[float],
    ) -> None:
        self._timeout = timeout
        self._max_retries = max_retries
        self._limiter = limiter
        self._ua_pool = UserAgentPool(user_agents)
        self._log_html = log_html
        self._log_html_on_error = log_html_on_error
        self._client_mode = (client_mode or "auto").lower()
        self._connect_timeout = connect_timeout or timeout
        self._read_timeout = read_timeout or timeout
        self._client = None

        if self._client_mode not in ("auto", "httpx", "urllib"):
            log("warn", "unknown_client_mode", client_mode=self._client_mode)
            self._client_mode = "auto"

        if self._client_mode in ("auto", "httpx") and httpx is not None:
            timeout_cfg = httpx.Timeout(
                connect=self._connect_timeout,
                read=self._read_timeout,
                write=self._read_timeout,
                pool=self._connect_timeout,
            )
            self._client = httpx.Client(timeout=timeout_cfg, follow_redirects=True)
        elif self._client_mode == "httpx" and httpx is None:
            log("warn", "httpx_unavailable_fallback", client_mode=self._client_mode)

    def close(self) -> None:
        if self._client:
            self._client.close()

    def get(self, url: str, params: Optional[Dict[str, Any]] = None, allow_retry: bool = True) -> HttpResponse:
        full_url = build_url(url, params)
        host = urlparse(full_url).netloc

        methods: List[str] = []
        if self._client_mode in ("auto", "httpx") and self._client is not None:
            methods.append("httpx")
        if self._client_mode in ("auto", "urllib") or self._client is None:
            methods.append("urllib")
        if not methods:
            methods = ["urllib"]

        for attempt in range(1, self._max_retries + 1):
            self._limiter.wait(host)
            user_agent = self._ua_pool.next()
            headers = {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
            }

            last_error = None
            for idx, method in enumerate(methods):
                log(
                    "info",
                    "http_request",
                    url=full_url,
                    attempt=attempt,
                    user_agent=user_agent,
                    client=method,
                )
                start = time.perf_counter()

                try:
                    if method == "httpx" and self._client is not None:
                        resp = self._client.get(full_url, headers=headers)
                        status = resp.status_code
                        body = resp.text
                        headers_out = dict(resp.headers)
                        final_url = str(resp.url)
                    else:
                        req = urlrequest.Request(full_url, headers=headers)
                        with urlrequest.urlopen(req, timeout=self._timeout) as resp:
                            status = resp.getcode()
                            raw = resp.read()
                            encoding = resp.headers.get_content_charset() or "utf-8"
                            body = raw.decode(encoding, errors="replace")
                            headers_out = dict(resp.headers)
                            final_url = resp.geturl()

                    duration_ms = int((time.perf_counter() - start) * 1000)
                    log(
                        "info",
                        "http_response",
                        url=full_url,
                        status=status,
                        duration_ms=duration_ms,
                        size=len(body),
                        client=method,
                    )

                    if self._log_html:
                        log("debug", "http_body", url=full_url, body=body, client=method)
                    elif status != 200 and self._log_html_on_error:
                        log(
                            "debug",
                            "http_body_snippet",
                            url=full_url,
                            status=status,
                            body_snippet=body[:2000],
                            client=method,
                        )

                    should_retry = status in RETRY_STATUSES and allow_retry and attempt < self._max_retries
                    if should_retry:
                        retry_after = parse_retry_after(headers_out.get("Retry-After"))
                        delay = self._limiter.backoff(host, attempt, retry_after=retry_after)
                        log(
                            "warn",
                            "http_retry",
                            url=full_url,
                            status=status,
                            attempt=attempt,
                            delay_s=round(delay, 2),
                            client=method,
                        )
                        time.sleep(delay)
                        break

                    self._limiter.mark_request(host)
                    return HttpResponse(
                        ok=status == 200,
                        status=status,
                        url=final_url,
                        text=body,
                        headers=headers_out,
                        duration_ms=duration_ms,
                        error=None,
                    )

                except Exception as exc:
                    duration_ms = int((time.perf_counter() - start) * 1000)
                    last_error = f"{type(exc).__name__}: {exc}"
                    log(
                        "error",
                        "http_exception",
                        url=full_url,
                        attempt=attempt,
                        duration_ms=duration_ms,
                        error=last_error,
                        client=method,
                    )

                    if idx < len(methods) - 1:
                        log("warn", "http_client_fallback", from_client=method, to_client=methods[idx + 1])
                        continue

            if last_error and allow_retry and attempt < self._max_retries:
                delay = self._limiter.backoff(host, attempt)
                log(
                    "warn",
                    "http_retry_exception",
                    url=full_url,
                    attempt=attempt,
                    delay_s=round(delay, 2),
                    error=last_error,
                )
                time.sleep(delay)
                continue

            if last_error:
                return HttpResponse(
                    ok=False,
                    status=None,
                    url=full_url,
                    text="",
                    headers={},
                    duration_ms=None,
                    error=last_error,
                )

        return HttpResponse(
            ok=False,
            status=None,
            url=full_url,
            text="",
            headers={},
            duration_ms=None,
            error="max_retries_exhausted",
        )


class SearchResultsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: List[Dict[str, Any]] = []
        self._current: Optional[Dict[str, Any]] = None
        self._capture_title = False
        self._capture_price = False
        self._capture_hood = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attrs_dict = {k: v for k, v in attrs}

        if tag == "li" and "class" in attrs_dict and "result-row" in (attrs_dict.get("class") or ""):
            self._current = {
                "post_id": attrs_dict.get("data-pid"),
                "repost_of": attrs_dict.get("data-repost-of"),
                "latitude": attrs_dict.get("data-latitude"),
                "longitude": attrs_dict.get("data-longitude"),
                "price": attrs_dict.get("data-price"),
            }
            return

        if self._current is None:
            return

        if tag == "a":
            if "class" in attrs_dict and "result-title" in (attrs_dict.get("class") or ""):
                self._current["url"] = attrs_dict.get("href")
                self._capture_title = True
        elif tag == "span":
            cls = attrs_dict.get("class") or ""
            if "result-price" in cls:
                self._capture_price = True
            elif "result-hood" in cls:
                self._capture_hood = True
        elif tag == "time":
            dt_value = attrs_dict.get("datetime")
            if dt_value:
                self._current["post_datetime"] = dt_value

    def handle_endtag(self, tag: str) -> None:
        if tag == "li" and self._current is not None:
            self.results.append(self._current)
            self._current = None
            return

        if tag == "a" and self._capture_title:
            self._capture_title = False
        elif tag == "span" and self._capture_price:
            self._capture_price = False
        elif tag == "span" and self._capture_hood:
            self._capture_hood = False

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return
        if self._capture_title:
            self._current["title"] = (self._current.get("title") or "") + data
        if self._capture_price:
            self._current["price_text"] = (self._current.get("price_text") or "") + data
        if self._capture_hood:
            self._current["hood"] = (self._current.get("hood") or "") + data


def parse_search_results(html: str) -> List[Dict[str, Any]]:
    parser = SearchResultsParser()
    try:
        parser.feed(html)
    except Exception as exc:
        log("error", "search_parse_failed", error=str(exc), body_snippet=html[:2000])
        return []

    results: List[Dict[str, Any]] = []
    for item in parser.results:
        item["title"] = clean_text(item.get("title"))
        item["hood"] = clean_text(item.get("hood"))
        price = item.get("price") or item.get("price_text")
        item["price"] = to_int(price)
        item["latitude"] = to_float(item.get("latitude"))
        item["longitude"] = to_float(item.get("longitude"))
        item["post_datetime"] = format_datetime(parse_datetime(item.get("post_datetime")))
        results.append(item)

    return results


def extract_price_from_text(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    match = re.search(r"\$\s*([0-9,]+)", text)
    if match:
        return to_int(match.group(1))
    return None


def parse_beds_baths_sqft_from_text(text: Optional[str]) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    if not text:
        return None, None, None
    beds = None
    baths = None
    sqft = None
    match = re.search(r"(\d+(?:\.\d+)?)\s*br", text, re.I)
    if match:
        beds = to_int(match.group(1))
    match = re.search(r"(\d+(?:\.\d+)?)\s*ba", text, re.I)
    if match:
        baths = to_int(match.group(1))
    match = re.search(r"(\d{2,})\s*ft", text, re.I)
    if match:
        sqft = to_int(match.group(1))
    return beds, baths, sqft


def parse_rss_results(xml: str) -> List[Dict[str, Any]]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        log("error", "rss_parse_failed", error=str(exc), body_snippet=xml[:2000])
        return []

    results: List[Dict[str, Any]] = []
    for item in root.findall(".//item"):
        title = clean_text(item.findtext("title"))
        link = clean_text(item.findtext("link"))
        guid = clean_text(item.findtext("guid"))
        desc_raw = item.findtext("description") or ""
        description = clean_text(strip_tags(desc_raw))
        pub_date = item.findtext("pubDate")

        post_dt = None
        if pub_date:
            try:
                post_dt = parsedate_to_datetime(pub_date)
            except Exception:
                post_dt = None

        lat = None
        lon = None
        for child in list(item):
            tag = child.tag.lower()
            text = (child.text or "").strip()
            if not text:
                continue
            if tag.endswith("point") and " " in text:
                parts = text.split()
                if len(parts) >= 2:
                    lat = to_float(parts[0])
                    lon = to_float(parts[1])
            elif tag.endswith("lat"):
                lat = to_float(text)
            elif tag.endswith("long") or tag.endswith("lon"):
                lon = to_float(text)

        price = extract_price_from_text(title) or extract_price_from_text(description)
        beds, baths, sqft = parse_beds_baths_sqft_from_text(description or title)

        results.append(
            {
                "post_id": extract_post_id_from_url(link) or extract_post_id_from_url(guid),
                "url": link or guid,
                "title": title,
                "description": description,
                "price": price,
                "latitude": lat,
                "longitude": lon,
                "post_datetime": format_datetime(post_dt),
                "beds": beds,
                "baths": baths,
                "sqft": sqft,
            }
        )

    return results


def extract_total_count(html: str) -> Optional[int]:
    match = re.search(r'class="totalcount">(\d+)<', html)
    if not match:
        match = re.search(r"search-count[^>]*>[^<]*(\d+)", html, re.S)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def extract_first(pattern: str, html: str) -> Optional[str]:
    match = re.search(pattern, html, re.S | re.I)
    return match.group(1) if match else None


def extract_title(html: str) -> Optional[str]:
    title = extract_first(r'id="titletextonly"[^>]*>(.*?)</span>', html)
    if not title:
        title = extract_first(r"<title>(.*?)</title>", html)
    return clean_text(title)


def extract_title_location(html: str) -> Optional[str]:
    return clean_text(extract_first(r"<small>\s*\(([^)]+)\)\s*</small>", html))


def extract_price(html: str) -> Optional[int]:
    return to_int(extract_first(r'class="price"\s*>\s*\$?([\d,]+)', html))


def extract_mapaddress(html: str) -> Optional[str]:
    raw = extract_first(r'class="mapaddress"[^>]*>(.*?)</div>', html)
    if not raw:
        return None
    return clean_text(strip_tags(raw))


def extract_geo(html: str) -> Tuple[Optional[float], Optional[float]]:
    lat = extract_first(r'data-latitude="([^"]+)"', html)
    lon = extract_first(r'data-longitude="([^"]+)"', html)
    if not lat or not lon:
        geo = extract_first(r'name="geo\.position"\s*content="([^"]+)"', html)
        if geo and ";" in geo:
            parts = [part.strip() for part in geo.split(";", 1)]
            if len(parts) == 2:
                lat, lon = parts
    return to_float(lat), to_float(lon)


def extract_attrgroup_spans(html: str) -> List[str]:
    spans: List[str] = []
    for group in re.findall(r'<p class="attrgroup">(.*?)</p>', html, re.S | re.I):
        for span in re.findall(r"<span[^>]*>(.*?)</span>", group, re.S | re.I):
            text = clean_text(strip_tags(span))
            if text:
                spans.append(text)
    return spans


def guess_style(attr: str) -> Optional[str]:
    lowered = attr.lower()
    for key in (
        "apartment",
        "condo",
        "house",
        "townhouse",
        "duplex",
        "loft",
        "studio",
        "in-law",
    ):
        if key in lowered:
            return key
    return None


def parse_attr_fields(attrs: List[str]) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[str]]:
    beds = None
    baths = None
    sqft = None
    style = None

    for attr in attrs:
        if beds is None:
            match = re.search(r"(\d+(?:\.\d+)?)\s*br", attr, re.I)
            if match:
                beds = to_int(match.group(1))
        if baths is None:
            match = re.search(r"(\d+(?:\.\d+)?)\s*ba", attr, re.I)
            if match:
                baths = to_int(match.group(1))
        if sqft is None:
            match = re.search(r"(\d{2,})\s*ft", attr, re.I)
            if match:
                sqft = to_int(match.group(1))
        if style is None:
            style = guess_style(attr)

    return beds, baths, sqft, style


def extract_posting_id(html: str, url: str) -> Optional[str]:
    pid = extract_first(r'data-posting-id="(\d+)"', html)
    if pid:
        return pid
    pid = extract_first(r"postingid[^0-9]*(\d+)", html)
    if pid:
        return pid
    pid = extract_first(r"/(\d{5,})\.html", url)
    return pid


def extract_post_id_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    match = re.search(r"/(\d{5,})\.html", url)
    if match:
        return match.group(1)
    match = re.search(r"(\d{5,})", url)
    return match.group(1) if match else None


def extract_posted_datetime(html: str) -> Optional[datetime]:
    for pattern in (
        r'id="display-date"[^>]*>.*?datetime="([^"]+)"',
        r'id="posted-date"[^>]*>.*?datetime="([^"]+)"',
        r"<time[^>]*datetime=\"([^\"]+)\"",
    ):
        value = extract_first(pattern, html)
        if value:
            parsed = parse_datetime(value)
            if parsed:
                return parsed
    return None


def extract_description(html: str) -> Optional[str]:
    body = extract_first(r'<section[^>]+id="postingbody"[^>]*>(.*?)</section>', html)
    if not body:
        return None
    body = body.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    body = unescape(strip_tags(body))
    body = body.replace("QR Code Link to This Post", "")
    return normalize_whitespace(body)


def extract_images(html: str) -> List[str]:
    images: List[str] = []
    og_image = extract_first(r'property="og:image"\s*content="([^"]+)"', html)
    if og_image:
        images.append(og_image)
    for url in re.findall(r'(?:data-lazy|src)="(https?://images\.craigslist\.org/[^"]+)"', html):
        if url not in images:
            images.append(url)
    return images


def split_street_unit(street: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not street:
        return None, None
    match = re.search(r"^(.*?)(?:\s+(?:#|apt\.?|unit)\s*([A-Za-z0-9\-]+))$", street, re.I)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return street, None


def parse_city_state_zip(address: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    if not address:
        return None, None, None
    zip_code = None
    match_zip = re.search(r"\b(\d{5}(?:-\d{4})?)\b", address)
    if match_zip:
        zip_code = match_zip.group(1)
    match_city = re.search(r",\s*([^,]+?),\s*([A-Z]{2})\b", address)
    if match_city:
        return match_city.group(1).strip(), match_city.group(2).strip(), zip_code
    return None, None, zip_code


def parse_city_from_title_location(location_text: Optional[str]) -> Optional[str]:
    if not location_text:
        return None
    cleaned = location_text.replace("/", ",")
    return cleaned.split(",")[0].strip() if cleaned else None


def parse_detail_page(html: str, url: str) -> Dict[str, Any]:
    try:
        title = extract_title(html)
        title_location = extract_title_location(html)
        price = extract_price(html)
        address = extract_mapaddress(html)
        lat, lon = extract_geo(html)
        attrs = extract_attrgroup_spans(html)
        beds, baths, sqft, style = parse_attr_fields(attrs)
        post_dt = extract_posted_datetime(html)
        description = extract_description(html)
        images = extract_images(html)
        primary_photo = images[0] if images else None
        posting_id = extract_posting_id(html, url)
        city, state, zip_code = parse_city_state_zip(address)
        street, unit = split_street_unit(address)

        return {
            "posting_id": posting_id,
            "url": url,
            "title": title,
            "title_location": title_location,
            "price": price,
            "street": street,
            "unit": unit,
            "city": city,
            "state": state,
            "zip_code": zip_code,
            "latitude": lat,
            "longitude": lon,
            "beds": beds,
            "baths": baths,
            "sqft": sqft,
            "style": style,
            "list_date": post_dt,
            "description": description,
            "images": images,
            "primary_photo": primary_photo,
            "mls_id": None,
            "mls_status": "active",
            "agent_name": None,
            "raw_attrs": attrs,
        }
    except Exception as exc:
        log_exception("detail_parse_failed", exc)
        return {"url": url, "parse_error": str(exc)}


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (int, float)) and value == 0:
        return True
    return False


def find_missing_required(record: Dict[str, Any]) -> List[str]:
    missing = []
    for field in REQUIRED_FIELDS:
        if is_missing(record.get(field)):
            missing.append(field)
    return missing


def build_record(location: str, search: Dict[str, Any], detail: Dict[str, Any]) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    location_city, _state = parse_location(location)

    list_date_dt = detail.get("list_date")
    if list_date_dt is None:
        list_date_dt = parse_datetime(search.get("post_datetime"))

    list_date = format_datetime(list_date_dt)
    days_on_mls = days_since(list_date_dt, now)

    title_city = parse_city_from_title_location(detail.get("title_location"))
    city = detail.get("city") or title_city or location_city

    record = {
        "property_id": detail.get("posting_id") or search.get("post_id"),
        "property_url": detail.get("url") or search.get("url"),
        "list_price": detail.get("price") or search.get("price"),
        "beds": detail.get("beds") if detail.get("beds") is not None else search.get("beds"),
        "full_baths": detail.get("baths") if detail.get("baths") is not None else search.get("baths"),
        "sqft": detail.get("sqft") if detail.get("sqft") is not None else search.get("sqft"),
        "street": detail.get("street"),
        "unit": detail.get("unit"),
        "city": city,
        "zip_code": detail.get("zip_code"),
        "latitude": detail.get("latitude")
        if detail.get("latitude") is not None
        else search.get("latitude"),
        "longitude": detail.get("longitude")
        if detail.get("longitude") is not None
        else search.get("longitude"),
        "text": detail.get("description"),
        "primary_photo": detail.get("primary_photo"),
        "mls_id": detail.get("mls_id"),
        "mls_status": detail.get("mls_status"),
        "days_on_mls": days_on_mls,
        "agent_name": detail.get("agent_name"),
        "style": detail.get("style"),
        "list_date": list_date,
        "source": "craigslist",
    }

    return record


def fetch_robots(fetcher: Fetcher, base_url: str) -> Optional[RobotFileParser]:
    url = f"{base_url.rstrip('/')}/robots.txt"
    resp = fetcher.get(url, allow_retry=False)
    if not resp.ok:
        log("warn", "robots_fetch_failed", url=url, status=resp.status, error=resp.error)
        return None
    parser = RobotFileParser()
    parser.parse(resp.text.splitlines())
    log("info", "robots_loaded", url=url)
    return parser


def crawl_location(
    fetcher: Fetcher,
    location: str,
    args: argparse.Namespace,
    bbox: Optional[Dict[str, float]],
    seen_ids: set,
    limit: Optional[int],
) -> Iterable[Dict[str, Any]]:
    city, state = parse_location(location)
    base_url = resolve_base_site(location, args.site, args.scheme)
    search_url = f"{base_url}/search/{args.category}"
    log(
        "info",
        "location_start",
        location=location,
        base_url=base_url,
        search_url=search_url,
        city=city,
        state=state,
    )

    robots = fetch_robots(fetcher, base_url) if args.check_robots else None

    emitted = 0
    for page in range(args.max_pages):
        offset = page * args.page_size
        params = {
            "query": city,
            "sort": "date",
            "s": offset,
            "bundleDuplicates": 1,
            "min_price": args.min_price,
            "max_price": args.max_price,
        }

        page_url = build_url(search_url, params)
        log("info", "search_page_start", location=location, page=page + 1, offset=offset, url=page_url)

        if robots and not robots.can_fetch("*", page_url):
            log("warn", "robots_disallowed", url=page_url)
            break

        results: List[Dict[str, Any]] = []
        if args.use_rss:
            rss_params = dict(params)
            rss_params["format"] = "rss"
            rss_url = build_url(search_url, rss_params)
            log("info", "rss_page_start", location=location, page=page + 1, url=rss_url)
            resp = fetcher.get(search_url, params=rss_params)
            if not resp.ok:
                log(
                    "error",
                    "rss_page_failed",
                    location=location,
                    status=resp.status,
                    error=resp.error,
                )
                break
            results = parse_rss_results(resp.text)
            log(
                "info",
                "search_page_parsed",
                location=location,
                page=page + 1,
                results=len(results),
                source="rss",
            )
        else:
            resp = fetcher.get(search_url, params=params)
            if not resp.ok:
                log(
                    "error",
                    "search_page_failed",
                    location=location,
                    status=resp.status,
                    error=resp.error,
                )
                if args.rss_fallback:
                    rss_params = dict(params)
                    rss_params["format"] = "rss"
                    rss_url = build_url(search_url, rss_params)
                    log("warn", "search_fallback_rss", location=location, url=rss_url)
                    rss_resp = fetcher.get(search_url, params=rss_params)
                    if not rss_resp.ok:
                        log(
                            "error",
                            "rss_page_failed",
                            location=location,
                            status=rss_resp.status,
                            error=rss_resp.error,
                        )
                        break
                    results = parse_rss_results(rss_resp.text)
                    log(
                        "info",
                        "search_page_parsed",
                        location=location,
                        page=page + 1,
                        results=len(results),
                        source="rss_fallback",
                    )
                else:
                    break
            else:
                total_count = extract_total_count(resp.text)
                if total_count is not None:
                    log("info", "search_total_count", location=location, total=total_count)

                results = parse_search_results(resp.text)
                log(
                    "info",
                    "search_page_parsed",
                    location=location,
                    page=page + 1,
                    results=len(results),
                    source="html",
                )

        if not results:
            break

        for item in results:
            candidate_id = item.get("post_id") or item.get("url")
            if not candidate_id:
                log("warn", "search_missing_id", location=location, item=item)
                continue
            if candidate_id in seen_ids:
                log("info", "duplicate_listing_skipped", location=location, post_id=candidate_id)
                continue
            seen_ids.add(candidate_id)

            detail: Dict[str, Any] = {}
            listing_url = item.get("url")
            if args.no_detail:
                detail = {}
            elif not listing_url:
                log("warn", "missing_listing_url", location=location, item=item)
            elif robots and not robots.can_fetch("*", listing_url):
                log("warn", "robots_disallowed", url=listing_url)
            else:
                detail_resp = fetcher.get(listing_url)
                if not detail_resp.ok:
                    log(
                        "error",
                        "detail_failed",
                        url=listing_url,
                        status=detail_resp.status,
                        error=detail_resp.error,
                    )
                else:
                    detail = parse_detail_page(detail_resp.text, detail_resp.url)

            record = build_record(location, item, detail)
            missing_required = find_missing_required(record)
            bbox_ok = in_bbox(record.get("latitude"), record.get("longitude"), bbox)
            log(
                "info",
                "listing_record",
                location=location,
                record=record,
                missing_required=missing_required,
                bbox_ok=bbox_ok,
                search_snapshot=item,
                detail_snapshot=detail,
            )

            yield record
            emitted += 1
            if limit is not None and emitted >= limit:
                log("info", "location_limit_reached", location=location, limit=limit)
                return

        if not args.use_rss and len(results) < args.page_size:
            break

    log("info", "location_complete", location=location, emitted=emitted)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Craigslist rentals scraper (log-everything mode).")
    parser.add_argument(
        "--location",
        action="append",
        default=None,
        help="Override locations from listings.yml (repeatable).",
    )
    parser.add_argument(
        "--site",
        type=str,
        default=None,
        help="Override Craigslist base site, e.g. https://boston.craigslist.org",
    )
    parser.add_argument(
        "--category",
        type=str,
        default=DEFAULT_CATEGORY,
        help="Craigslist category (default: apa).",
    )
    parser.add_argument("--limit", type=int, default=None, help="Max listings to emit (total).")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, help="Pages per location.")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="Search page size.")
    parser.add_argument("--min-price", type=int, default=None, help="Minimum rent price filter.")
    parser.add_argument("--max-price", type=int, default=None, help="Maximum rent price filter.")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds.")
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=None,
        help="HTTP connect timeout in seconds (overrides --timeout for connects).",
    )
    parser.add_argument(
        "--read-timeout",
        type=float,
        default=None,
        help="HTTP read timeout in seconds (overrides --timeout for reads).",
    )
    parser.add_argument("--min-delay", type=float, default=DEFAULT_MIN_DELAY, help="Min delay between requests.")
    parser.add_argument("--max-delay", type=float, default=DEFAULT_MAX_DELAY, help="Max backoff delay.")
    parser.add_argument("--jitter", type=float, default=DEFAULT_JITTER, help="Random jitter multiplier.")
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES, help="Max retry attempts.")
    parser.add_argument(
        "--client",
        type=str,
        default=DEFAULT_CLIENT_MODE,
        help="HTTP client mode: auto, httpx, or urllib.",
    )
    parser.add_argument(
        "--scheme",
        type=str,
        default=DEFAULT_SCHEME,
        help="URL scheme to use when no scheme is provided: https or http.",
    )
    parser.add_argument("--use-rss", action="store_true", help="Use Craigslist RSS feed for search.")
    parser.add_argument(
        "--no-rss-fallback",
        action="store_false",
        dest="rss_fallback",
        help="Disable RSS fallback when HTML search fails.",
    )
    parser.set_defaults(rss_fallback=DEFAULT_RSS_FALLBACK)
    parser.add_argument("--check-robots", action="store_true", help="Fetch and honor robots.txt.")
    parser.add_argument("--log-html", action="store_true", help="Log full HTML bodies (very verbose).")
    parser.add_argument(
        "--no-log-html-on-error",
        action="store_false",
        dest="log_html_on_error",
        help="Disable HTML snippet logging for non-200 responses.",
    )
    parser.set_defaults(log_html_on_error=True)
    parser.add_argument("--no-detail", action="store_true", help="Skip detail page fetches.")
    return parser


def main() -> int:
    global RUN_ID
    RUN_ID = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ-") + str(random.randint(1000, 9999))

    parser = build_argument_parser()
    args = parser.parse_args()

    args.scheme = (args.scheme or DEFAULT_SCHEME).lower()

    log("info", "run_start", args=vars(args), python_version=sys.version)
    log("info", "library_check", httpx_available=httpx is not None, yaml_available=yaml is not None)
    log("info", "schema_fields", fields=SCHEMA_FIELDS, required=REQUIRED_FIELDS)

    locations = load_locations(LISTINGS_CONFIG, args.location)
    bbox = load_bbox(SPATIAL_CONFIG)
    log("info", "config_loaded", locations=locations, bbox=bbox, listings_config=str(LISTINGS_CONFIG))

    limiter = RateLimiter(args.min_delay, args.max_delay, args.jitter)
    fetcher = Fetcher(
        args.timeout,
        args.max_retries,
        limiter,
        DEFAULT_USER_AGENTS,
        args.log_html,
        args.log_html_on_error,
        args.client,
        args.connect_timeout,
        args.read_timeout,
    )

    total_emitted = 0
    seen_ids: set = set()

    try:
        for location in locations:
            remaining = None
            if args.limit is not None:
                remaining = max(0, args.limit - total_emitted)
                if remaining == 0:
                    break

            for _record in crawl_location(fetcher, location, args, bbox, seen_ids, remaining):
                total_emitted += 1
                if args.limit is not None and total_emitted >= args.limit:
                    break
            if args.limit is not None and total_emitted >= args.limit:
                break

        log("info", "run_complete", emitted=total_emitted, locations=len(locations))
        return 0
    except Exception as exc:
        log_exception("run_failed", exc)
        return 1
    finally:
        fetcher.close()


if __name__ == "__main__":
    raise SystemExit(main())
