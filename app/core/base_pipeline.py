"""Base pipeline — shared infrastructure for all Vicinity pipelines.

Handles: run tracking, structured logging, Snowflake connection,
timing, error recording, and graceful shutdown.
"""

import uuid
import time
import json
import argparse
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

import structlog
import snowflake.connector
from pydantic import BaseModel, Field

from app.config import get_settings


# ── Structured logging setup ─────────────────────────────────

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
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
    error: Optional[str] = None


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
    - timing and result tracking
    - CLI argument parsing with --mode support
    """

    # Subclasses must set these
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
        """Log a failed record for dead-letter storage."""
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
                         error_message=error_message)

    def _flush_errors(self):
        """Write accumulated errors to RAW.PIPELINE_ERRORS."""
        if not self._errors:
            return

        for err in self._errors:
            self.cursor.execute(
                "INSERT INTO RAW.PIPELINE_ERRORS "
                "(id, pipeline_run_id, source, record_key, error_type, error_message, raw_record) "
                "SELECT %s, %s, %s, %s, %s, %s, PARSE_JSON(%s)",
                (
                    str(uuid.uuid4()),
                    err.pipeline_run_id,
                    err.source,
                    err.record_key,
                    err.error_type,
                    err.error_message,
                    json.dumps(err.raw_record) if err.raw_record else None,
                ),
            )

        self.log.info("errors_flushed", count=len(self._errors))

    # ── High watermark ───────────────────────────────────────

    def get_high_watermark(self, table: str, date_column: str) -> Optional[str]:
        """Get the latest date from a table for incremental extraction."""
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
        """Base CLI arguments. Subclasses extend via add_arguments()."""
        parser = argparse.ArgumentParser(description=cls.DESCRIPTION)
        parser.add_argument(
            "--mode",
            choices=["full", "incremental"],
            default="incremental",
            help="full: backfill all data. incremental: only new records since last run.",
        )
        parser.add_argument(
            "--start-date",
            type=str,
            default=None,
            help="Override start date for extraction (YYYY-MM-DD). Ignores high watermark.",
        )
        parser.add_argument(
            "--end-date",
            type=str,
            default=None,
            help="Override end date for extraction (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Extract and validate only. Do not write to Snowflake.",
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
        """Implement pipeline logic. Connection is already open."""
        ...

    def run(self, args: Optional[argparse.Namespace] = None) -> PipelineRunResult:
        """Entry point. Manages connection lifecycle, timing, error flushing."""
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

            result.duration_ms = int((time.perf_counter() - start) * 1000)
            result.records_failed = len(self._errors)
            self.log.info("pipeline_complete", **result.model_dump())

        except Exception as e:
            result.status = "failed"
            result.error = str(e)
            result.duration_ms = int((time.perf_counter() - start) * 1000)
            self.log.error("pipeline_failed", error=str(e), **result.model_dump())
            raise

        finally:
            self._disconnect()

        return result