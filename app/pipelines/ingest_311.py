"""311 complaints ingestion pipeline.

Extracts from 3 CKAN resource IDs (legacy, 2026, new system),
normalizes field names, classifies complaint types via LLM cache,
loads to RAW.COMPLAINTS_311 via staging + MERGE.

Usage:
    python -m app.pipelines.ingest_311 --mode full --limit 100 --dry-run
    python -m app.pipelines.ingest_311 --mode full
    python -m app.pipelines.ingest_311 --mode incremental
    python -m app.pipelines.ingest_311 --start-date 2026-01-01
"""

import json
import argparse

import structlog

from app.core.base_pipeline import BasePipeline, PipelineRunResult
from app.core.config_loader import load_source_config
from app.core.extractors import CKANMultiExtractor
from app.core.validator import RecordValidator
from app.core.classifier import ClassificationCache

logger = structlog.get_logger()


class Complaints311Pipeline(BasePipeline):

    SOURCE = "311"
    DESCRIPTION = "Ingest Boston 311 service requests from CKAN (3 resource IDs)"

    def __init__(self):
        super().__init__()
        self._config = load_source_config("complaints_311")
        self._fields = self._config["fields"]

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
        extractor = CKANMultiExtractor("complaints_311")

        classifier = ClassificationCache(
            cursor=self.cursor,
            pipeline_run_id=self.pipeline_run_id,
            source=self.SOURCE,
            field_name="type",
        )

        stage_table = self._create_staging_table()

        total_extracted = 0
        total_valid = 0
        total_staged = 0

        # Stream pages from all 3 variants
        for page in extractor.extract_pages(since=since,
                                            until=args.end_date,
                                            max_records=args.limit):

            total_extracted += len(page.records)

            # Validate coordinates
            valid_records = []
            for raw in page.records:
                result = validator.validate(
                    record=raw,
                    lat_field=self._fields["lat"],
                    lon_field=self._fields["lon"],
                    required=[self._fields["case_enquiry_id"]],
                )
                if result.valid:
                    valid_records.append(raw)
                else:
                    self.record_error(
                        record_key=raw.get(self._fields["case_enquiry_id"]),
                        error_type="validation",
                        error_message="; ".join(result.errors),
                        raw_record=raw,
                    )

            if not valid_records:
                continue

            total_valid += len(valid_records)

            # Classify complaint types
            type_values = [
                r.get(self._fields["type"], "") for r in valid_records
            ]
            classifications = classifier.classify(type_values)

            # Transform and stage
            transformed = [
                self._transform(raw, classifications, validator)
                for raw in valid_records
            ]
            transformed = [r for r in transformed if r]

            if not args.dry_run and transformed:
                self._stage_batch(stage_table, transformed)
                total_staged += len(transformed)

            self.log.info("page_processed",
                          page=page.page_number,
                          extracted=len(page.records),
                          valid=len(valid_records),
                          staged=len(transformed))

        # Merge or report
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
            date_column="open_dt",
        )

    # ── Transform ────────────────────────────────────────────

    def _transform(self, raw: dict, classifications: dict,
                   v: RecordValidator) -> dict | None:
        f = self._fields
        complaint_type = v.to_str(raw.get(f["type"]))
        classification = classifications.get(complaint_type, {})

        street = v.to_str(raw.get(f["street"]), max_len=200)
        neighborhood = v.to_str(raw.get(f.get("neighborhood", "neighborhood")))
        zip_code = v.to_str(raw.get(f["zip_code"]), max_len=10)

        # Enrich neighborhood from zip if missing
        if not neighborhood and zip_code:
            neighborhood = v.resolve_neighborhood(zip_code)

        type_narrative = classification.get("narrative", complaint_type or "Service request")
        parts = [type_narrative.rstrip(".")]
        if street:
            parts.append(f"at {street}")
        if neighborhood:
            parts.append(f"in {neighborhood}")
        record_narrative = " ".join(parts) + "."

        return {
            "case_enquiry_id": v.to_str(raw.get(f["case_enquiry_id"])),
            "source_resource_id": self._resolve_resource_id(raw),
            "open_dt": v.to_str(raw.get(f["open_dt"])),
            "closed_dt": v.to_str(raw.get(f.get("closed_dt", "closed_dt"))),
            "case_status": v.to_str(raw.get(f.get("case_status", "case_status")), max_len=20),
            "case_title": v.to_str(raw.get(f.get("case_title", "case_title")), max_len=100),
            "subject": v.to_str(raw.get(f.get("subject", "subject")), max_len=100),
            "reason": v.to_str(raw.get(f.get("reason", "reason")), max_len=100),
            "type": v.to_str(complaint_type, max_len=100),
            "category": classification.get("category", "other"),
            "neighborhood": v.to_str(neighborhood, max_len=100),
            "ward": v.to_str(raw.get(f.get("ward", "ward")), max_len=10),
            "street": street,
            "zip_code": zip_code,
            "lat": v.to_float(raw.get(f["lat"])),
            "lon": v.to_float(raw.get(f["lon"])),
            "classification_metadata": json.dumps({
                "severity": classification.get("severity"),
                "category": classification.get("category"),
                "narrative": record_narrative,
                "type_narrative": classification.get("narrative"),
                "source_fields": {
                    "type": complaint_type,
                    "case_title": v.to_str(raw.get(f.get("case_title", "case_title"))),
                    "subject": v.to_str(raw.get(f.get("subject", "subject"))),
                    "neighborhood": neighborhood,
                    "street": street,
                },
            }),
            "pipeline_run_id": self.pipeline_run_id,
        }

    def _resolve_resource_id(self, raw: dict) -> str:
        """Identify which resource ID this record came from."""
        # new_system records have case_id, legacy/2026 have case_enquiry_id
        if "case_id" in raw and "case_enquiry_id" not in raw:
            return self._config["variants"]["new_system"]["resource_id"]
        # Can't distinguish legacy vs 2026 from the record itself — use first match
        return self._config["variants"]["2025_legacy"]["resource_id"]

    # ── Staging + Merge ──────────────────────────────────────

    def _create_staging_table(self) -> str:
        batch_id = self.pipeline_run_id[:8]
        table = f"RAW.C311_STAGING_{batch_id}"

        self.cursor.execute(f"""
            CREATE TEMPORARY TABLE {table} (
                case_enquiry_id         VARCHAR(20),
                source_resource_id      VARCHAR(50),
                open_dt                 VARCHAR(50),
                closed_dt               VARCHAR(50),
                case_status             VARCHAR(20),
                case_title              VARCHAR(100),
                subject                 VARCHAR(100),
                reason                  VARCHAR(100),
                type                    VARCHAR(100),
                category                VARCHAR(30),
                neighborhood            VARCHAR(100),
                ward                    VARCHAR(10),
                street                  VARCHAR(200),
                zip_code                VARCHAR(10),
                lat                     FLOAT,
                lon                     FLOAT,
                classification_metadata VARCHAR,
                pipeline_run_id         VARCHAR(36)
            )
        """)

        self.log.info("staging_table_created", table=table)
        return table

    def _stage_batch(self, stage_table: str, records: list[dict]):
        sql = f"""
            INSERT INTO {stage_table} (
                case_enquiry_id, source_resource_id, open_dt, closed_dt,
                case_status, case_title, subject, reason,
                type, category, neighborhood, ward, street, zip_code,
                lat, lon, classification_metadata, pipeline_run_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        rows = [
            (
                r["case_enquiry_id"], r["source_resource_id"],
                r["open_dt"], r["closed_dt"],
                r["case_status"], r["case_title"], r["subject"], r["reason"],
                r["type"], r["category"], r["neighborhood"], r["ward"],
                r["street"], r["zip_code"], r["lat"], r["lon"],
                r["classification_metadata"], r["pipeline_run_id"],
            )
            for r in records
        ]

        self.cursor.executemany(sql, rows)

    def _merge(self, stage_table: str) -> int:
        self.cursor.execute(f"""
            MERGE INTO RAW.COMPLAINTS_311 AS target
            USING {stage_table} AS src
            ON target.case_enquiry_id = src.case_enquiry_id
            WHEN NOT MATCHED THEN INSERT (
                case_enquiry_id, source_resource_id, open_dt, closed_dt,
                case_status, case_title, subject, reason,
                type, category, neighborhood, ward, street, zip_code,
                lat, lon, classification_metadata, pipeline_run_id
            ) VALUES (
                src.case_enquiry_id, src.source_resource_id,
                src.open_dt::TIMESTAMP_NTZ, src.closed_dt::TIMESTAMP_NTZ,
                src.case_status, src.case_title, src.subject, src.reason,
                src.type, src.category, src.neighborhood, src.ward,
                src.street, src.zip_code, src.lat, src.lon,
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
    pipeline = Complaints311Pipeline()
    result = pipeline.run()
    raise SystemExit(0 if result.status == "success" else 1)