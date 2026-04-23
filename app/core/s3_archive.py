"""S3 archival for pipeline runs.

Queries the target table for records matching the pipeline_run_id,
compresses as JSONL, uploads to S3. Best-effort — failures log a
warning but never block the pipeline.

Directory layout:
  s3://{bucket}/pipelines/{source}/{YYYY}/{MM}/{DD}/{run_id}.jsonl.gz

Credentials: boto3 reads AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY
from environment. No explicit credential passing.

Disable: leave S3_BUCKET empty in .env.
"""

import gzip
import json
import io
from datetime import datetime, timezone

import boto3
import structlog

from app.config import get_settings

logger = structlog.get_logger()


class S3Archiver:
    """Archive pipeline output to S3."""

    def __init__(self):
        settings = get_settings()
        self._bucket = settings.s3_bucket
        self._enabled = bool(self._bucket)

        if self._enabled:
            self._client = boto3.client(
                "s3",
                region_name=settings.aws_region,
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def archive_run(
        self,
        cursor,
        source: str,
        pipeline_run_id: str,
        target_table: str,
    ) -> str | None:
        """Query target table for this run's records and upload to S3.

        Returns the S3 key on success, None on skip/failure.
        """
        if not self._enabled:
            return None

        try:
            return self._do_archive(
                cursor, source, pipeline_run_id, target_table,
            )
        except Exception as e:
            logger.warning(
                "s3_archive_failed",
                source=source,
                pipeline_run_id=pipeline_run_id,
                error=str(e)[:200],
            )
            return None

    def _do_archive(
        self,
        cursor,
        source: str,
        pipeline_run_id: str,
        target_table: str,
    ) -> str | None:
        """Fetch records, compress to JSONL, upload."""
        cursor.execute(
            f"SELECT * FROM {target_table} "
            f"WHERE pipeline_run_id = %s",
            (pipeline_run_id,),
        )
        rows = cursor.fetchall()

        if not rows:
            logger.info(
                "s3_archive_skip_empty",
                source=source,
                pipeline_run_id=pipeline_run_id,
            )
            return None

        cols = [d[0].lower() for d in cursor.description]

        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            for row in rows:
                record = {}
                for col, val in zip(cols, row):
                    if isinstance(val, datetime):
                        record[col] = val.isoformat()
                    elif isinstance(val, bytes):
                        record[col] = val.hex()
                    elif type(val) not in (
                        str, int, float, bool, type(None), list, dict,
                    ):
                        record[col] = str(val)
                    else:
                        record[col] = val
                gz.write(json.dumps(record).encode("utf-8"))
                gz.write(b"\n")

        buf = io.BytesIO(buf.getvalue())


        now = datetime.now(timezone.utc)
        key = (
            f"pipelines/{source}/"
            f"{now.strftime('%Y/%m/%d')}/"
            f"{pipeline_run_id[:8]}.jsonl.gz"
        )

        self._client.upload_fileobj(
            buf, self._bucket, key,
            ExtraArgs={"ContentType": "application/gzip"},
        )

        size_kb = buf.tell() // 1024
        logger.info(
            "s3_archive_complete",
            source=source,
            key=key,
            records=len(rows),
            size_kb=size_kb,
        )
        return key