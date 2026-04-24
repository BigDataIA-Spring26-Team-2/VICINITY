"""Rental listings ingestion pipeline.

Fetches Boston-area rentals from Realtor.com via HomeHarvest,
validates, deduplicates, loads to RAW.LISTINGS via staging + MERGE.

Deactivation strategy (grace-period, not one-shot):
    A listing is marked is_current = FALSE only when it has not been
    seen for `grace_hours` (default 48h). This tolerates HomeHarvest
    response drift — a listing that's active on Realtor.com but not
    returned by a single ZIP search won't be killed. It must be absent
    across multiple runs spanning grace_hours before deactivation.

    Configure via listings.yml:
        deactivation:
          grace_hours: 48

Usage:
    python -m app.pipelines.ingest_listings --mode full --limit 50 --dry-run
    python -m app.pipelines.ingest_listings --mode full
    python -m app.pipelines.ingest_listings --mode incremental
    python -m app.pipelines.ingest_listings --location "Boston, MA" --past-days 3
    python -m app.pipelines.ingest_listings --min-price 1500 --max-price 4000
    python -m app.pipelines.ingest_listings --mode full --skip-deactivation
"""

import json
import time
import hashlib
import argparse

import structlog

from app.core.base_pipeline import BasePipeline, PipelineRunResult
from app.core.config_loader import load_source_config, load_spatial
from app.core.validator import RecordValidator

logger = structlog.get_logger()


def _listing_id(source: str, source_native_id: str) -> str:
    """Deterministic listing ID from source + native ID."""
    return hashlib.sha256(f"{source}:{source_native_id}".encode()).hexdigest()[:16]


class HomeHarvestExtractor:
    """Fetches rental listings via HomeHarvest with per-location retry."""

    def __init__(self, config: dict, location_override: str = None,
                 past_days_override: int = None):
        hh = config["homeharvest"]
        self._locations = [location_override] if location_override else hh["locations"]
        self._listing_type = hh["listing_type"]
        self._past_days = past_days_override or hh["past_days"]
        self._delay = hh["delay_between_calls"]
        self._max_retries = hh.get("max_retries", 3)
        self._log = logger.bind(extractor="homeharvest")

    def extract(self, limit: int = None) -> list[dict]:
        """Fetch listings across all configured locations."""
        try:
            from homeharvest import scrape_property
        except ImportError:
            self._log.error("homeharvest_not_installed")
            raise RuntimeError("homeharvest package not installed")

        all_records = []

        for i, location in enumerate(self._locations):
            records = self._fetch_location(scrape_property, location)
            all_records.extend(records)

            self._log.info("location_fetched",
                           location=location,
                           records=len(records),
                           total=len(all_records))

            if limit and len(all_records) >= limit:
                all_records = all_records[:limit]
                self._log.info("limit_reached", limit=limit)
                break

            if i < len(self._locations) - 1:
                time.sleep(self._delay)

        self._log.info("extraction_complete",
                       total=len(all_records),
                       locations=len(self._locations))
        return all_records

    def _fetch_location(self, scrape_fn, location: str) -> list[dict]:
        """Fetch one location with retry on failure."""
        for attempt in range(1, self._max_retries + 1):
            try:
                df = scrape_fn(
                    location=location,
                    listing_type=self._listing_type,
                    past_days=self._past_days,
                )

                if df is None or df.empty:
                    self._log.warning("empty_response",
                                      location=location, attempt=attempt)
                    if attempt < self._max_retries:
                        time.sleep(self._delay * attempt)
                        continue
                    return []

                return df.to_dict(orient="records")

            except Exception as e:
                self._log.warning("fetch_failed",
                                  location=location,
                                  attempt=attempt,
                                  error=str(e))
                if attempt < self._max_retries:
                    time.sleep(self._delay * attempt)
                    continue

                self._log.error("location_exhausted",
                                location=location, error=str(e))
                return []


class ListingsPipeline(BasePipeline):

    SOURCE = "listings"
    DESCRIPTION = "Ingest Boston rental listings from HomeHarvest"

    # A listing without these is useless to the platform
    REQUIRED_FIELDS = ["property_id", "property_url", "list_price", "latitude", "longitude"]

    def __init__(self):
        super().__init__()
        self._config = load_source_config("listings")
        self._source_name = self._config["dedup"]["source_name"]
        self._source_id_field = self._config["dedup"]["source_id_field"]
        self._spatial = load_spatial()

        # Grace-period deactivation config. Default 48h — a listing must be
        # absent across runs spanning two days before is_current flips.
        deact_cfg = self._config.get("deactivation", {})
        self._grace_hours = deact_cfg.get("grace_hours", 48)

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser):
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Max listings to extract. Use for testing.",
        )
        parser.add_argument(
            "--skip-deactivation",
            action="store_true",
            help="Skip marking unseen listings as inactive.",
        )
        parser.add_argument(
            "--location",
            type=str,
            default=None,
            help="Override locations. Fetch from a single city only.",
        )
        parser.add_argument(
            "--past-days",
            type=int,
            default=None,
            help="Override past_days config. Fetch listings from last N days.",
        )
        parser.add_argument(
            "--min-price",
            type=int,
            default=None,
            help="Filter: minimum listing price.",
        )
        parser.add_argument(
            "--max-price",
            type=int,
            default=None,
            help="Filter: maximum listing price.",
        )
        parser.add_argument(
            "--grace-hours",
            type=int,
            default=None,
            help="Override grace period before a missing listing is deactivated.",
        )

    def run_pipeline(self, args: argparse.Namespace) -> PipelineRunResult:

        validator = RecordValidator()
        extractor = HomeHarvestExtractor(
            self._config,
            location_override=args.location,
            past_days_override=args.past_days,
        )

        # ── Extract ──────────────────────────────────────────

        raw_records = extractor.extract(limit=args.limit)

        if not raw_records:
            self.log.warning("no_listings_extracted")
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
                records_extracted=0,
            )

        self.log.info("extraction_done", count=len(raw_records))

        # ── Validate + deduplicate ───────────────────────────

        valid = []
        seen_ids = set()

        for raw in raw_records:
            source_id = self._clean(raw.get(self._source_id_field))

            # Must have a source ID
            if not source_id:
                self.record_error(
                    record_key=None,
                    error_type="missing_source_id",
                    error_message=f"No {self._source_id_field}",
                    raw_record=raw,
                )
                continue

            # Deduplicate within batch
            if source_id in seen_ids:
                continue
            seen_ids.add(source_id)

            # Check all required fields
            missing = self._check_required(raw)
            if missing:
                self.record_error(
                    record_key=source_id,
                    error_type="missing_required",
                    error_message=f"Missing: {', '.join(missing)}",
                    raw_record=raw,
                )
                continue

            # Validate coordinates within Boston bbox
            result = validator.validate(
                record=raw,
                lat_field="latitude",
                lon_field="longitude",
                required=[self._source_id_field],
            )
            if not result.valid:
                self.record_error(
                    record_key=source_id,
                    error_type="validation",
                    error_message="; ".join(result.errors),
                    raw_record=raw,
                )
                continue

            # Price range filter if specified
            price = validator.to_int(raw.get("list_price"))
            if args.min_price and price and price < args.min_price:
                continue
            if args.max_price and price and price > args.max_price:
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

        transformed = [self._transform(raw, validator) for raw in valid]
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

        # ── Deactivate stale ─────────────────────────────────

        deactivated = 0
        if not args.skip_deactivation and args.mode == "full" and not args.location:
            grace = args.grace_hours if args.grace_hours is not None else self._grace_hours
            deactivated = self._deactivate_stale(grace_hours=grace)

        return PipelineRunResult(
            pipeline_run_id=self.pipeline_run_id,
            source=self.SOURCE,
            records_extracted=len(raw_records),
            records_loaded=loaded,
            records_skipped=len(transformed) - loaded,
            records_failed=len(raw_records) - len(valid),
        )

    # ── Validation ───────────────────────────────────────────

    def _check_required(self, raw: dict) -> list[str]:
        """Check that all required fields have non-null values."""
        missing = []
        for field in self.REQUIRED_FIELDS:
            val = self._clean(raw.get(field))
            if val is None:
                missing.append(field)
        return missing

    # ── Transform ────────────────────────────────────────────

    def _transform(self, raw: dict, v: RecordValidator) -> dict | None:
        source_id = self._clean(raw.get(self._source_id_field))
        if not source_id:
            return None

        lid = _listing_id(self._source_name, source_id)
        zip_code = v.to_str(self._clean(raw.get("zip_code")), max_len=10)

        return {
            "listing_id": lid,
            "source": self._source_name,
            "source_native_id": source_id,
            "source_url": self._clean(raw.get("property_url")),
            "price": v.to_int(raw.get("list_price")),
            "beds": v.to_int(raw.get("beds")),
            "baths": v.to_int(raw.get("full_baths")),
            "sqft": v.to_int(raw.get("sqft")),
            "street": self._clean(raw.get("street")),
            "unit": self._clean(raw.get("unit")),
            "city": self._clean(raw.get("city")),
            "zip_code": zip_code,
            "neighborhood": v.resolve_neighborhood(zip_code),
            "lat": v.to_float(raw.get("latitude")),
            "lon": v.to_float(raw.get("longitude")),
            "description_text": self._clean(raw.get("text")),
            "primary_photo_url": self._clean(raw.get("primary_photo")),
            "mls_id": self._clean(raw.get("mls_id")),
            "mls_status": self._clean(raw.get("mls_status")),
            "days_on_mls": v.to_int(raw.get("days_on_mls")),
            "agent_name": self._clean(raw.get("agent_name")),
            "style": self._clean(raw.get("style")),
            "list_date": self._clean(raw.get("list_date")),
            "is_current": True,
            "raw_json": json.dumps(
                {k: self._clean(val) for k, val in raw.items()},
                default=str,
            ),
            "pipeline_run_id": self.pipeline_run_id,
        }

    @staticmethod
    def _clean(val) -> str | None:
        """Handle pandas NaN/NaT/None."""
        if val is None:
            return None
        s = str(val).strip()
        if s in ("", "nan", "NaT", "None", "NaN", "<NA>"):
            return None
        return s

    # ── Staging + Merge ──────────────────────────────────────

    def _create_staging_table(self) -> str:
        batch_id = self.pipeline_run_id[:8]
        table = f"RAW.LISTINGS_STAGING_{batch_id}"

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
        """Upsert: insert new, update existing with latest data.

        The UPDATE branch bumps last_seen_at on every re-fetch, which
        is what _deactivate_stale() reads to decide what's actually gone.

        neighborhood, zip_code, city are included in UPDATE so that
        changes to spatial.yml (adding new ZIP mappings) propagate on
        the next pipeline run. Without this, a listing first inserted
        with neighborhood=NULL would keep NULL forever.
        """
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
                city = src.city,
                zip_code = src.zip_code,
                neighborhood = src.neighborhood,
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

    def _deactivate_stale(self, grace_hours: int = 48) -> int:
        """Mark listings as inactive only when not seen for grace_hours.

        Uses last_seen_at (bumped by MERGE on every successful re-fetch)
        instead of pipeline_run_id. This tolerates HomeHarvest response
        drift — a single run that misses a listing won't kill it. The
        listing must remain absent across enough runs to span grace_hours
        before is_current flips to FALSE.

        Default grace_hours = 48 means a listing survives roughly two
        daily pipeline runs before deactivation, giving Realtor.com's
        non-deterministic ZIP responses time to re-include it.
        """
        self.cursor.execute("""
            UPDATE RAW.LISTINGS
            SET is_current = FALSE
            WHERE source = %s
              AND is_current = TRUE
              AND last_seen_at < DATEADD(hour, -%s, CURRENT_TIMESTAMP())
        """, (self._source_name, grace_hours))

        deactivated = self.cursor.rowcount
        self.conn.commit()

        if deactivated > 0:
            self.log.info(
                "stale_deactivated",
                count=deactivated,
                grace_hours=grace_hours,
            )

        return deactivated

    def _drop_staging_table(self, stage_table: str):
        self.cursor.execute(f"DROP TABLE IF EXISTS {stage_table}")


if __name__ == "__main__":
    pipeline = ListingsPipeline()
    result = pipeline.run()
    raise SystemExit(0 if result.status == "success" else 1)