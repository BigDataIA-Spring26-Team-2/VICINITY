"""OpenStreetMap amenities ingestion pipeline.

Fetches points of interest via the Overpass API using bbox queries
for each configured category. One query per category, full metro
Boston coverage, no overlapping circles or gaps.

Usage:
    python -m app.pipelines.ingest_amenities --mode full --dry-run
    python -m app.pipelines.ingest_amenities --mode full
    python -m app.pipelines.ingest_amenities --mode incremental
    python -m app.pipelines.ingest_amenities --categories supermarket pharmacy park
"""

import json
import time
import argparse
from typing import Optional

import httpx
import structlog

from app.core.base_pipeline import BasePipeline, PipelineRunResult
from app.core.config_loader import load_source_config
from app.core.validator import RecordValidator

logger = structlog.get_logger()


# ── Extractor ────────────────────────────────────────────────

class OverpassExtractor:
    """Queries Overpass API for OSM features by tag within a bbox.

    One query per category: fetches both nodes and ways, uses `out center`
    so way elements return a centroid lat/lon. Deduplicates by osm_id
    across all queries (a restaurant could also be tagged as a cafe).
    """

    def __init__(self, config: dict):
        conn = config["connection"]
        self._url = conn["base_url"]
        self._timeout = conn.get("timeout", 60)
        self._bbox = config["bbox"]
        self._queries = config["queries"]
        self._delay = config.get("rate_limit", {}).get("delay_between_queries", 6.0)
        self._max_attempts = config.get("rate_limit", {}).get("max_attempts", 5)
        self._log = logger.bind(extractor="overpass")

    def extract(self, categories: list[str] | None = None) -> list[dict]:
        """Run batched Overpass queries (one per OSM key), return deduplicated records.

        Groups all subcategories by key and uses Overpass regex matching
        to fetch in bulk. 22 individual queries → 3 batched queries.
        """
        bbox_str = f'{self._bbox["south"]},{self._bbox["west"]},{self._bbox["north"]},{self._bbox["east"]}'

        # Group queries by OSM key: {"shop": ["supermarket", "convenience", ...], ...}
        key_groups: dict[str, list[str]] = {}
        for q in self._queries:
            key, value = q["key"], q["value"]
            if categories and value not in categories:
                continue
            key_groups.setdefault(key, []).append(value)

        all_records: dict[int, dict] = {}

        for key, values in key_groups.items():
            regex = "|".join(values)
            query = (
                f'[out:json][timeout:{self._timeout}];'
                f'('
                f'  node["{key}"~"^({regex})$"]({bbox_str});'
                f'  way["{key}"~"^({regex})$"]({bbox_str});'
                f');'
                f'out center;'
            )

            elements = self._run_query(query)
            if elements is None:
                self._log.error("query_failed", key=key, values=values)
                continue

            for el in elements:
                osm_id = el.get("id")
                if not osm_id or osm_id in all_records:
                    continue

                lat = el.get("lat") or el.get("center", {}).get("lat")
                lon = el.get("lon") or el.get("center", {}).get("lon")
                tags = el.get("tags", {})

                # Resolve subcategory from the element's actual tag value
                subcategory = tags.get(key, "unknown")

                all_records[osm_id] = {
                    "osm_id": osm_id,
                    "name": tags.get("name"),
                    "category": key,
                    "subcategory": subcategory,
                    "lat": lat,
                    "lon": lon,
                    "address": _build_address(tags),
                    "opening_hours": tags.get("opening_hours"),
                    "website": tags.get("website"),
                    "phone": tags.get("phone") or tags.get("contact:phone"),
                    "brand": tags.get("brand"),
                    "wheelchair": tags.get("wheelchair"),
                    "tags": tags,
                }

            self._log.info("batch_done", key=key, values=values,
                           results=len(elements),
                           total_unique=len(all_records))
            time.sleep(self._delay)

        records = list(all_records.values())
        self._log.info("extraction_complete", total=len(records))
        return records

    def _run_query(self, query: str) -> list[dict] | None:
        """POST a single Overpass QL query with retry. 5 attempts gives
        ~60s total backoff to outlast rate-limit windows."""
        for attempt in range(1, self._max_attempts + 1):
            try:
                with httpx.Client(timeout=self._timeout + 10) as client:
                    resp = client.post(
                        self._url,
                        data={"data": query},
                    )

                if resp.status_code == 200:
                    return resp.json().get("elements", [])

                if resp.status_code == 429:
                    wait = min(2.0 ** attempt * self._delay, 60.0)
                    self._log.warning("rate_limited",
                                      attempt=attempt, wait_s=wait)
                    time.sleep(wait)
                    continue

                self._log.error("http_error",
                                status=resp.status_code, attempt=attempt)
                time.sleep(2.0 ** attempt)

            except httpx.TimeoutException:
                self._log.warning("timeout", attempt=attempt)
                time.sleep(2.0 ** attempt)

            except httpx.RequestError as e:
                self._log.error("request_error",
                                error=str(e), attempt=attempt)
                time.sleep(2.0 ** attempt)

        return None


def _build_address(tags: dict) -> str | None:
    """Assemble address from OSM addr:* tags."""
    parts = []
    street = tags.get("addr:street")
    number = tags.get("addr:housenumber")
    if number and street:
        parts.append(f"{number} {street}")
    elif street:
        parts.append(street)
    city = tags.get("addr:city")
    if city:
        parts.append(city)
    return ", ".join(parts) if parts else None


# ── Pipeline ─────────────────────────────────────────────────

class AmenitiesPipeline(BasePipeline):
    """Ingest OSM amenities into RAW.AMENITIES.

    Monthly seed refresh. Full mode replaces all data.
    Incremental skips if data is fresher than freshness_days.
    """

    SOURCE = "amenities"
    DESCRIPTION = "Ingest OpenStreetMap amenities via Overpass API"

    def __init__(self):
        super().__init__()
        self._config = load_source_config("amenities")
        self._freshness_days = self._config.get("freshness_days", 30)

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser):
        parser.add_argument(
            "--categories", nargs="+", default=None,
            help="Only fetch specific subcategories (e.g. supermarket pharmacy park).",
        )

    def run_pipeline(self, args: argparse.Namespace) -> PipelineRunResult:

        # Incremental: skip if data is fresh
        if args.mode == "incremental" and not args.dry_run:
            if self._data_is_fresh():
                self.log.info("data_is_fresh",
                              freshness_days=self._freshness_days)
                return PipelineRunResult(
                    pipeline_run_id=self.pipeline_run_id,
                    source=self.SOURCE,
                    status="success",
                )

        validator = RecordValidator()
        extractor = OverpassExtractor(self._config)

        # ── Extract ──────────────────────────────────────────

        raw_records = extractor.extract(
            categories=getattr(args, "categories", None),
        )

        if not raw_records:
            self.log.warning("no_amenities_extracted")
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
                records_extracted=0,
            )

        self.log.info("extraction_done", count=len(raw_records))

        # ── Validate ─────────────────────────────────────────

        valid = []

        for raw in raw_records:
            osm_id = raw.get("osm_id")

            if not raw.get("subcategory"):
                self.record_error(
                    record_key=str(osm_id),
                    error_type="missing_required",
                    error_message="Missing subcategory",
                )
                continue

            result = validator.validate(
                record=raw, lat_field="lat", lon_field="lon",
            )
            if not result.valid:
                self.record_error(
                    record_key=str(osm_id),
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

    # ── Freshness check ──────────────────────────────────────

    def _data_is_fresh(self) -> bool:
        self.cursor.execute("SELECT MAX(scraped_at) FROM RAW.AMENITIES")
        row = self.cursor.fetchone()
        if not row or not row[0]:
            return False
        from datetime import datetime, timedelta, timezone
        last = row[0].replace(tzinfo=timezone.utc) if row[0].tzinfo is None else row[0]
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._freshness_days)
        return last > cutoff

    # ── Transform ────────────────────────────────────────────

    def _transform(self, raw: dict) -> dict:
        # Strip internal/bulky tags before storing
        tags = dict(raw.get("tags", {}))
        for drop_key in ("name", "addr:street", "addr:housenumber",
                         "addr:city", "opening_hours", "website",
                         "phone", "contact:phone", "brand", "wheelchair"):
            tags.pop(drop_key, None)

        return {
            "osm_id": raw["osm_id"],
            "name": raw.get("name"),
            "category": raw["category"],
            "subcategory": raw["subcategory"],
            "lat": raw["lat"],
            "lon": raw["lon"],
            "address": raw.get("address"),
            "opening_hours": raw.get("opening_hours"),
            "website": raw.get("website"),
            "phone": raw.get("phone"),
            "brand": raw.get("brand"),
            "wheelchair": raw.get("wheelchair"),
            "tags": json.dumps(tags),
            "pipeline_run_id": self.pipeline_run_id,
        }

    # ── Staging + Merge ──────────────────────────────────────

    def _create_staging_table(self) -> str:
        batch_id = self.pipeline_run_id[:8]
        table = f"RAW.AMENITIES_STAGING_{batch_id}"

        self.cursor.execute(f"""
            CREATE TEMPORARY TABLE {table} (
                osm_id              BIGINT,
                name                TEXT,
                category            VARCHAR(50),
                subcategory         VARCHAR(50),
                lat                 FLOAT,
                lon                 FLOAT,
                address             TEXT,
                opening_hours       TEXT,
                website             TEXT,
                phone               VARCHAR(100),
                brand               TEXT,
                wheelchair          VARCHAR(20),
                tags                VARCHAR,
                pipeline_run_id     VARCHAR(36)
            )
        """)

        self.log.info("staging_table_created", table=table)
        return table

    def _stage_batch(self, stage_table: str, records: list[dict]):
        sql = f"""
            INSERT INTO {stage_table} (
                osm_id, name, category, subcategory, lat, lon,
                address, opening_hours, website, phone, brand,
                wheelchair, tags, pipeline_run_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        rows = [
            (
                r["osm_id"], r["name"], r["category"], r["subcategory"],
                r["lat"], r["lon"], r["address"], r["opening_hours"],
                r["website"], r["phone"], r["brand"], r["wheelchair"],
                r["tags"], r["pipeline_run_id"],
            )
            for r in records
        ]

        batch_size = 1000
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            self.cursor.executemany(sql, batch)
            self.log.debug("staged_batch", offset=i, size=len(batch))

    def _merge(self, stage_table: str) -> int:
        self.cursor.execute(f"""
            MERGE INTO RAW.AMENITIES AS target
            USING {stage_table} AS src
            ON target.osm_id = src.osm_id
            WHEN MATCHED THEN UPDATE SET
                name = src.name,
                category = src.category,
                subcategory = src.subcategory,
                lat = src.lat,
                lon = src.lon,
                address = src.address,
                opening_hours = src.opening_hours,
                website = src.website,
                phone = src.phone,
                brand = src.brand,
                wheelchair = src.wheelchair,
                tags = PARSE_JSON(src.tags),
                pipeline_run_id = src.pipeline_run_id,
                scraped_at = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (
                osm_id, name, category, subcategory, lat, lon,
                address, opening_hours, website, phone, brand,
                wheelchair, tags, pipeline_run_id
            ) VALUES (
                src.osm_id, src.name, src.category, src.subcategory,
                src.lat, src.lon, src.address, src.opening_hours,
                src.website, src.phone, src.brand, src.wheelchair,
                PARSE_JSON(src.tags), src.pipeline_run_id
            )
        """)

        loaded = self.cursor.rowcount
        self.conn.commit()
        self.log.info("merge_complete", loaded=loaded)
        return loaded

    def _drop_staging_table(self, stage_table: str):
        self.cursor.execute(f"DROP TABLE IF EXISTS {stage_table}")


if __name__ == "__main__":
    pipeline = AmenitiesPipeline()
    result = pipeline.run()
    raise SystemExit(0 if result.status == "success" else 1)
