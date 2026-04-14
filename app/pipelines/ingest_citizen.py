"""Citizen incident ingestion pipeline.

Fetches Boston-area trending incidents, hydrates each by key,
classifies titles via LLM cache, upserts to RAW.CITIZEN_INCIDENTS.
Handles evolving incidents — severity and closed status update on re-fetch.

Usage:
    python -m app.pipelines.ingest_citizen --dry-run
    python -m app.pipelines.ingest_citizen
    python -m app.pipelines.ingest_citizen --limit 10
"""

import json
import time
import argparse
from datetime import datetime, timezone

import httpx
import structlog

from app.core.base_pipeline import BasePipeline, PipelineRunResult
from app.core.config_loader import load_source_config
from app.core.validator import RecordValidator
from app.core.classifier import ClassificationCache

logger = structlog.get_logger()


class CitizenExtractor:
    """Fetches Boston Citizen incidents: trending list then per-incident detail."""

    def __init__(self, config: dict):
        conn = config["connection"]
        center = config["center"]

        self._trending_url = conn["base_url"]
        self._detail_url = conn["detail_url"]
        self._timeout = conn.get("timeout", 30)
        self._limit = conn.get("limit", 50)

        buf = conn.get("bbox_buffer", 0.02)
        self._bbox = {
            "lowerLatitude": center["lat"] - buf,
            "lowerLongitude": center["lon"] - buf,
            "upperLatitude": center["lat"] + buf,
            "upperLongitude": center["lon"] + buf,
            "fullResponse": "true",
            "limit": self._limit,
        }

        rate = config.get("rate_limit", {})
        self._delay = rate.get("delay_between_requests", 0.5)
        self._backoff_base = rate.get("backoff_base", 2.0)
        self._backoff_max = rate.get("backoff_max", 30.0)
        self._max_attempts = rate.get("max_attempts", 3)
        self._headers = {"User-Agent": "Vicinity/1.0"}
        self._log = logger.bind(extractor="citizen")

    def fetch_trending(self, max_records: int = None) -> list[dict]:
        """Fetch trending feed, extract incident keys, hydrate each."""
        data = self._get(self._trending_url, params=self._bbox)
        if data is None:
            return []

        # Citizen returns list directly or dict with list values
        items = data if isinstance(data, list) else []
        if isinstance(data, dict):
            for val in data.values():
                if isinstance(val, list):
                    items = val
                    break

        # Extract unique keys preserving order
        keys = list(dict.fromkeys(
            item.get("key") for item in items
            if isinstance(item, dict) and item.get("key")
        ))

        limit = max_records or self._limit
        keys = keys[:limit]
        self._log.info("trending_fetched", keys=len(keys))

        # Hydrate each incident via detail endpoint
        hydrated = []
        for key in keys:
            detail = self._get(f"{self._detail_url}/{key}")
            if detail and isinstance(detail, dict):
                hydrated.append(detail)
            else:
                match = next((i for i in items if i.get("key") == key), None)
                if match:
                    hydrated.append(match)
            time.sleep(self._delay)

        self._log.info("incidents_hydrated", count=len(hydrated))
        return hydrated

    def _get(self, url: str, params: dict = None) -> dict | list | None:
        """GET with retry and backoff."""
        for attempt in range(1, self._max_attempts + 1):
            try:
                with httpx.Client(timeout=self._timeout, headers=self._headers) as c:
                    resp = c.get(url, params=params)

                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 404:
                    return None
                if resp.status_code == 429 or resp.status_code >= 500:
                    wait = min(self._backoff_base ** attempt, self._backoff_max)
                    self._log.warning("retrying", status=resp.status_code,
                                      attempt=attempt, wait_s=wait)
                    time.sleep(wait)
                    continue

                self._log.error("http_error", status=resp.status_code, url=url[:80])
                return None

            except (httpx.TimeoutException, httpx.RequestError) as e:
                wait = min(self._backoff_base ** attempt, self._backoff_max)
                self._log.warning("request_failed", error=str(e),
                                  attempt=attempt, wait_s=wait)
                time.sleep(wait)

        self._log.error("exhausted_retries", url=url[:80])
        return None


class CitizenPipeline(BasePipeline):

    SOURCE = "citizen"
    DESCRIPTION = "Ingest Boston Citizen incidents from trending feed"

    def __init__(self):
        super().__init__()
        self._config = load_source_config("citizen")

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser):
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Max incidents to fetch. Use for testing.",
        )

    def run_pipeline(self, args: argparse.Namespace) -> PipelineRunResult:

        validator = RecordValidator()
        extractor = CitizenExtractor(self._config)
        classifier = None
        classification_cfg = self._config.get("classification", {})
        if classification_cfg.get("enabled", True):
            classifier = ClassificationCache(
                cursor=self.cursor,
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
                field_name="title",
            )

        # ── Extract + validate ───────────────────────────────

        raw_incidents = extractor.fetch_trending(max_records=args.limit)

        if not raw_incidents:
            self.log.info("no_incidents")
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
                records_extracted=0,
            )

        valid = []
        for raw in raw_incidents:
            result = validator.validate(
                record=raw,
                lat_field="latitude",
                lon_field="longitude",
                required=["key", "title"],
            )
            if result.valid:
                valid.append(raw)
            else:
                self.record_error(
                    record_key=raw.get("key"),
                    error_type="validation",
                    error_message="; ".join(result.errors),
                    raw_record=raw,
                )

        if not valid:
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
                records_extracted=len(raw_incidents),
            )

        # ── Classify ─────────────────────────────────────────

        titles = [validator.to_str(r.get("title")) or "" for r in valid]
        classifications = classifier.classify(titles) if classifier else {}

        # ── Transform ────────────────────────────────────────

        transformed = [self._transform(r, classifications, validator) for r in valid]
        transformed = [r for r in transformed if r]

        if args.dry_run:
            self.log.info("dry_run_complete",
                          extracted=len(raw_incidents),
                          valid=len(valid))
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
                records_extracted=len(raw_incidents),
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
            records_extracted=len(raw_incidents),
            records_loaded=loaded,
            records_skipped=len(transformed) - loaded,
            records_failed=len(raw_incidents) - len(valid),
        )

    def _transform(self, raw: dict, classifications: dict,
                   v: RecordValidator) -> dict | None:
        title = v.to_str(raw.get("title"))
        key = v.to_str(raw.get("key"))
        if not title or not key:
            return None

        classification = classifications.get(title, {})

        # Normalize categories
        categories = raw.get("categories", [])
        if isinstance(categories, str):
            categories = [categories]
        if not isinstance(categories, list):
            categories = []

        # Timestamp: Citizen uses millisecond epoch
        ts = raw.get("ts") or raw.get("cs")
        incident_ts = None
        is_nighttime = False
        if ts and isinstance(ts, (int, float)) and ts > 1e12:
            dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            incident_ts = dt.strftime("%Y-%m-%d %H:%M:%S")
            is_nighttime = dt.hour >= 22 or dt.hour < 6

        address = v.to_str(raw.get("address"))
        district = v.to_str(raw.get("police"), max_len=20)
        severity = classification.get("severity") or v.to_str(raw.get("severity")) or "unknown"
        category = classification.get("category") or (categories[0] if categories else "other")

        # Per-record narrative with location context
        type_narrative = classification.get("narrative", title)
        parts = [type_narrative.rstrip(".")]
        if address:
            parts.append(f"at {address.split(',')[0]}")
        if is_nighttime:
            parts.append("(nighttime)")
        record_narrative = " ".join(parts) + "."

        return {
            "incident_key": key,
            "title": title,
            "description": v.to_str(raw.get("raw") or raw.get("location") or title),
            "categories": json.dumps(categories),
            "severity": v.to_str(severity, max_len=20),
            "level": v.to_int(raw.get("level")),
            "is_nighttime": is_nighttime,
            "lat": v.to_float(raw.get("latitude")),
            "lon": v.to_float(raw.get("longitude")),
            "address": address,
            "police_district": district,
            "incident_ts": incident_ts,
            "source": v.to_str(raw.get("source"), max_len=20) or "citizen",
            "closed": v.to_bool(raw.get("closed")),
            "classification_metadata": json.dumps({
                "severity": severity,
                "category": category,
                "narrative": record_narrative,
                "type_narrative": classification.get("narrative"),
                "source_fields": {
                    "title": title,
                    "address": address,
                    "police_district": district,
                    "categories": categories,
                },
            }),
            "pipeline_run_id": self.pipeline_run_id,
        }

    def _create_staging_table(self) -> str:
        batch_id = self.pipeline_run_id[:8]
        table = f"RAW.CITIZEN_STAGING_{batch_id}"

        self.cursor.execute(f"""
            CREATE TEMPORARY TABLE {table} (
                incident_key            VARCHAR(50),
                title                   VARCHAR(500),
                description             TEXT,
                categories              TEXT,
                severity                VARCHAR(20),
                level                   INT,
                is_nighttime            BOOLEAN,
                lat                     FLOAT,
                lon                     FLOAT,
                address                 VARCHAR(500),
                police_district         VARCHAR(20),
                incident_ts             VARCHAR(50),
                source                  VARCHAR(20),
                closed                  BOOLEAN,
                classification_metadata TEXT,
                pipeline_run_id         VARCHAR(36)
            )
        """)

        self.log.info("staging_table_created", table=table)
        return table

    def _stage_batch(self, stage_table: str, records: list[dict]):
        sql = f"""
            INSERT INTO {stage_table} (
                incident_key, title, description, categories,
                severity, level, is_nighttime, lat, lon,
                address, police_district, incident_ts, source,
                closed, classification_metadata, pipeline_run_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        rows = [
            (
                r["incident_key"], r["title"], r["description"], r["categories"],
                r["severity"], r["level"], r["is_nighttime"], r["lat"], r["lon"],
                r["address"], r["police_district"], r["incident_ts"], r["source"],
                r["closed"], r["classification_metadata"], r["pipeline_run_id"],
            )
            for r in records
        ]

        self.cursor.executemany(sql, rows)

    def _merge(self, stage_table: str) -> int:
        """Upsert: insert new, update evolving incidents."""
        self.cursor.execute(f"""
            MERGE INTO RAW.CITIZEN_INCIDENTS AS target
            USING {stage_table} AS src
            ON target.incident_key = src.incident_key
            WHEN MATCHED THEN UPDATE SET
                title = src.title,
                description = src.description,
                categories = PARSE_JSON(src.categories)::ARRAY,
                severity = src.severity,
                level = src.level,
                is_nighttime = src.is_nighttime,
                address = src.address,
                closed = src.closed,
                classification_metadata = PARSE_JSON(src.classification_metadata),
                pipeline_run_id = src.pipeline_run_id,
                scraped_at = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (
                incident_key, title, description, categories,
                severity, level, is_nighttime, lat, lon,
                address, police_district, incident_ts, source,
                closed, classification_metadata, pipeline_run_id
            ) VALUES (
                src.incident_key, src.title, src.description,
                PARSE_JSON(src.categories)::ARRAY,
                src.severity, src.level, src.is_nighttime,
                src.lat, src.lon, src.address, src.police_district,
                src.incident_ts::TIMESTAMP_NTZ, src.source, src.closed,
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
    pipeline = CitizenPipeline()
    result = pipeline.run()
    raise SystemExit(0 if result.status == "success" else 1)