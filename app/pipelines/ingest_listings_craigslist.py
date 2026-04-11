"""Craigslist rental listings pipeline (fallback).

Scrapes Boston-area Craigslist apartments via Scrapling's StealthyFetcher,
validates, deduplicates, loads to RAW.LISTINGS via staging + MERGE.
Runs as fallback when HomeHarvest returns insufficient results.

Usage:
    python -m app.pipelines.ingest_listings_craigslist --mode full --limit 50 --dry-run
    python -m app.pipelines.ingest_listings_craigslist --mode full
    python -m app.pipelines.ingest_listings_craigslist --mode incremental
    python -m app.pipelines.ingest_listings_craigslist --min-price 1500 --max-price 3500
    python -m app.pipelines.ingest_listings_craigslist --no-headless --limit 5
"""

import re
import json
import time
import hashlib
import argparse
from typing import Optional

import structlog

from app.core.base_pipeline import BasePipeline, PipelineRunResult
from app.core.config_loader import load_source_config, load_spatial
from app.core.validator import RecordValidator

logger = structlog.get_logger()

try:
    from scrapling.fetchers import StealthyFetcher
except ImportError:
    raise RuntimeError(
        "scrapling[fetchers] not installed. "
        "Run: pip install 'scrapling[fetchers]' && scrapling install"
    )


def _listing_id(source: str, source_native_id: str) -> str:
    """Deterministic listing ID from source + native ID."""
    return hashlib.sha256(f"{source}:{source_native_id}".encode()).hexdigest()[:16]


def _normalize_timestamp(ts: str | None) -> str | None:
    """Fix Craigslist timezone offset for Snowflake: -0400 → -04:00."""
    if not ts:
        return None
    m = re.search(r'([+-]\d{2})(\d{2})$', ts)
    if m:
        return ts[:-len(m.group(0))] + f"{m.group(1)}:{m.group(2)}"
    return ts


# ── Extractor ────────────────────────────────────────────────

class CraigslistExtractor:
    """Scrapes Craigslist apartment listings via StealthyFetcher.

    Flow: fetch search page → extract URLs → fetch individual listings → parse.
    Anti-bot bypass via Scrapling's stealth browser (Playwright under the hood).
    Uses headless Chromium with TLS fingerprint spoofing, randomized headers,
    and network_idle detection for dynamic content.
    """

    def __init__(self, config: dict):
        cl = config["craigslist"]
        self._domain = cl["domain"]
        self._search_path = cl["search_path"]
        self._default_params = dict(cl.get("default_params", {}))
        self._locations = cl.get("locations", [])
        self._max_listings = cl["max_listings_per_run"]
        self._delay = cl["delay_between_fetches"]
        self._headless = cl["headless"]
        self._max_retries = cl.get("max_retries", 3)
        self._backoff_base = cl.get("backoff_base", 2.0)
        self._backoff_max = cl.get("backoff_max", 30.0)
        self._log = logger.bind(extractor="craigslist")

    def extract(self, limit: int = None, min_price: int = None,
                max_price: int = None, existing_ids: set = None) -> list[dict]:
        """Scrape listings from Craigslist search results.

        Args:
            limit: Max listings to fetch. Overrides config max_listings_per_run.
            min_price: Override minimum price search filter.
            max_price: Override maximum price search filter.
            existing_ids: Posting IDs already in Snowflake (skipped).

        Returns:
            List of parsed listing dicts.
        """
        urls = self._fetch_all_search_urls(min_price, max_price)

        if not urls:
            self._log.warning("no_search_results")
            return []

        # Skip already-ingested listings (incremental optimization)
        if existing_ids:
            before = len(urls)
            urls = [u for u in urls if self._extract_posting_id(u) not in existing_ids]
            self._log.info("incremental_filter",
                           before=before, after=len(urls),
                           skipped=before - len(urls))

        # Cap at limit or config max
        cap = min(limit or self._max_listings, self._max_listings)
        if len(urls) > cap:
            urls = urls[:cap]
            self._log.info("capped_at_limit", limit=cap)

        # Fetch + parse each listing
        listings = []
        for i, url in enumerate(urls):
            if i > 0:
                time.sleep(self._delay)

            listing = self._fetch_and_parse_listing(url)
            if listing:
                listings.append(listing)

            if (i + 1) % 25 == 0:
                self._log.info("progress",
                               fetched=i + 1, total=len(urls),
                               parsed=len(listings))

        self._log.info("extraction_complete",
                       urls_found=len(urls), listings_parsed=len(listings))
        return listings

    # ── Search pages ─────────────────────────────────────────

    def _fetch_all_search_urls(self, min_price: int = None,
                               max_price: int = None) -> list[str]:
        """Fetch search pages for all configured locations, deduplicate."""
        all_urls: dict[str, None] = {}  # ordered set

        if not self._locations:
            # No locations configured — single broad search
            urls = self._fetch_search_urls({}, min_price, max_price)
            all_urls.update(dict.fromkeys(urls))
        else:
            for loc in self._locations:
                loc_params = {
                    "postal": loc["postal"],
                    "search_distance": loc.get("search_distance", 5),
                }
                urls = self._fetch_search_urls(
                    loc_params, min_price, max_price)
                new = [u for u in urls if u not in all_urls]
                all_urls.update(dict.fromkeys(new))

                self._log.info("location_searched",
                               name=loc["name"], found=len(urls),
                               new=len(new), total=len(all_urls))

                if len(self._locations) > 1:
                    time.sleep(self._delay)

        self._log.info("search_complete",
                       total_unique_urls=len(all_urls))
        return list(all_urls)

    def _fetch_search_urls(self, loc_params: dict,
                           min_price: int = None,
                           max_price: int = None) -> list[str]:
        """Fetch one search page and extract unique listing URLs."""
        params = dict(self._default_params)
        params.update(loc_params)
        if min_price is not None:
            params["min_price"] = min_price
        if max_price is not None:
            params["max_price"] = max_price

        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"https://{self._domain}{self._search_path}?{query}"

        html = self._fetch_html(url)
        if not html:
            return []

        # Extract listing URLs, deduplicate preserving order
        pattern = rf'href="(https://{re.escape(self._domain)}/[^"]+/\d+\.html)"'
        urls = list(dict.fromkeys(re.findall(pattern, html)))
        return urls

    # ── Individual listing ───────────────────────────────────

    def _fetch_and_parse_listing(self, url: str) -> dict | None:
        """Fetch a single listing page and extract structured data."""
        html = self._fetch_html(url)
        if not html:
            return None
        return self._parse_listing(url, html)

    def _parse_listing(self, url: str, html: str) -> dict | None:
        """Extract fields from Craigslist listing HTML."""
        posting_id = self._extract_posting_id(url)
        if not posting_id:
            return None

        # Price
        m = re.search(r'class="price"[^>]*>\$?([\d,]+)', html)
        price = int(m.group(1).replace(",", "")) if m else None

        # Title
        m = re.search(
            r'<span[^>]*id="titletextonly"[^>]*>(.*?)</span>', html, re.DOTALL)
        title = re.sub(r'<[^>]+>', '', m.group(1)).strip() if m else None

        # Description
        m = re.search(
            r'<section[^>]*id="postingbody"[^>]*>(.*?)</section>',
            html, re.DOTALL)
        description = None
        if m:
            description = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            description = re.sub(r'\s+', ' ', description)

        # Coordinates (try JSON-LD first, then data attributes)
        lat = self._extract_coord(
            re.search(r'"latitude"\s*[=:]\s*"?([\d.-]+)', html)
            or re.search(r'data-latitude="([\d.-]+)"', html)
        )
        lon = self._extract_coord(
            re.search(r'"longitude"\s*[=:]\s*"?([\d.-]+)', html)
            or re.search(r'data-longitude="([\d.-]+)"', html)
        )

        # Address
        m = re.search(
            r'<div[^>]*class="mapaddress"[^>]*>(.*?)</div>', html, re.DOTALL)
        address = re.sub(r'<[^>]+>', '', m.group(1)).strip() if m else None

        # Posted date (ISO 8601)
        m = re.search(r'<time[^>]*datetime="([^"]+)"', html)
        posted_date = m.group(1) if m else None

        # Images (deduplicated, ordered)
        images = list(dict.fromkeys(
            re.findall(r'"(https://images\.craigslist\.org/[^"]+)"', html)
        ))

        # Beds / baths / sqft from structured attrs, title, description
        beds, baths, sqft = self._parse_housing_attrs(html, title, description)

        return {
            "posting_id": posting_id,
            "url": url,
            "price": price,
            "title": title,
            "description": description,
            "lat": lat,
            "lon": lon,
            "address": address,
            "posted_date": posted_date,
            "images": images,
            "beds": beds,
            "baths": baths,
            "sqft": sqft,
        }

    # ── Attribute parsing ────────────────────────────────────

    @staticmethod
    def _parse_housing_attrs(html: str, title: str = None,
                             description: str = None) -> tuple:
        """Parse beds, baths, sqft from listing content.

        Priority: structured attrgroup > title > description.
        Returns (beds, baths, sqft) — any may be None.
        """
        # Collect text: attrgroup sections first (most reliable)
        attr_blocks = re.findall(
            r'<p[^>]*class="attrgroup"[^>]*>(.*?)</p>', html, re.DOTALL)
        attr_text = " ".join(re.sub(r'<[^>]+>', ' ', b) for b in attr_blocks)
        search_text = f"{attr_text} {title or ''} {description or ''}"

        # Beds: "2BR", "2br", "2 bed", "2 bedroom", "studio"
        beds = None
        m = re.search(r'\b(\d+)\s*(?:br|BR|Br|bed(?:room)?s?)\b', search_text)
        if m:
            beds = int(m.group(1))
        elif re.search(r'\bstudio\b', search_text, re.IGNORECASE):
            beds = 0

        # Baths: "1ba", "1BA", "1 bath", "1.5ba", "2 bathroom"
        baths = None
        m = re.search(
            r'\b(\d+(?:\.\d)?)\s*(?:ba(?:th(?:room)?s?)?)\b',
            search_text, re.IGNORECASE)
        if m:
            baths = int(float(m.group(1)))

        # Sqft: "800ft²", "800ft2", "800 sq ft", "800sqft"
        sqft = None
        m = re.search(
            r'\b(\d{3,5})\s*(?:ft²|ft2|sq\.?\s*ft|sqft)\b',
            search_text, re.IGNORECASE)
        if m:
            sqft = int(m.group(1))

        return beds, baths, sqft

    # ── Fetch helpers ────────────────────────────────────────

    def _fetch_html(self, url: str) -> str | None:
        """Fetch URL via StealthyFetcher with exponential backoff retry."""
        for attempt in range(1, self._max_retries + 1):
            try:
                page = StealthyFetcher.fetch(
                    url, headless=self._headless, network_idle=True,
                )
                html = (page.html_content
                        if hasattr(page, 'html_content') else str(page))

                # Guard against blocked/captcha pages
                if len(html) < 1000:
                    wait = min(self._backoff_base ** attempt, self._backoff_max)
                    self._log.warning("page_too_short",
                                      url=url[:80], chars=len(html),
                                      attempt=attempt, wait_s=wait)
                    if attempt < self._max_retries:
                        time.sleep(wait)
                        continue
                    return None

                return html

            except Exception as e:
                wait = min(self._backoff_base ** attempt, self._backoff_max)
                self._log.warning("fetch_failed",
                                  url=url[:80], attempt=attempt,
                                  error=str(e), wait_s=wait)
                if attempt < self._max_retries:
                    time.sleep(wait)

        self._log.error("fetch_exhausted", url=url[:80])
        return None

    @staticmethod
    def _extract_posting_id(url: str) -> str | None:
        m = re.search(r'/(\d+)\.html', url)
        return m.group(1) if m else None

    @staticmethod
    def _extract_coord(match) -> float | None:
        return float(match.group(1)) if match else None


# ── Pipeline ─────────────────────────────────────────────────

class CraigslistPipeline(BasePipeline):
    """Ingest Craigslist apartments as fallback to HomeHarvest.

    Writes to RAW.LISTINGS with source='craigslist'.
    No deactivation — Craigslist is supplementary, not authoritative.
    """

    SOURCE = "listings"
    DESCRIPTION = "Ingest Boston rental listings from Craigslist (fallback)"

    REQUIRED_FIELDS = ["posting_id", "price", "lat", "lon"]

    def __init__(self):
        super().__init__()
        self._config = load_source_config("listings_craigslist")
        self._source_name = self._config["dedup"]["source_name"]
        self._spatial = load_spatial()

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser):
        parser.add_argument(
            "--limit", type=int, default=None,
            help="Max listings to scrape. Overrides config max_listings_per_run.",
        )
        parser.add_argument(
            "--min-price", type=int, default=None,
            help="Filter: minimum listing price.",
        )
        parser.add_argument(
            "--max-price", type=int, default=None,
            help="Filter: maximum listing price.",
        )
        parser.add_argument(
            "--delay", type=float, default=None,
            help="Override delay between fetches (seconds).",
        )
        parser.add_argument(
            "--no-headless", action="store_true",
            help="Run browser in visible mode (debugging).",
        )

    def run_pipeline(self, args: argparse.Namespace) -> PipelineRunResult:

        validator = RecordValidator()

        # Override config from CLI
        if args.delay is not None:
            self._config["craigslist"]["delay_between_fetches"] = args.delay
        if args.no_headless:
            self._config["craigslist"]["headless"] = False

        extractor = CraigslistExtractor(self._config)

        # Incremental: skip already-ingested posting IDs
        existing_ids = set()
        if args.mode == "incremental":
            existing_ids = self._get_existing_ids()

        # ── Extract ──────────────────────────────────────────

        raw_records = extractor.extract(
            limit=args.limit,
            min_price=args.min_price,
            max_price=args.max_price,
            existing_ids=existing_ids,
        )

        if not raw_records:
            self.log.warning("no_listings_extracted")
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
                records_extracted=0,
            )

        self.log.info("extraction_done", count=len(raw_records))

        # ── Validate ─────────────────────────────────────────

        valid = []
        seen_ids = set()

        for raw in raw_records:
            pid = raw.get("posting_id")

            # Deduplicate within batch
            if pid in seen_ids:
                continue
            seen_ids.add(pid)

            # Required fields
            missing = [f for f in self.REQUIRED_FIELDS if not raw.get(f)]
            if missing:
                self.record_error(
                    record_key=pid,
                    error_type="missing_required",
                    error_message=f"Missing: {', '.join(missing)}",
                )
                continue

            # Spatial validation
            result = validator.validate(
                record=raw, lat_field="lat", lon_field="lon",
            )
            if not result.valid:
                self.record_error(
                    record_key=pid,
                    error_type="validation",
                    error_message="; ".join(result.errors),
                )
                continue

            valid.append(raw)

        self.log.info("validation_done",
                      valid=len(valid),
                      rejected=len(raw_records) - len(valid))

        if not valid:
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
                records_extracted=len(raw_records),
            )

        # ── Transform ────────────────────────────────────────

        transformed = [self._transform(r) for r in valid]
        transformed = [r for r in transformed if r]

        self.log.info("transform_done", count=len(transformed))

        if args.dry_run:
            self.log.info("dry_run_complete",
                          extracted=len(raw_records),
                          transformed=len(transformed))
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
                records_extracted=len(raw_records),
                records_loaded=0,
            )

        # ── Load ─────────────────────────────────────────────

        stage_table = self._create_staging_table()
        self._stage_batch(stage_table, transformed)
        loaded = self._merge(stage_table)
        self._drop_staging_table(stage_table)

        return PipelineRunResult(
            pipeline_run_id=self.pipeline_run_id,
            source=self.SOURCE,
            records_extracted=len(raw_records),
            records_loaded=loaded,
            records_skipped=len(transformed) - loaded,
            records_failed=len(raw_records) - len(valid),
        )

    # ── Incremental optimization ─────────────────────────────

    def _get_existing_ids(self) -> set[str]:
        """Fetch Craigslist posting IDs already in Snowflake."""
        self.cursor.execute(
            "SELECT source_native_id FROM RAW.LISTINGS "
            "WHERE source = %s", (self._source_name,)
        )
        ids = {row[0] for row in self.cursor.fetchall() if row[0]}
        self.log.info("existing_ids_loaded", count=len(ids))
        return ids

    # ── Transform ────────────────────────────────────────────

    def _transform(self, raw: dict) -> dict | None:
        pid = str(raw["posting_id"])
        lid = _listing_id(self._source_name, pid)

        return {
            "listing_id": lid,
            "source": self._source_name,
            "source_native_id": pid,
            "source_url": raw.get("url"),
            "price": raw.get("price"),
            "beds": raw.get("beds"),
            "baths": raw.get("baths"),
            "sqft": raw.get("sqft"),
            "street": raw.get("address"),
            "unit": None,
            "city": "Boston",
            "zip_code": None,
            "neighborhood": None,
            "lat": raw.get("lat"),
            "lon": raw.get("lon"),
            "description_text": raw.get("description"),
            "primary_photo_url": raw["images"][0] if raw.get("images") else None,
            "mls_id": None,
            "mls_status": None,
            "days_on_mls": None,
            "agent_name": None,
            "style": None,
            "list_date": _normalize_timestamp(raw.get("posted_date")),
            "is_current": True,
            "raw_json": json.dumps(raw, default=str),
            "pipeline_run_id": self.pipeline_run_id,
        }

    # ── Staging + Merge ──────────────────────────────────────

    def _create_staging_table(self) -> str:
        batch_id = self.pipeline_run_id[:8]
        table = f"RAW.LISTINGS_CL_STAGING_{batch_id}"

        self.cursor.execute(f"""
            CREATE TEMPORARY TABLE {table} (
                listing_id          VARCHAR(64),
                source              VARCHAR(20),
                source_native_id    VARCHAR(50),
                source_url          VARCHAR(500),
                price               INT,
                beds                INT,
                baths               INT,
                sqft                INT,
                street              VARCHAR(500),
                unit                VARCHAR(100),
                city                VARCHAR(100),
                zip_code            VARCHAR(10),
                neighborhood        VARCHAR(100),
                lat                 FLOAT,
                lon                 FLOAT,
                description_text    TEXT,
                primary_photo_url   VARCHAR(500),
                mls_id              VARCHAR(50),
                mls_status          VARCHAR(30),
                days_on_mls         INT,
                agent_name          VARCHAR(500),
                style               VARCHAR(50),
                list_date           VARCHAR(50),
                is_current          BOOLEAN,
                raw_json            VARCHAR,
                pipeline_run_id     VARCHAR(36)
            )
        """)

        self.log.info("staging_table_created", table=table)
        return table

    def _stage_batch(self, stage_table: str, records: list[dict]):
        sql = f"""
            INSERT INTO {stage_table} (
                listing_id, source, source_native_id, source_url,
                price, beds, baths, sqft,
                street, unit, city, zip_code, neighborhood,
                lat, lon, description_text, primary_photo_url,
                mls_id, mls_status, days_on_mls, agent_name, style,
                list_date, is_current, raw_json, pipeline_run_id
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
        """

        rows = [
            (
                r["listing_id"], r["source"], r["source_native_id"], r["source_url"],
                r["price"], r["beds"], r["baths"], r["sqft"],
                r["street"], r["unit"], r["city"], r["zip_code"], r["neighborhood"],
                r["lat"], r["lon"], r["description_text"], r["primary_photo_url"],
                r["mls_id"], r["mls_status"], r["days_on_mls"], r["agent_name"],
                r["style"], r["list_date"], r["is_current"], r["raw_json"],
                r["pipeline_run_id"],
            )
            for r in records
        ]

        batch_size = 500
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            self.cursor.executemany(sql, batch)
            self.log.debug("staged_batch", offset=i, size=len(batch))

    def _merge(self, stage_table: str) -> int:
        """Upsert: insert new, update existing with latest data."""
        self.cursor.execute(f"""
            MERGE INTO RAW.LISTINGS AS target
            USING {stage_table} AS src
            ON target.listing_id = src.listing_id
            WHEN MATCHED THEN UPDATE SET
                price = src.price,
                beds = src.beds,
                baths = src.baths,
                sqft = src.sqft,
                street = src.street,
                unit = src.unit,
                mls_status = src.mls_status,
                days_on_mls = src.days_on_mls,
                agent_name = src.agent_name,
                description_text = src.description_text,
                primary_photo_url = src.primary_photo_url,
                is_current = TRUE,
                last_seen_at = CURRENT_TIMESTAMP(),
                raw_json = PARSE_JSON(src.raw_json),
                pipeline_run_id = src.pipeline_run_id,
                scraped_at = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (
                listing_id, source, source_native_id, source_url,
                price, beds, baths, sqft,
                street, unit, city, zip_code, neighborhood,
                lat, lon, description_text, primary_photo_url,
                mls_id, mls_status, days_on_mls, agent_name, style,
                list_date, is_current, raw_json, pipeline_run_id
            ) VALUES (
                src.listing_id, src.source, src.source_native_id, src.source_url,
                src.price, src.beds, src.baths, src.sqft,
                src.street, src.unit, src.city, src.zip_code, src.neighborhood,
                src.lat, src.lon, src.description_text, src.primary_photo_url,
                src.mls_id, src.mls_status, src.days_on_mls, src.agent_name,
                src.style, src.list_date::TIMESTAMP_NTZ, TRUE,
                PARSE_JSON(src.raw_json), src.pipeline_run_id
            )
        """)

        loaded = self.cursor.rowcount
        self.conn.commit()
        self.log.info("merge_complete", loaded=loaded)
        return loaded

    def _drop_staging_table(self, stage_table: str):
        self.cursor.execute(f"DROP TABLE IF EXISTS {stage_table}")


if __name__ == "__main__":
    pipeline = CraigslistPipeline()
    result = pipeline.run()
    raise SystemExit(0 if result.status == "success" else 1)
