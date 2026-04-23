"""Base pipeline — shared infrastructure for all Vicinity pipelines.

Handles: run tracking, structured logging, Snowflake connection,
timing, error recording, S3 archival, and graceful shutdown.
"""

import uuid
import time
import json
import argparse
from abc import ABC, abstractmethod
from typing import Optional
import sys
import os
import structlog
import snowflake.connector
from pydantic import BaseModel
from dotenv import load_dotenv

from app.config import get_settings

# Load .env once at import — all pipelines inherit this
load_dotenv()


# ── Structured logging setup ─────────────────────────────────

renderer = (
    structlog.dev.ConsoleRenderer()
    if sys.stderr.isatty() or os.environ.get("VICINITY_LOG_FORMAT") == "console"
    else structlog.processors.JSONRenderer()
)

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        renderer,
    ],
    wrapper_class=structlog.make_filtering_bound_logger(0),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)


# ── Pydantic models ─────────────────────────────────────────

class PipelineRunResult(BaseModel):
    """Outcome of a single pipeline execution."""
    pipeline_run_id: str
    source: str
    status: str = "success"
    records_extracted: int = 0
    records_loaded: int = 0
    records_skipped: int = 0
    records_failed: int = 0
    duration_ms: int = 0
    error_message: Optional[str] = None


class ErrorRecord(BaseModel):
    """A single failed record for dead-letter logging."""
    pipeline_run_id: str
    source: str
    record_key: Optional[str] = None
    error_type: str
    error_message: str
    raw_record: Optional[dict] = None


# ── Base pipeline ────────────────────────────────────────────

class BasePipeline(ABC):
    """Abstract base for all Vicinity pipelines.

    Subclasses implement `run_pipeline()`. This class provides:
    - pipeline_run_id generation
    - Snowflake connection lifecycle
    - structlog bound to run context
    - error recording to RAW.PIPELINE_ERRORS
    - S3 archival of loaded records (best-effort)
    - timing and result tracking
    - CLI argument parsing with --mode support
    """

    SOURCE: str = ""
    DESCRIPTION: str = ""

    def __init__(self):
        self.pipeline_run_id = str(uuid.uuid4())
        self.log = structlog.get_logger().bind(
            pipeline_run_id=self.pipeline_run_id,
            source=self.SOURCE,
        )
        self._conn = None
        self._cursor = None
        self._errors: list[ErrorRecord] = []

    # ── Snowflake connection ─────────────────────────────────

    def _connect(self):
        settings = get_settings()
        self._conn = snowflake.connector.connect(
            account=settings.snowflake_account,
            user=settings.snowflake_user,
            password=settings.snowflake_password.get_secret_value(),
            database=settings.snowflake_database,
            warehouse=settings.snowflake_warehouse,
            role=settings.snowflake_role,
            schema="RAW",
        )
        self._cursor = self._conn.cursor()
        self.log.info("snowflake_connected")

    def _disconnect(self):
        if self._cursor:
            self._cursor.close()
        if self._conn:
            self._conn.close()
        self.log.info("snowflake_disconnected")

    @property
    def cursor(self):
        if not self._cursor:
            raise RuntimeError("Pipeline not connected. Call run(), not run_pipeline() directly.")
        return self._cursor

    @property
    def conn(self):
        if not self._conn:
            raise RuntimeError("Pipeline not connected. Call run(), not run_pipeline() directly.")
        return self._conn

    # ── Error handling ───────────────────────────────────────

    def record_error(self, record_key: Optional[str], error_type: str,
                     error_message: str, raw_record: Optional[dict] = None):
        err = ErrorRecord(
            pipeline_run_id=self.pipeline_run_id,
            source=self.SOURCE,
            record_key=record_key,
            error_type=error_type,
            error_message=error_message,
            raw_record=raw_record,
        )
        self._errors.append(err)
        self.log.warning("record_error",
                         record_key=record_key,
                         error_type=error_type,
                         msg=error_message)

    def _flush_errors(self):
            if not self._errors:
                return

            # Aggregate by error type
            summary = {}
            for err in self._errors:
                key = err.error_type
                if key not in summary:
                    summary[key] = {"count": 0, "sample_keys": []}
                summary[key]["count"] += 1
                if len(summary[key]["sample_keys"]) < 5:
                    summary[key]["sample_keys"].append(err.record_key)

            self.cursor.execute(
                "INSERT INTO RAW.PIPELINE_ERRORS "
                "(id, pipeline_run_id, source, record_key, error_type, error_message, raw_record) "
                "SELECT %s, %s, %s, %s, %s, %s, PARSE_JSON(%s)",
                (
                    str(uuid.uuid4()),
                    self.pipeline_run_id,
                    self.SOURCE,
                    None,
                    "summary",
                    f"{len(self._errors)} total errors",
                    json.dumps(summary),
                ),
            )

            self.log.info("errors_flushed",
                        total=len(self._errors),
                        breakdown=summary)

    # ── S3 archival ──────────────────────────────────────────

    def _archive_to_s3(self, result: "PipelineRunResult"):
        """Archive this run's loaded records to S3. Best-effort.

        Resolves target_table from pipeline config or class attribute.
        Skips silently if S3 is not configured or table is unknown.
        """
        target_table = None
        if hasattr(self, "_config") and isinstance(self._config, dict):
            target_table = self._config.get("target_table")
        if not target_table:
            target_table = getattr(self, "TARGET_TABLE", None)
        if not target_table:
            return

        from app.core.s3_archive import S3Archiver
        archiver = S3Archiver()
        if archiver.enabled:
            archiver.archive_run(
                self.cursor,
                self.SOURCE,
                self.pipeline_run_id,
                target_table,
            )

    # ── High watermark ───────────────────────────────────────

    def get_high_watermark(self, table: str, date_column: str) -> Optional[str]:
        self.cursor.execute(f"SELECT MAX({date_column}) FROM {table}")
        row = self.cursor.fetchone()
        if row and row[0]:
            self.log.info("high_watermark", table=table, value=str(row[0]))
            return str(row[0])
        self.log.info("high_watermark_empty", table=table)
        return None

    # ── CLI argument parsing ─────────────────────────────────

    @classmethod
    def build_arg_parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(description=cls.DESCRIPTION)
        parser.add_argument(
            "--mode",
            choices=["full", "incremental"],
            default="incremental",
            help="full: backfill all data. incremental: only new records.",
        )
        parser.add_argument(
            "--start-date",
            type=str,
            default=None,
            help="Override start date (YYYY-MM-DD). Ignores high watermark.",
        )
        parser.add_argument(
            "--end-date",
            type=str,
            default=None,
            help="Override end date (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Extract and validate only. No writes to Snowflake.",
        )
        cls.add_arguments(parser)
        return parser

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser):
        """Override in subclasses to add pipeline-specific CLI args."""
        pass

    # ── Main execution ───────────────────────────────────────

    @abstractmethod
    def run_pipeline(self, args: argparse.Namespace) -> PipelineRunResult:
        ...

    def run(self, args: Optional[argparse.Namespace] = None) -> PipelineRunResult:
        if args is None:
            parser = self.build_arg_parser()
            args = parser.parse_args()

        self.log.info("pipeline_start",
                      mode=args.mode,
                      dry_run=args.dry_run,
                      start_date=args.start_date,
                      end_date=args.end_date)

        start = time.perf_counter()
        result = PipelineRunResult(
            pipeline_run_id=self.pipeline_run_id,
            source=self.SOURCE,
        )

        try:
            self._connect()
            result = self.run_pipeline(args)
            result.pipeline_run_id = self.pipeline_run_id

            if not args.dry_run:
                self._flush_errors()

                if result.records_loaded > 0:
                    self._archive_to_s3(result)

            result.duration_ms = int((time.perf_counter() - start) * 1000)
            result.records_failed = len(self._errors)
            self.log.info("pipeline_complete", **result.model_dump())

        except Exception as e:
            result.status = "failed"
            result.error_message = str(e)
            result.duration_ms = int((time.perf_counter() - start) * 1000)
            result.records_failed = len(self._errors)
            self.log.error("pipeline_failed", **result.model_dump())
            raise

        finally:
            self._disconnect()

        return result