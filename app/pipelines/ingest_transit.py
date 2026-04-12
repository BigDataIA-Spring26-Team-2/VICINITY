"""MBTA transit stop ingestion pipeline.

Fetches subway, light rail, and commuter rail parent stations from
MBTA v3 API, resolves serving routes per station, validates, loads
to RAW.TRANSIT_STOPS via staging + MERGE. Monthly seed refresh.

Strategy: fetch all routes by type → fetch stops per route →
aggregate into station-level records with route arrays.
~25 API calls total vs ~200 if querying routes per stop.

Usage:
    python -m app.pipelines.ingest_transit --mode full --dry-run
    python -m app.pipelines.ingest_transit --mode full
    python -m app.pipelines.ingest_transit --mode incremental
    python -m app.pipelines.ingest_transit --route-types 0 1
"""

import json
import time
import argparse
from typing import Optional
from collections import defaultdict

import httpx
import structlog

from app.core.base_pipeline import BasePipeline, PipelineRunResult
from app.core.config_loader import load_source_config
from app.core.validator import RecordValidator

logger = structlog.get_logger()


# ── Extractor ────────────────────────────────────────────────

class MBTAExtractor:
    """Fetches MBTA parent stations with serving routes.

    Two-phase extraction:
    1. Fetch all routes for configured route_types (~25 routes).
    2. For each route, fetch its parent stations.
    3. Aggregate: station → {attributes, route_ids, route_names, route_types}.

    This minimizes API calls: ~25 route-based stop queries vs ~200
    per-station route lookups.
    """

    def __init__(self, config: dict):
        conn = config["connection"]
        self._base_url = conn["base_url"]
        self._page_size = conn["page_size"]
        self._timeout = conn.get("timeout", 30)
        self._route_types = config["route_types"]
        self._delay = config.get("rate_limit", {}).get("delay_between_requests", 0.2)
        self._log = logger.bind(extractor="mbta")

    def extract(self) -> list[dict]:
        """Fetch all parent stations with route associations."""

        # Phase 1: fetch all routes for configured types
        routes = self._fetch_routes()
        if not routes:
            self._log.warning("no_routes_found")
            return []

        self._log.info("routes_fetched", count=len(routes))

        # Phase 2: fetch stops per route, build station → routes map
        stations: dict[str, dict] = {}
        station_routes: dict[str, list[dict]] = defaultdict(list)

        for route in routes:
            route_id = route["id"]
            route_name = route["attributes"]["long_name"]
            route_type = route["attributes"]["type"]

            stops = self._fetch_stops_for_route(route_id)

            for stop in stops:
                sid = stop["id"]
                if sid not in stations:
                    stations[sid] = stop["attributes"]
                station_routes[sid].append({
                    "route_id": route_id,
                    "route_name": route_name,
                    "route_type": route_type,
                })

            self._log.debug("route_stops_fetched",
                            route=route_id, stations=len(stops))
            time.sleep(self._delay)

        # Phase 3: merge into flat records
        records = []
        for sid, attrs in stations.items():
            routes_for_stop = station_routes[sid]

            # Deduplicate routes (same route can appear from child stops)
            seen = set()
            unique_routes = []
            for r in routes_for_stop:
                if r["route_id"] not in seen:
                    seen.add(r["route_id"])
                    unique_routes.append(r)

            records.append({
                "stop_id": sid,
                "stop_name": attrs.get("name"),
                "lat": attrs.get("latitude"),
                "lon": attrs.get("longitude"),
                "municipality": attrs.get("municipality"),
                "wheelchair_boarding": attrs.get("wheelchair_boarding", 0),
                "route_ids": [r["route_id"] for r in unique_routes],
                "route_names": [r["route_name"] for r in unique_routes],
                "route_types": [r["route_type"] for r in unique_routes],
            })

        self._log.info("extraction_complete", stations=len(records))
        return records

    # ── API helpers ──────────────────────────────────────────

    def _fetch_routes(self) -> list[dict]:
        """Fetch all routes for configured route types."""
        type_filter = ",".join(str(t) for t in self._route_types)
        return self._fetch_all_pages(
            "/routes", params={"filter[type]": type_filter},
        )

    def _fetch_stops_for_route(self, route_id: str) -> list[dict]:
        """Fetch parent stations (location_type=1) for a single route."""
        return self._fetch_all_pages(
            "/stops",
            params={
                "filter[route]": route_id,
                "filter[location_type]": "1",
            },
        )

    def _fetch_all_pages(self, path: str, params: dict = None) -> list[dict]:
        """Paginate through MBTA v3 JSON:API responses."""
        all_data = []
        offset = 0
        params = dict(params or {})

        while True:
            params["page[limit]"] = self._page_size
            params["page[offset]"] = offset

            data = self._get(path, params)
            if data is None:
                break

            records = data.get("data", [])
            if not records:
                break

            all_data.extend(records)

            # Check for next page
            next_url = data.get("links", {}).get("next")
            if not next_url or len(records) < self._page_size:
                break

            offset += self._page_size
            time.sleep(self._delay)

        return all_data

    def _get(self, path: str, params: dict) -> dict | None:
        """Single GET with retry. 5 attempts gives ~60s total backoff
        (2+4+8+16+30), enough to outlast most rate-limit windows."""
        url = f"{self._base_url}{path}"

        for attempt in range(1, 6):
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.get(url, params=params)

                if resp.status_code == 200:
                    return resp.json()

                if resp.status_code == 429:
                    wait = min(2.0 ** attempt, 30.0)
                    self._log.warning("rate_limited",
                                      attempt=attempt, wait_s=wait)
                    time.sleep(wait)
                    continue

                self._log.error("http_error",
                                status=resp.status_code, path=path,
                                attempt=attempt)
                time.sleep(2.0 ** attempt)

            except httpx.TimeoutException:
                self._log.warning("timeout", path=path, attempt=attempt)
                time.sleep(2.0 ** attempt)

            except httpx.RequestError as e:
                self._log.error("request_error",
                                error=str(e), attempt=attempt)
                time.sleep(2.0 ** attempt)

        self._log.error("fetch_exhausted", path=path)
        return None


# ── Pipeline ─────────────────────────────────────────────────

class TransitPipeline(BasePipeline):
    """Ingest MBTA parent stations into RAW.TRANSIT_STOPS.

    Monthly seed refresh. Full mode replaces all data.
    Incremental skips if data is fresher than freshness_days.
    """

    SOURCE = "transit"
    DESCRIPTION = "Ingest MBTA transit stops from v3 API"

    def __init__(self):
        super().__init__()
        self._config = load_source_config("transit")
        self._freshness_days = self._config.get("freshness_days", 30)

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser):
        parser.add_argument(
            "--route-types", type=int, nargs="+", default=None,
            help="Override route types. 0=Light Rail, 1=Heavy Rail, 2=Commuter Rail.",
        )

    def run_pipeline(self, args: argparse.Namespace) -> PipelineRunResult:

        # Incremental: skip if data is fresh
        if args.mode == "incremental" and not args.dry_run:
            if self._data_is_fresh():
                self.log.info("data_is_fresh", freshness_days=self._freshness_days)
                return PipelineRunResult(
                    pipeline_run_id=self.pipeline_run_id,
                    source=self.SOURCE,
                    status="success",
                )

        # Override route types from CLI
        if args.route_types:
            self._config["route_types"] = args.route_types

        validator = RecordValidator()
        extractor = MBTAExtractor(self._config)

        # ── Extract ──────────────────────────────────────────

        raw_records = extractor.extract()

        if not raw_records:
            self.log.warning("no_stations_extracted")
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
            sid = raw.get("stop_id")

            if not sid or not raw.get("stop_name"):
                self.record_error(
                    record_key=sid,
                    error_type="missing_required",
                    error_message="Missing stop_id or stop_name",
                )
                continue

            if sid in seen_ids:
                continue
            seen_ids.add(sid)

            result = validator.validate(
                record=raw, lat_field="lat", lon_field="lon",
            )
            if not result.valid:
                self.record_error(
                    record_key=sid,
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
        """Check if existing data was scraped within freshness_days."""
        self.cursor.execute(
            "SELECT MAX(scraped_at) FROM RAW.TRANSIT_STOPS"
        )
        row = self.cursor.fetchone()
        if not row or not row[0]:
            return False
        from datetime import datetime, timedelta, timezone
        last = row[0].replace(tzinfo=timezone.utc) if row[0].tzinfo is None else row[0]
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._freshness_days)
        return last > cutoff

    # ── Transform ────────────────────────────────────────────

    def _transform(self, raw: dict) -> dict:
        return {
            "stop_id": raw["stop_id"],
            "stop_name": raw["stop_name"],
            "lat": raw["lat"],
            "lon": raw["lon"],
            "municipality": raw.get("municipality"),
            "wheelchair_boarding": raw.get("wheelchair_boarding", 0),
            "route_ids": json.dumps(raw.get("route_ids", [])),
            "route_names": json.dumps(raw.get("route_names", [])),
            "route_types": json.dumps(raw.get("route_types", [])),
            "pipeline_run_id": self.pipeline_run_id,
        }

    # ── Staging + Merge ──────────────────────────────────────

    def _create_staging_table(self) -> str:
        batch_id = self.pipeline_run_id[:8]
        table = f"RAW.TRANSIT_STAGING_{batch_id}"

        self.cursor.execute(f"""
            CREATE TEMPORARY TABLE {table} (
                stop_id                 VARCHAR(20),
                stop_name               VARCHAR(100),
                lat                     FLOAT,
                lon                     FLOAT,
                municipality            VARCHAR(50),
                wheelchair_boarding     INT,
                route_ids               VARCHAR,
                route_names             VARCHAR,
                route_types             VARCHAR,
                pipeline_run_id         VARCHAR(36)
            )
        """)

        self.log.info("staging_table_created", table=table)
        return table

    def _stage_batch(self, stage_table: str, records: list[dict]):
        sql = f"""
            INSERT INTO {stage_table} (
                stop_id, stop_name, lat, lon, municipality,
                wheelchair_boarding, route_ids, route_names,
                route_types, pipeline_run_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        rows = [
            (
                r["stop_id"], r["stop_name"], r["lat"], r["lon"],
                r["municipality"], r["wheelchair_boarding"],
                r["route_ids"], r["route_names"], r["route_types"],
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
        """Full upsert: insert new stations, update existing."""
        self.cursor.execute(f"""
            MERGE INTO RAW.TRANSIT_STOPS AS target
            USING {stage_table} AS src
            ON target.stop_id = src.stop_id
            WHEN MATCHED THEN UPDATE SET
                stop_name = src.stop_name,
                lat = src.lat,
                lon = src.lon,
                municipality = src.municipality,
                wheelchair_boarding = src.wheelchair_boarding,
                route_ids = PARSE_JSON(src.route_ids),
                route_names = PARSE_JSON(src.route_names),
                route_types = PARSE_JSON(src.route_types),
                pipeline_run_id = src.pipeline_run_id,
                scraped_at = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (
                stop_id, stop_name, lat, lon, municipality,
                wheelchair_boarding, route_ids, route_names,
                route_types, pipeline_run_id
            ) VALUES (
                src.stop_id, src.stop_name, src.lat, src.lon,
                src.municipality, src.wheelchair_boarding,
                PARSE_JSON(src.route_ids), PARSE_JSON(src.route_names),
                PARSE_JSON(src.route_types), src.pipeline_run_id
            )
        """)

        loaded = self.cursor.rowcount
        self.conn.commit()
        self.log.info("merge_complete", loaded=loaded)
        return loaded

    def _drop_staging_table(self, stage_table: str):
        self.cursor.execute(f"DROP TABLE IF EXISTS {stage_table}")


if __name__ == "__main__":
    pipeline = TransitPipeline()
    result = pipeline.run()
    raise SystemExit(0 if result.status == "success" else 1)
