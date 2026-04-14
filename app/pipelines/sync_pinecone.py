"""Pinecone vector sync — embed lifestyle signals and upsert.

Reads RAW.LIFESTYLE_SIGNALS, embeds snippet_text via OpenAI,
upserts to Pinecone with metadata for filtered retrieval.
Tracks sync state in RAW.EMBEDDING_SYNC for idempotent runs.

Delta logic:
  - incremental: only new or content-changed records
  - full: re-embed everything
  - --gc: delete Pinecone vectors for removed signals

Usage:
    python -m app.pipelines.sync_pinecone --dry-run
    python -m app.pipelines.sync_pinecone --preference-tag safety
    python -m app.pipelines.sync_pinecone --mode full --force-reembed
    python -m app.pipelines.sync_pinecone --gc
"""

import json
import argparse
from typing import Optional

import structlog
from openai import OpenAI
from pinecone import Pinecone, ServerlessSpec

from app.config import get_settings
from app.core.base_pipeline import BasePipeline, PipelineRunResult
from app.core.config_loader import load_classification
from app.core.classifier import CostTracker

logger = structlog.get_logger()


class PineconeSyncPipeline(BasePipeline):
    """Idempotent Pinecone sync for lifestyle signal embeddings.

    Shared index, per-tag metadata filtering at query time.
    content_hash comparison prevents redundant embedding calls.
    """

    SOURCE = "pinecone_sync"
    DESCRIPTION = "Embed lifestyle signals and sync to Pinecone."

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser):
        parser.add_argument(
            "--preference-tag", type=str, default=None,
            help="Sync only signals for this tag. Default: all tags.",
        )
        parser.add_argument(
            "--batch-size", type=int, default=None,
            help="Override embedding batch size from config.",
        )
        parser.add_argument(
            "--force-reembed", action="store_true",
            help="Ignore content_hash, re-embed all matching signals.",
        )
        parser.add_argument(
            "--gc", action="store_true",
            help="Delete Pinecone vectors for signals no longer in Snowflake.",
        )

    def run_pipeline(self, args: argparse.Namespace) -> PipelineRunResult:
        settings = get_settings()
        emb_config = load_classification().get("embedding", {})

        model = emb_config.get("model", "text-embedding-3-small")
        dimensions = emb_config.get("dimensions", 1536)
        batch_size = args.batch_size or emb_config.get("batch_size", 50)
        max_chars = emb_config.get("max_input_chars", 8000)

        # ── Clients ───────────────────────────────────────────
        openai_client = OpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
        )
        pc = Pinecone(
            api_key=settings.pinecone_api_key.get_secret_value(),
        )
        index = self._ensure_index(
            pc, settings.pinecone_index, dimensions,
            settings.pinecone_cloud, settings.pinecone_region,
        )
        cost = CostTracker(self.cursor, self.pipeline_run_id, self.SOURCE)

        self.log.info(
            "sync_config",
            model=model, dimensions=dimensions,
            batch_size=batch_size, index=settings.pinecone_index,
            preference_tag=args.preference_tag,
            force_reembed=args.force_reembed,
        )

        # ── Garbage collection (optional, separate path) ─────
        if args.gc:
            deleted = self._gc(index)
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
                records_extracted=deleted,
                records_loaded=deleted,
            )

        # ── Query delta ───────────────────────────────────────
        records = self._query_delta(
            args.mode, args.preference_tag, args.force_reembed,
        )

        if not records:
            self.log.info("nothing_to_sync")
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
            )

        self.log.info("delta_queried", records=len(records))

        # ── Embed + upsert in batches ─────────────────────────
        total_embedded = 0
        total_upserted = 0
        sync_rows = []

        for i in range(0, len(records), batch_size):
            batch = records[i : i + batch_size]

            # Compose embedding inputs
            texts = [
                self._compose_input(r, max_chars) for r in batch
            ]

            # Embed
            try:
                emb_result = openai_client.embeddings.create(
                    model=model,
                    input=texts,
                    dimensions=dimensions,
                )
            except Exception as e:
                self.log.error(
                    "embedding_failed",
                    batch_start=i, batch_size=len(batch),
                    error=str(e)[:200],
                )
                for r in batch:
                    self.record_error(
                        record_key=r["signal_id"],
                        error_type="embedding",
                        error_message=str(e)[:200],
                    )
                continue

            # Log cost
            cost.log_usage(
                {
                    "model": model,
                    "provider": "openai",
                    "input_tokens": emb_result.usage.prompt_tokens,
                    "output_tokens": 0,
                    "total_tokens": emb_result.usage.total_tokens,
                    "duration_ms": 0,
                },
                operation="embed_signals",
                batch_size=len(batch),
            )
            total_embedded += len(batch)

            if args.dry_run:
                self.log.info(
                    "dry_run_batch",
                    batch=i // batch_size + 1,
                    embedded=len(batch),
                    tokens=emb_result.usage.total_tokens,
                    sample_id=batch[0]["signal_id"][:16],
                )
                continue

            # Build Pinecone vectors
            vectors = []
            for j, record in enumerate(batch):
                meta = self._build_metadata(record)
                vectors.append({
                    "id": record["signal_id"],
                    "values": emb_result.data[j].embedding,
                    "metadata": meta,
                })
                sync_rows.append((
                    record["signal_id"],
                    record["content_hash"],
                    model,
                    dimensions,
                ))

            # Upsert to Pinecone
            try:
                index.upsert(vectors=vectors)
                total_upserted += len(vectors)
                self.log.info(
                    "batch_upserted",
                    batch=i // batch_size + 1,
                    vectors=len(vectors),
                )
            except Exception as e:
                self.log.error(
                    "upsert_failed",
                    batch_start=i, error=str(e)[:200],
                )
                sync_rows = sync_rows[: -len(vectors)]
                for r in batch:
                    self.record_error(
                        record_key=r["signal_id"],
                        error_type="upsert",
                        error_message=str(e)[:200],
                    )
                continue

        # ── Update EMBEDDING_SYNC ─────────────────────────────
        if sync_rows and not args.dry_run:
            self._update_sync(sync_rows)

        self.log.info(
            "sync_complete",
            delta=len(records),
            embedded=total_embedded,
            upserted=total_upserted,
        )

        return PipelineRunResult(
            pipeline_run_id=self.pipeline_run_id,
            source=self.SOURCE,
            records_extracted=len(records),
            records_loaded=total_upserted,
            records_skipped=len(records) - total_embedded,
        )

    # ── Index bootstrap ──────────────────────────────────────

    @staticmethod
    def _ensure_index(
        pc: Pinecone,
        name: str,
        dimensions: int,
        cloud: str,
        region: str,
    ):
        """Connect to Pinecone index, creating it if it doesn't exist."""
        existing = [i.name for i in pc.list_indexes()]
        if name not in existing:
            pc.create_index(
                name=name,
                dimension=dimensions,
                metric="cosine",
                spec=ServerlessSpec(cloud=cloud, region=region),
            )
            logger.info(
                "pinecone_index_created",
                name=name, dims=dimensions,
                cloud=cloud, region=region,
            )
        return pc.Index(name)

    # ── Delta query ───────────────────────────────────────────

    def _query_delta(
        self,
        mode: str,
        preference_tag: Optional[str],
        force_reembed: bool,
    ) -> list[dict]:
        """Fetch signals needing embedding. Returns list of dicts."""

        if mode == "full" or force_reembed:
            sql = """
                SELECT signal_id, signal_source, preference_tag,
                       title, snippet_text, sentiment, relevance_score,
                       url, content_hash, classification_metadata
                FROM RAW.LIFESTYLE_SIGNALS
                WHERE snippet_text IS NOT NULL
                  AND LENGTH(TRIM(snippet_text)) > 0
            """
        else:
            sql = """
                SELECT ls.signal_id, ls.signal_source, ls.preference_tag,
                       ls.title, ls.snippet_text, ls.sentiment,
                       ls.relevance_score, ls.url, ls.content_hash,
                       ls.classification_metadata
                FROM RAW.LIFESTYLE_SIGNALS ls
                LEFT JOIN RAW.EMBEDDING_SYNC es
                    ON ls.signal_id = es.signal_id
                WHERE ls.snippet_text IS NOT NULL
                  AND LENGTH(TRIM(ls.snippet_text)) > 0
                  AND (es.signal_id IS NULL
                       OR es.content_hash != ls.content_hash)
            """

        params = []
        if preference_tag:
            alias = "ls." if "ls." in sql else ""
            sql += f" AND {alias}preference_tag = %s"
            params.append(preference_tag)

        self.cursor.execute(sql, params if params else None)
        columns = [desc[0].lower() for desc in self.cursor.description]
        return [dict(zip(columns, row)) for row in self.cursor.fetchall()]

    # ── Embedding input composition ──────────────────────────

    @staticmethod
    def _compose_input(record: dict, max_chars: int) -> str:
        """Build embedding text from title + narrative."""
        title = str(record.get("title") or "").strip()
        snippet = str(record.get("snippet_text") or "").strip()
        text = f"{title}\n{snippet}" if title else snippet
        return text[:max_chars]

    # ── Pinecone metadata ────────────────────────────────────

    @staticmethod
    def _build_metadata(record: dict) -> dict:
        """Extract Pinecone-filterable metadata from record."""
        raw_meta = record.get("classification_metadata")
        if isinstance(raw_meta, str):
            try:
                parsed = json.loads(raw_meta)
            except (json.JSONDecodeError, TypeError):
                parsed = {}
        elif isinstance(raw_meta, dict):
            parsed = raw_meta
        else:
            parsed = {}

        neighborhoods = parsed.get("neighborhoods_mentioned", [])
        if isinstance(neighborhoods, str):
            neighborhoods = [neighborhoods]

        return {
            "signal_source": str(record.get("signal_source") or ""),
            "preference_tag": str(record.get("preference_tag") or ""),
            "sentiment": str(record.get("sentiment") or ""),
            "relevance_score": int(record.get("relevance_score") or 0),
            "neighborhoods": neighborhoods,
            "category": str(parsed.get("category") or ""),
            "url": str(record.get("url") or "")[:500],
            "content_hash": str(record.get("content_hash") or ""),
        }

    # ── EMBEDDING_SYNC update ────────────────────────────────

    def _update_sync(self, rows: list[tuple]):
        """MERGE sync state into RAW.EMBEDDING_SYNC."""
        batch_id = self.pipeline_run_id[:8]
        stage = f"RAW.EMBSYNC_STAGING_{batch_id}"

        self.cursor.execute(f"""
            CREATE TEMPORARY TABLE {stage} (
                signal_id       VARCHAR(64),
                content_hash    VARCHAR(64),
                embedding_model VARCHAR(50),
                vector_dim      INT
            )
        """)

        sql = f"""
            INSERT INTO {stage}
                (signal_id, content_hash, embedding_model, vector_dim)
            VALUES (%s, %s, %s, %s)
        """
        self.cursor.executemany(sql, rows)

        self.cursor.execute(f"""
            MERGE INTO RAW.EMBEDDING_SYNC AS tgt
            USING {stage} AS src ON tgt.signal_id = src.signal_id
            WHEN MATCHED THEN UPDATE SET
                content_hash    = src.content_hash,
                embedding_model = src.embedding_model,
                vector_dim      = src.vector_dim,
                embedded_at     = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (signal_id, content_hash, embedding_model, vector_dim)
            VALUES
                (src.signal_id, src.content_hash,
                 src.embedding_model, src.vector_dim)
        """)
        self.conn.commit()
        self.cursor.execute(f"DROP TABLE IF EXISTS {stage}")
        self.log.info("sync_table_updated", rows=len(rows))

    # ── Garbage collection ───────────────────────────────────

    def _gc(self, index) -> int:
        """Delete Pinecone vectors for signals removed from Snowflake."""
        self.cursor.execute("""
            SELECT es.signal_id
            FROM RAW.EMBEDDING_SYNC es
            LEFT JOIN RAW.LIFESTYLE_SIGNALS ls
                ON es.signal_id = ls.signal_id
            WHERE ls.signal_id IS NULL
        """)
        orphans = [row[0] for row in self.cursor.fetchall()]

        if not orphans:
            self.log.info("gc_no_orphans")
            return 0

        self.log.info("gc_found_orphans", count=len(orphans))

        for i in range(0, len(orphans), 1000):
            batch = orphans[i : i + 1000]
            index.delete(ids=batch)

        placeholders = ",".join(["%s"] * len(orphans))
        self.cursor.execute(
            f"DELETE FROM RAW.EMBEDDING_SYNC "
            f"WHERE signal_id IN ({placeholders})",
            orphans,
        )
        self.conn.commit()
        self.log.info("gc_complete", deleted=len(orphans))
        return len(orphans)


if __name__ == "__main__":
    pipeline = PineconeSyncPipeline()
    result = pipeline.run()
    raise SystemExit(0 if result.status == "success" else 1)