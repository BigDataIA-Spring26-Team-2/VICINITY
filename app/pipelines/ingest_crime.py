"""Crime incident ingestion pipeline.

Extracts from Boston PD CKAN page-by-page, validates, classifies
offense severity via LLM cache, stages in batches, single MERGE.

Usage:
    python -m app.pipelines.ingest_crime --mode full --limit 100 --dry-run
    python -m app.pipelines.ingest_crime --mode full
    python -m app.pipelines.ingest_crime --mode incremental
    python -m app.pipelines.ingest_crime --start-date 2026-01-01
"""

import json
import argparse

import structlog

from app.core.base_pipeline import BasePipeline, PipelineRunResult
from app.core.config_loader import load_source_config
from app.core.extractors import CKANExtractor
from app.core.validator import RecordValidator
from app.core.classifier import ClassificationCache

logger = structlog.get_logger()


class CrimePipeline(BasePipeline):

    SOURCE = "crime"
    DESCRIPTION = "Ingest Boston PD crime incidents from CKAN"

    def __init__(self):
        super().__init__()
        self._config = load_source_config("crime")
        self._fields = self._config["fields"]
        self._required = [
            self._fields["incident_id"],
            self._fields["offense_description"],
        ]

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser):
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Max records to extract. Use for testing.",
        )

    def run_pipeline(self, args: argparse.Namespace) -> PipelineRunResult:

        since = self._resolve_watermark(args)
        validator = RecordValidator()
        extractor = CKANExtractor("crime")

        classifier = ClassificationCache(
            cursor=self.cursor,
            pipeline_run_id=self.pipeline_run_id,
            source=self.SOURCE,
            field_name="offense_description",
        )

        # Create staging table once
        stage_table = self._create_staging_table()

        total_extracted = 0
        total_valid = 0
        total_staged = 0

        # ── Process page by page ─────────────────────────────

        for page in extractor.extract_pages(since=since,
                                            until=args.end_date,
                                            max_records=args.limit):

            total_extracted += len(page.records)

            # Validate
            valid_records = []
            for raw in page.records:
                result = validator.validate(
                    record=raw,
                    lat_field=self._fields["lat"],
                    lon_field=self._fields["lon"],
                    required=self._required,
                )
                if result.valid:
                    valid_records.append(raw)
                else:
                    self.record_error(
                        record_key=raw.get(self._fields["incident_id"]),
                        error_type="validation",
                        error_message="; ".join(result.errors),
                        raw_record=raw,
                    )

            if not valid_records:
                continue

            total_valid += len(valid_records)

            # Classify — collects distinct values, cache handles dedup
            offense_values = [
                r.get(self._fields["offense_description"], "")
                for r in valid_records
            ]
            classifications = classifier.classify(offense_values)

            # Transform
            transformed = [
                self._transform(raw, classifications, validator)
                for raw in valid_records
            ]
            transformed = [r for r in transformed if r]

            # Stage
            if not args.dry_run and transformed:
                self._stage_batch(stage_table, transformed)
                total_staged += len(transformed)

            self.log.info("page_processed",
                          page=page.page_number,
                          extracted=len(page.records),
                          valid=len(valid_records),
                          staged=len(transformed))

        # ── Merge ────────────────────────────────────────────

        if args.dry_run:
            self.log.info("dry_run_complete",
                          total_extracted=total_extracted,
                          total_valid=total_valid)
            self._drop_staging_table(stage_table)
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
                records_extracted=total_extracted,
                records_loaded=0,
                records_skipped=total_extracted - total_valid,
            )

        if total_staged == 0:
            self.log.info("nothing_to_merge")
            self._drop_staging_table(stage_table)
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
                records_extracted=total_extracted,
                records_loaded=0,
            )

        loaded = self._merge(stage_table)
        skipped = total_staged - loaded
        self._drop_staging_table(stage_table)

        return PipelineRunResult(
            pipeline_run_id=self.pipeline_run_id,
            source=self.SOURCE,
            records_extracted=total_extracted,
            records_loaded=loaded,
            records_skipped=skipped,
            records_failed=total_extracted - total_valid,
        )

    # ── Watermark ────────────────────────────────────────────

    def _resolve_watermark(self, args: argparse.Namespace) -> str | None:
        if args.start_date:
            return args.start_date
        if args.mode == "full":
            return None
        return self.get_high_watermark(
            table=self._config["target_table"],
            date_column="occurred_on_date",
        )

    # ── Transform ────────────────────────────────────────────

    def _transform(self, raw: dict, classifications: dict,
                    v: RecordValidator) -> dict | None:
            f = self._fields
            offense_desc = v.to_str(raw.get(f["offense_description"]))
            classification = classifications.get(offense_desc, {})

            street = v.to_str(raw.get(f["street"]), max_len=100)
            hour = v.to_int(raw.get(f["hour"]))
            district = v.to_str(raw.get(f["district"]), max_len=5)
            day = v.to_str(raw.get(f["day_of_week"]), max_len=10)
            shooting = v.to_bool(raw.get(f["shooting"]))

            # Per-record narrative: type narrative + specific context
            type_narrative = classification.get("narrative", offense_desc or "Incident")
            parts = [type_narrative.rstrip(".")]
            if street:
                parts.append(f"on {street}")
            if hour is not None:
                parts.append(f"at {hour:02d}:00")
            if day:
                parts.append(f"on a {day.strip()}")
            if district:
                parts.append(f"in district {district}")
            if shooting:
                parts.append("— shooting involved")
            record_narrative = " ".join(parts) + "."

            return {
                "incident_id": v.to_str(raw.get(f["incident_id"])),
                "offense_code": v.to_str(raw.get(f["offense_code"]), max_len=10),
                "offense_description": v.to_str(offense_desc, max_len=100),
                "severity": classification.get("severity", "unknown"),
                "occurred_on_date": v.to_str(raw.get(f["occurred_on_date"])),
                "hour": hour,
                "day_of_week": day,
                "district": district,
                "street": street,
                "lat": v.to_float(raw.get(f["lat"])),
                "lon": v.to_float(raw.get(f["lon"])),
                "shooting": shooting,
                "classification_metadata": json.dumps({
                    "severity": classification.get("severity"),
                    "category": classification.get("category"),
                    "narrative": record_narrative,
                    "type_narrative": classification.get("narrative"),
                    "source_fields": {
                        "offense_description": offense_desc,
                        "offense_code": v.to_str(raw.get(f["offense_code"])),
                        "district": district,
                        "street": street,
                        "hour": hour,
                    },
                }),
                "source_resource_id": self._config["connection"]["resource_id"],
                "pipeline_run_id": self.pipeline_run_id,
            }
    # ── Staging + Merge ──────────────────────────────────────

    def _create_staging_table(self) -> str:
        batch_id = self.pipeline_run_id[:8]
        table = f"RAW.CRIME_STAGING_{batch_id}"

        self.cursor.execute(f"""
            CREATE TEMPORARY TABLE {table} (
                incident_id             VARCHAR(30),
                offense_code            VARCHAR(10),
                offense_description     VARCHAR(100),
                severity                VARCHAR(20),
                occurred_on_date        VARCHAR(50),
                hour                    INT,
                day_of_week             VARCHAR(10),
                district                VARCHAR(5),
                street                  VARCHAR(100),
                lat                     FLOAT,
                lon                     FLOAT,
                shooting                BOOLEAN,
                classification_metadata VARCHAR,
                source_resource_id      VARCHAR(50),
                pipeline_run_id         VARCHAR(36)
            )
        """)

        self.log.info("staging_table_created", table=table)
        return table

    def _stage_batch(self, stage_table: str, records: list[dict]):
        sql = f"""
            INSERT INTO {stage_table} (
                incident_id, offense_code, offense_description, severity,
                occurred_on_date, hour, day_of_week,
                district, street, lat, lon, shooting,
                classification_metadata, source_resource_id, pipeline_run_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        rows = [
            (
                r["incident_id"], r["offense_code"],
                r["offense_description"], r["severity"],
                r["occurred_on_date"], r["hour"], r["day_of_week"],
                r["district"], r["street"], r["lat"], r["lon"],
                r["shooting"], r["classification_metadata"],
                r["source_resource_id"], r["pipeline_run_id"],
            )
            for r in records
        ]

        self.cursor.executemany(sql, rows)

    def _merge(self, stage_table: str) -> int:
        self.cursor.execute(f"""
            MERGE INTO RAW.CRIME_INCIDENTS AS target
            USING {stage_table} AS src
            ON target.incident_id = src.incident_id
            WHEN NOT MATCHED THEN INSERT (
                incident_id, offense_code, offense_description, severity,
                occurred_on_date, hour, day_of_week,
                district, street, lat, lon, shooting,
                classification_metadata, source_resource_id, pipeline_run_id
            ) VALUES (
                src.incident_id, src.offense_code, src.offense_description,
                src.severity, src.occurred_on_date::TIMESTAMP_NTZ,
                src.hour, src.day_of_week,
                src.district, src.street, src.lat, src.lon, src.shooting,
                PARSE_JSON(src.classification_metadata),
                src.source_resource_id, src.pipeline_run_id
            )
        """)

        loaded = self.cursor.rowcount
        self.conn.commit()
        self.log.info("merge_complete", loaded=loaded)
        return loaded

    def _drop_staging_table(self, stage_table: str):
        self.cursor.execute(f"DROP TABLE IF EXISTS {stage_table}")


if __name__ == "__main__":
    pipeline = CrimePipeline()
    result = pipeline.run()
    raise SystemExit(0 if result.status == "success" else 1)