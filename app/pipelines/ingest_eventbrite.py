"""Eventbrite lifestyle-signal ingestion pipeline.

Fetches events matching preference tags from Eventbrite search,
validates coordinates and proximity, upserts to RAW.LIFESTYLE_SIGNALS.

Tag resolution (precedence):
  --tags live_music yoga     → run exactly those tags
  --preference-tag live_music → single tag (backward compat / manual trigger)
  --category lifestyle        → all non-livability tags from config
  (none)                      → all keys from config/sources/eventbrite.yml queries

Config is agent-writable: the Organizer Agent appends tags and slugs
to config/sources/eventbrite.yml; the next DAG run picks them up.

Usage:
    python -m app.pipelines.ingest_eventbrite --preference-tag live_music
    python -m app.pipelines.ingest_eventbrite --tags live_music korean_food yoga
    python -m app.pipelines.ingest_eventbrite --category lifestyle --dry-run
    python -m app.pipelines.ingest_eventbrite --dry-run
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


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(min(math.sqrt(a), 1.0))


def _tag_to_slug(tag: str) -> str:
    return tag.strip().lower().replace("_", "-")


class EventbriteExtractor:
    """Fetches Eventbrite search results via StealthyFetcher + __SERVER_DATA__ parse."""

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

    def _fetch_html(self, url: str) -> str | None:
        for attempt in range(1, self._max_attempts + 1):
            try:
                page = StealthyFetcher.fetch(url, headless=self._headless, network_idle=True)
                html = page.html_content if hasattr(page, "html_content") else str(page)
                if len(html) < 2000:
                    wait = min(self._backoff_base ** attempt, self._backoff_max)
                    self._log.warning("page_too_short", url=url[:100], chars=len(html),
                                      attempt=attempt, wait_s=wait)
                    if attempt < self._max_attempts:
                        time.sleep(wait)
                        continue
                    return None
                return html
            except Exception as exc:
                wait = min(self._backoff_base ** attempt, self._backoff_max)
                self._log.warning("fetch_failed", url=url[:100], attempt=attempt,
                                  error=str(exc), wait_s=wait)
                if attempt < self._max_attempts:
                    time.sleep(wait)
        self._log.error("exhausted_retries", url=url[:100])
        return None

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
        parser.add_argument("--preference-tag", type=str, default=None,
                            help="Single tag. Omit to run all configured tags.")
        parser.add_argument("--tags", nargs="*", default=None,
                            help="Explicit tag list (e.g. --tags live_music korean_food).")
        parser.add_argument("--category", type=str, default=None,
                            choices=["livability", "lifestyle"],
                            help="Run livability or lifestyle partition only.")
        parser.add_argument("--query", type=str, default=None,
                            help="Custom search slug override.")
        parser.add_argument("--pages", type=int, default=1,
                            help="Number of result pages to fetch (20 events/page).")

    def _resolve_tags(self, args: argparse.Namespace) -> list[str]:
        """--tags > --preference-tag > --category partition > all config keys."""
        if args.tags:
            return args.tags
        if args.preference_tag:
            return [args.preference_tag]
        all_tags = list(self._config.get("queries", {}).keys())
        if args.category:
            livability = set(self._config.get("livability_tags", []))
            if args.category == "livability":
                return [t for t in all_tags if t in livability]
            return [t for t in all_tags if t not in livability]
        return all_tags

    def _load_seen_signals(self, tag: str) -> set[str]:
        """Load existing signal_ids for this source + tag to skip reprocessing."""
        self.cursor.execute(
            "SELECT signal_id FROM RAW.LIFESTYLE_SIGNALS "
            "WHERE signal_source = %s AND preference_tag = %s",
            (self.SOURCE, tag),
        )
        return {row[0] for row in self.cursor.fetchall()}

    def run_pipeline(self, args: argparse.Namespace) -> PipelineRunResult:
        tags = self._resolve_tags(args)
        if not tags:
            self.log.warning("no_tags_resolved")
            return PipelineRunResult(pipeline_run_id=self.pipeline_run_id, source=self.SOURCE)

        self.log.info("tags_resolved", tags=tags, count=len(tags))
        total_extracted = 0
        total_loaded = 0
        total_skipped = 0
        total_failed = 0
        tags_succeeded = 0

        for tag in tags:
            self.log.info("tag_start", tag=tag)
            try:
                result = self._run_tag(tag, args)
                total_extracted += result.records_extracted
                total_loaded += result.records_loaded
                total_skipped += result.records_skipped
                total_failed += result.records_failed
                tags_succeeded += 1
                self.log.info("tag_complete", tag=tag,
                              extracted=result.records_extracted,
                              loaded=result.records_loaded)
            except Exception as exc:
                self.log.error("tag_failed", tag=tag, error=str(exc))
                self.record_error(record_key=tag, error_type="tag_failure",
                                  error_message=str(exc))

        return PipelineRunResult(
            pipeline_run_id=self.pipeline_run_id, source=self.SOURCE,
            status="success" if tags_succeeded > 0 else "failed",
            records_extracted=total_extracted, records_loaded=total_loaded,
            records_skipped=total_skipped, records_failed=total_failed,
        )

    def _run_tag(self, tag: str, args: argparse.Namespace) -> PipelineRunResult:
        max_pages = args.pages
        validator = RecordValidator()
        extractor = EventbriteExtractor(self._config)

        if args.query:
            slugs = [args.query]
        else:
            slugs = self._config.get("queries", {}).get(tag, [])
            if not slugs:
                slugs = [_tag_to_slug(tag)]

        seen_signals = self._load_seen_signals(tag)
        self.log.info("seen_signals_loaded", tag=tag, count=len(seen_signals))

        raw_events: list[dict] = []
        seen_ids: set[str] = set()

        for slug in slugs:
            events = extractor.fetch_events(slug, max_pages=max_pages)
            for e in events:
                eid = str(e.get("id", ""))
                if eid and eid not in seen_ids:
                    seen_ids.add(eid)
                    raw_events.append(e)
            if events and slug != slugs[-1]:
                time.sleep(self._config.get("rate_limit", {}).get(
                    "delay_between_requests", 1.0))

        if not raw_events:
            self.log.info("no_events", tag=tag, slugs=slugs)
            return PipelineRunResult(pipeline_run_id=self.pipeline_run_id,
                                     source=self.SOURCE, records_extracted=0)

        valid = []
        for raw in raw_events:
            event_id = raw.get("id")
            event_name = raw.get("name")
            if not event_id or not event_name:
                self.record_error(record_key=str(event_id), error_type="validation",
                                  error_message="missing id or name")
                continue
            venue = raw.get("primary_venue") or {}
            addr = venue.get("address") or {}
            raw["_lat"] = addr.get("latitude")
            raw["_lon"] = addr.get("longitude")
            result = validator.validate(record=raw, lat_field="_lat", lon_field="_lon",
                                        required=["id", "name"])
            if not result.valid:
                self.record_error(record_key=str(event_id), error_type="validation",
                                  error_message="; ".join(result.errors))
                continue
            lat = float(raw["_lat"])
            lon = float(raw["_lon"])
            dist = _haversine_km(self._center_lat, self._center_lon, lat, lon)
            if dist > self._max_radius_km:
                self.record_error(record_key=str(event_id), error_type="proximity",
                                  error_message=f"distance={dist:.1f}km > max={self._max_radius_km}km")
                continue
            valid.append(raw)

        if not valid:
            return PipelineRunResult(pipeline_run_id=self.pipeline_run_id,
                                     source=self.SOURCE, records_extracted=len(raw_events))

        # Skip events already in Snowflake from prior runs
        novel = []
        for raw in valid:
            eid = str(raw.get("id", ""))
            sid = hashlib.sha256(f"eventbrite:{eid}:{tag}".encode()).hexdigest()[:64]
            if sid not in seen_signals:
                novel.append(raw)

        skipped_seen = len(valid) - len(novel)
        if skipped_seen:
            self.log.info("skipped_seen", tag=tag, skipped=skipped_seen)

        if not novel:
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id, source=self.SOURCE,
                records_extracted=len(raw_events), records_skipped=skipped_seen)

        transformed = [self._transform(r, tag, validator) for r in novel]
        transformed = [r for r in transformed if r]

        if args.dry_run:
            self.log.info("dry_run_complete", tag=tag, extracted=len(raw_events),
                          valid=len(valid), novel=len(novel), transformed=len(transformed))
            return PipelineRunResult(pipeline_run_id=self.pipeline_run_id,
                                     source=self.SOURCE, records_extracted=len(raw_events),
                                     records_loaded=0, records_skipped=skipped_seen)

        stage_table = self._create_staging_table()
        self._stage_batch(stage_table, transformed)
        loaded = self._merge(stage_table)
        self._drop_staging_table(stage_table)

        return PipelineRunResult(
            pipeline_run_id=self.pipeline_run_id, source=self.SOURCE,
            records_extracted=len(raw_events), records_loaded=loaded,
            records_skipped=skipped_seen + (len(novel) - len(transformed)),
            records_failed=len(raw_events) - len(valid),
        )

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
        content_hash = hashlib.sha256(f"{name}|{summary}".encode()).hexdigest()
        signal_id = hashlib.sha256(f"eventbrite:{event_id}:{preference_tag}".encode()).hexdigest()[:64]
        tags = [t.get("display_name", "") for t in (raw.get("tags") or []) if isinstance(t, dict)]

        return {
            "signal_id": signal_id, "signal_source": "eventbrite",
            "source_native_id": event_id, "preference_tag": preference_tag,
            "title": name, "snippet_text": summary, "url": url,
            "content_hash": content_hash, "sentiment": None,
            "relevance_score": None,
            "lat": v.to_float(addr.get("latitude")),
            "lon": v.to_float(addr.get("longitude")),
            "classification_metadata": json.dumps({
                "venue_name": v.to_str(venue.get("name")),
                "venue_address": v.to_str(addr.get("localized_address_display")),
                "start_date": v.to_str(raw.get("start_date")),
                "end_date": v.to_str(raw.get("end_date")),
                "image_url": v.to_str((raw.get("image") or {}).get("url")),
                "tags": tags, "is_free": raw.get("is_free", False),
                "ticket_availability": raw.get("ticket_availability"),
            }),
            "pipeline_run_id": self.pipeline_run_id,
        }

    def _create_staging_table(self) -> str:
        batch_id = self.pipeline_run_id[:8]
        table = f"RAW.EVENTBRITE_STAGING_{batch_id}"
        self.cursor.execute(f"""
            CREATE TEMPORARY TABLE {table} (
                signal_id VARCHAR(64), signal_source VARCHAR(30),
                source_native_id VARCHAR(100), preference_tag VARCHAR(50),
                title VARCHAR(500), snippet_text TEXT, url VARCHAR(500),
                content_hash VARCHAR(64), sentiment VARCHAR(20),
                relevance_score INT, lat FLOAT, lon FLOAT,
                classification_metadata TEXT, pipeline_run_id VARCHAR(36)
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
            (r["signal_id"], r["signal_source"], r["source_native_id"],
             r["preference_tag"], r["title"], r["snippet_text"],
             r["url"], r["content_hash"], r["sentiment"],
             r["relevance_score"], r["lat"], r["lon"],
             r["classification_metadata"], r["pipeline_run_id"])
            for r in records
        ]
        self.cursor.executemany(sql, rows)

    def _merge(self, stage_table: str) -> int:
        self.cursor.execute(f"""
            MERGE INTO RAW.LIFESTYLE_SIGNALS AS target
            USING {stage_table} AS src
            ON target.signal_id = src.signal_id
            WHEN MATCHED THEN UPDATE SET
                title = src.title, snippet_text = src.snippet_text,
                url = src.url, content_hash = src.content_hash,
                lat = src.lat, lon = src.lon,
                classification_metadata = PARSE_JSON(src.classification_metadata),
                pipeline_run_id = src.pipeline_run_id,
                fetched_at = CURRENT_TIMESTAMP()
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