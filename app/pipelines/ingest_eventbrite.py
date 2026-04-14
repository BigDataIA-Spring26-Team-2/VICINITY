"""Eventbrite lifestyle-signal ingestion pipeline.

Fetches events matching a preference tag from Eventbrite search,
validates coordinates and proximity, upserts to RAW.LIFESTYLE_SIGNALS.
Reuses the same tag→slug convention across all lifestyle sources.

Usage:
    python -m app.pipelines.ingest_eventbrite --preference-tag live_music
    python -m app.pipelines.ingest_eventbrite --preference-tag live_music --dry-run
    python -m app.pipelines.ingest_eventbrite --preference-tag korean_food --query "korean-food-festivals"
"""

import hashlib
import json
import math
import time
import argparse

import structlog

from app.core.base_pipeline import BasePipeline, PipelineRunResult
from app.core.config_loader import load_source_config
from app.core.validator import RecordValidator

logger = structlog.get_logger()

try:
    from scrapling.fetchers import StealthyFetcher
except ImportError:
    raise RuntimeError(
        "scrapling[fetchers] not installed. "
        "Run: pip install 'scrapling[fetchers]' && scrapling install"
    )


# ── Helpers ──────────────────────────────────────────────────

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in kilometres."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.asin(min(math.sqrt(a), 1.0))


def _tag_to_slug(tag: str) -> str:
    """Convert a preference tag to a URL-safe search slug.

    live_music  → live-music
    korean_food → korean-food
    """
    return tag.strip().lower().replace("_", "-")


# ── Extractor ────────────────────────────────────────────────

class EventbriteExtractor:
    """Fetches Eventbrite search results via StealthyFetcher + __SERVER_DATA__ parse.

    Uses Scrapling's stealth browser (Playwright under the hood) with TLS
    fingerprint spoofing and network_idle detection to avoid bot detection.
    """

    _SERVER_DATA_MARKER = "window.__SERVER_DATA__ = "

    def __init__(self, config: dict):
        conn = config["connection"]
        location = config["location"]

        self._base_url = conn["base_url"]
        self._location_slug = location["slug"]

        rate = config.get("rate_limit", {})
        self._delay = rate.get("delay_between_requests", 1.0)
        self._backoff_base = rate.get("backoff_base", 2.0)
        self._backoff_max = rate.get("backoff_max", 30.0)
        self._max_attempts = rate.get("max_attempts", 3)
        self._headless = True

        self._log = logger.bind(extractor="eventbrite")

    def fetch_events(self, query_slug: str, max_pages: int = 1) -> list[dict]:
        """Fetch events across one or more result pages."""
        all_events: list[dict] = []

        for page in range(1, max_pages + 1):
            url = f"{self._base_url}/{self._location_slug}/{query_slug}/"
            if page > 1:
                url += f"?page={page}"

            html = self._fetch_html(url)
            if not html:
                break

            events = self._parse_server_data(html)
            if not events:
                break

            all_events.extend(events)
            self._log.info("page_fetched", query=query_slug, page=page, count=len(events))

            if page < max_pages:
                time.sleep(self._delay)

        self._log.info("extraction_complete", query=query_slug, total=len(all_events))
        return all_events

    # ── Fetch ────────────────────────────────────────────────

    def _fetch_html(self, url: str) -> str | None:
        """Fetch URL via StealthyFetcher with exponential backoff retry."""
        for attempt in range(1, self._max_attempts + 1):
            try:
                page = StealthyFetcher.fetch(
                    url, headless=self._headless, network_idle=True,
                )
                html = (
                    page.html_content
                    if hasattr(page, "html_content")
                    else str(page)
                )

                # Guard against blocked/captcha pages
                if len(html) < 2000:
                    wait = min(self._backoff_base ** attempt, self._backoff_max)
                    self._log.warning("page_too_short",
                                      url=url[:100], chars=len(html),
                                      attempt=attempt, wait_s=wait)
                    if attempt < self._max_attempts:
                        time.sleep(wait)
                        continue
                    return None

                return html

            except Exception as exc:
                wait = min(self._backoff_base ** attempt, self._backoff_max)
                self._log.warning("fetch_failed",
                                  url=url[:100], attempt=attempt,
                                  error=str(exc), wait_s=wait)
                if attempt < self._max_attempts:
                    time.sleep(wait)

        self._log.error("exhausted_retries", url=url[:100])
        return None

    # ── Parser ───────────────────────────────────────────────

    def _parse_server_data(self, html: str) -> list[dict]:
        idx = html.find(self._SERVER_DATA_MARKER)
        if idx == -1:
            self._log.warning("server_data_not_found")
            return []

        json_start = idx + len(self._SERVER_DATA_MARKER)

        try:
            decoder = json.JSONDecoder()
            data, _ = decoder.raw_decode(html, json_start)
        except json.JSONDecodeError as exc:
            self._log.error("json_parse_error", error=str(exc))
            return []

        try:
            results = data["search_data"]["events"]["results"]
            return results if isinstance(results, list) else []
        except (KeyError, TypeError):
            self._log.warning("unexpected_structure")
            return []


# ── Pipeline ─────────────────────────────────────────────────

class EventbritePipeline(BasePipeline):

    SOURCE = "eventbrite"
    DESCRIPTION = "Ingest Eventbrite events as lifestyle signals"

    def __init__(self):
        super().__init__()
        self._config = load_source_config("eventbrite")
        self._center_lat = self._config["center"]["lat"]
        self._center_lon = self._config["center"]["lon"]
        self._max_radius_km = self._config["location"]["max_radius_km"]

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser):
        parser.add_argument(
            "--preference-tag",
            type=str,
            required=True,
            help="Preference tag to query (e.g. live_music, korean_food).",
        )
        parser.add_argument(
            "--query",
            type=str,
            default=None,
            help="Custom search slug. Defaults to tag with hyphens.",
        )
        parser.add_argument(
            "--pages",
            type=int,
            default=1,
            help="Number of result pages to fetch (20 events/page).",
        )

    def run_pipeline(self, args: argparse.Namespace) -> PipelineRunResult:

        preference_tag = args.preference_tag
        query_slug = args.query or _tag_to_slug(preference_tag)
        max_pages = args.pages

        validator = RecordValidator()
        extractor = EventbriteExtractor(
            self._config, self._pipeline_config["retry"],
        )

        # ── Extract ──────────────────────────────────────────

        raw_events = extractor.fetch_events(query_slug, max_pages=max_pages)

        if not raw_events:
            self.log.info("no_events", preference_tag=preference_tag,
                          query=query_slug)
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
                records_extracted=0,
            )

        # ── Validate ─────────────────────────────────────────

        valid = []
        for raw in raw_events:
            event_id = raw.get("id")
            event_name = raw.get("name")

            if not event_id or not event_name:
                self.record_error(
                    record_key=str(event_id),
                    error_type="validation",
                    error_message="missing id or name",
                )
                continue

            # Pull coords from nested venue structure
            venue = raw.get("primary_venue") or {}
            addr = venue.get("address") or {}
            raw["_lat"] = addr.get("latitude")
            raw["_lon"] = addr.get("longitude")

            result = validator.validate(
                record=raw,
                lat_field="_lat",
                lon_field="_lon",
                required=["id", "name"],
            )
            if not result.valid:
                self.record_error(
                    record_key=str(event_id),
                    error_type="validation",
                    error_message="; ".join(result.errors),
                )
                continue

            # Proximity gate — reject events beyond max_radius_km
            lat = float(raw["_lat"])
            lon = float(raw["_lon"])
            dist = _haversine_km(self._center_lat, self._center_lon, lat, lon)
            if dist > self._max_radius_km:
                self.record_error(
                    record_key=str(event_id),
                    error_type="proximity",
                    error_message=f"distance={dist:.1f}km > max={self._max_radius_km}km",
                )
                continue

            valid.append(raw)

        if not valid:
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
                records_extracted=len(raw_events),
            )

        # ── Transform ────────────────────────────────────────

        transformed = [
            self._transform(r, preference_tag, validator) for r in valid
        ]
        transformed = [r for r in transformed if r]

        if args.dry_run:
            self.log.info("dry_run_complete",
                          extracted=len(raw_events),
                          valid=len(valid),
                          transformed=len(transformed))
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
                records_extracted=len(raw_events),
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
            records_extracted=len(raw_events),
            records_loaded=loaded,
            records_skipped=len(valid) - len(transformed),
            records_failed=len(raw_events) - len(valid),
        )

    # ── Transform ────────────────────────────────────────────

    def _transform(self, raw: dict, preference_tag: str,
                   v: RecordValidator) -> dict | None:
        event_id = str(raw.get("id", ""))
        name = v.to_str(raw.get("name"), max_len=500)
        if not event_id or not name:
            return None

        venue = raw.get("primary_venue") or {}
        addr = venue.get("address") or {}

        summary = v.to_str(raw.get("summary"), max_len=2000) or ""
        url = v.to_str(raw.get("url"), max_len=500) or ""

        content_hash = hashlib.sha256(
            f"{name}|{summary}".encode()
        ).hexdigest()

        signal_id = hashlib.sha256(
            f"eventbrite:{event_id}:{preference_tag}".encode()
        ).hexdigest()[:64]

        tags = [
            t.get("display_name", "")
            for t in (raw.get("tags") or [])
            if isinstance(t, dict)
        ]

        return {
            "signal_id": signal_id,
            "signal_source": "eventbrite",
            "source_native_id": event_id,
            "preference_tag": preference_tag,
            "title": name,
            "snippet_text": summary,
            "url": url,
            "content_hash": content_hash,
            "sentiment": None,
            "relevance_score": None,
            "lat": v.to_float(addr.get("latitude")),
            "lon": v.to_float(addr.get("longitude")),
            "classification_metadata": json.dumps({
                "venue_name": v.to_str(venue.get("name")),
                "venue_address": v.to_str(
                    addr.get("localized_address_display")
                ),
                "start_date": v.to_str(raw.get("start_date")),
                "end_date": v.to_str(raw.get("end_date")),
                "image_url": v.to_str(
                    (raw.get("image") or {}).get("url")
                ),
                "tags": tags,
                "is_free": raw.get("is_free", False),
                "ticket_availability": raw.get("ticket_availability"),
            }),
            "pipeline_run_id": self.pipeline_run_id,
        }

    # ── Staging + Merge ──────────────────────────────────────

    def _create_staging_table(self) -> str:
        batch_id = self.pipeline_run_id[:8]
        table = f"RAW.EVENTBRITE_STAGING_{batch_id}"

        self.cursor.execute(f"""
            CREATE TEMPORARY TABLE {table} (
                signal_id               VARCHAR(64),
                signal_source           VARCHAR(30),
                source_native_id        VARCHAR(100),
                preference_tag          VARCHAR(50),
                title                   VARCHAR(500),
                snippet_text            TEXT,
                url                     VARCHAR(500),
                content_hash            VARCHAR(64),
                sentiment               VARCHAR(20),
                relevance_score         INT,
                lat                     FLOAT,
                lon                     FLOAT,
                classification_metadata TEXT,
                pipeline_run_id         VARCHAR(36)
            )
        """)

        self.log.info("staging_table_created", table=table)
        return table

    def _stage_batch(self, stage_table: str, records: list[dict]):
        sql = f"""
            INSERT INTO {stage_table} (
                signal_id, signal_source, source_native_id, preference_tag,
                title, snippet_text, url, content_hash,
                sentiment, relevance_score, lat, lon,
                classification_metadata, pipeline_run_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        rows = [
            (
                r["signal_id"], r["signal_source"], r["source_native_id"],
                r["preference_tag"], r["title"], r["snippet_text"],
                r["url"], r["content_hash"], r["sentiment"],
                r["relevance_score"], r["lat"], r["lon"],
                r["classification_metadata"], r["pipeline_run_id"],
            )
            for r in records
        ]

        self.cursor.executemany(sql, rows)

    def _merge(self, stage_table: str) -> int:
        self.cursor.execute(f"""
            MERGE INTO RAW.LIFESTYLE_SIGNALS AS target
            USING {stage_table} AS src
            ON target.signal_id = src.signal_id
            WHEN MATCHED THEN UPDATE SET
                title                   = src.title,
                snippet_text            = src.snippet_text,
                url                     = src.url,
                content_hash            = src.content_hash,
                lat                     = src.lat,
                lon                     = src.lon,
                classification_metadata = PARSE_JSON(src.classification_metadata),
                pipeline_run_id         = src.pipeline_run_id,
                fetched_at              = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (
                signal_id, signal_source, source_native_id, preference_tag,
                title, snippet_text, url, content_hash,
                sentiment, relevance_score, lat, lon,
                classification_metadata, pipeline_run_id
            ) VALUES (
                src.signal_id, src.signal_source, src.source_native_id,
                src.preference_tag, src.title, src.snippet_text,
                src.url, src.content_hash, src.sentiment,
                src.relevance_score, src.lat, src.lon,
                PARSE_JSON(src.classification_metadata), src.pipeline_run_id
            )
        """)

        loaded = self.cursor.rowcount
        self.conn.commit()
        self.log.info("merge_complete", loaded=loaded)
        return loaded

    def _drop_staging_table(self, stage_table: str):
        self.cursor.execute(f"DROP TABLE IF EXISTS {stage_table}")


if __name__ == "__main__":
    pipeline = EventbritePipeline()
    result = pipeline.run()
    raise SystemExit(0 if result.status == "success" else 1)
