"""Pinecone search service -- HyDE retrieval for narrative evidence.

Bridges the vocabulary gap between conversational queries ("is it
sketchy at night?") and stored records ("ASSAULT - AGGRAVATED, 11 PM").

Flow:
  1. User question arrives
  2. If HyDE enabled: LLM generates a hypothetical answer
  3. Hypothetical (or raw question if HyDE off) is embedded
  4. Pinecone query with metadata filters returns matching narratives
  5. On empty results, retry with broadened filters (configurable)
  6. Multi-query pass if enabled (rephrased angles on same question)

Usage:
    from app.services.pinecone_search import search_narratives
    results = search_narratives(
        question="is it safe to walk home at night near Allston?",
        filters={"signal_source": "crime", "neighborhoods": "Allston"},
    )
"""

from __future__ import annotations

import time
from typing import Optional

import structlog
from openai import OpenAI
from pinecone import Pinecone

from app.config import get_settings
from app.core.config_loader import CONFIG_DIR
from app.services.listing_queries import QueryResult

logger = structlog.get_logger()


# -- Config ---------------------------------------------------------------

def _cfg() -> dict:
    if not hasattr(_cfg, "_cache"):
        import yaml
        with open(CONFIG_DIR / "services.yml", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        _cfg._cache = raw.get("pinecone_search", {})
    return _cfg._cache


def reload_config():
    if hasattr(_cfg, "_cache"):
        del _cfg._cache


# -- Clients (lazy singletons) -------------------------------------------

_openai_client: Optional[OpenAI] = None
_pinecone_index = None


def _get_openai() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        settings = get_settings()
        _openai_client = OpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
        )
    return _openai_client


def _get_index():
    global _pinecone_index
    if _pinecone_index is None:
        settings = get_settings()
        pc = Pinecone(api_key=settings.pinecone_api_key.get_secret_value())
        _pinecone_index = pc.Index(settings.pinecone_index)
    return _pinecone_index


# -- HyDE generation -----------------------------------------------------

def _generate_hypothetical(question: str) -> Optional[str]:
    """Generate a hypothetical document that would answer the question.

    Uses the LLM to write a fake crime report / Reddit post / news article
    so the embedding lands near real stored narratives rather than near
    the conversational phrasing of the question.

    Returns None on failure (caller falls back to direct embedding).
    """
    cfg = _cfg()
    hyde = cfg.get("hyde", {})

    if not hyde.get("enabled", True):
        return None

    model = hyde.get("model", "deepseek-chat")
    system = hyde.get("system_prompt", "")
    temperature = hyde.get("temperature", 0.3)
    max_tokens = hyde.get("max_tokens", 200)

    log = logger.bind(service="pinecone_search", op="hyde")

    try:
        # Use ProviderChain pattern: try configured model, no fallback here
        # because this is a best-effort enhancement, not critical path
        from app.core.classifier import ProviderChain
        chain = ProviderChain()
        result = chain.complete(system=system, user=question)
        hypothetical = result["content"].strip()

        log.info("generated", model=result["model"],
                 tokens=result["total_tokens"],
                 length=len(hypothetical))
        return hypothetical

    except Exception as e:
        log.warning("failed_falling_back_to_direct", error=str(e)[:200])
        return None


# -- Embedding ------------------------------------------------------------

def _embed(text: str) -> list[float]:
    """Embed text using the same model as the sync pipeline."""
    cfg = _cfg()
    emb = cfg.get("embedding", {})
    model = emb.get("model", "text-embedding-3-small")
    dimensions = emb.get("dimensions", 1536)

    client = _get_openai()
    response = client.embeddings.create(
        model=model,
        input=[text],
        dimensions=dimensions,
    )
    return response.data[0].embedding


# -- Pinecone query -------------------------------------------------------

def _query_pinecone(
    vector: list[float],
    *,
    top_k: int,
    min_score: float,
    filters: Optional[dict] = None,
) -> list[dict]:
    """Query Pinecone with vector and metadata filters.

    Args:
        vector: Query embedding.
        top_k: Max results.
        min_score: Minimum cosine similarity.
        filters: Pinecone metadata filter dict. Keys must match stored
                 metadata: signal_source, preference_tag, sentiment,
                 neighborhoods, category, relevance_score.

    Returns:
        List of {"id", "score", "metadata"} dicts above min_score.
    """
    index = _get_index()

    # Build Pinecone filter
    pc_filter = {}
    if filters:
        for key, value in filters.items():
            if value is None:
                continue
            if isinstance(value, list):
                # List filter: e.g. neighborhoods contains "Allston"
                pc_filter[key] = {"$in": value}
            else:
                pc_filter[key] = {"$eq": value}

    result = index.query(
        vector=vector,
        top_k=top_k,
        include_metadata=True,
        filter=pc_filter if pc_filter else None,
    )

    matches = []
    for match in result.get("matches", []):
        if match["score"] >= min_score:
            matches.append({
                "id": match["id"],
                "score": round(match["score"], 4),
                "metadata": match.get("metadata", {}),
            })

    return matches


# -- Public API -----------------------------------------------------------

def search_narratives(
    question: str,
    *,
    filters: Optional[dict] = None,
    top_k: Optional[int] = None,
    min_score: Optional[float] = None,
    skip_hyde: bool = False,
) -> QueryResult:
    """Search Pinecone for narrative evidence matching a question.

    Full flow: HyDE generation -> embedding -> Pinecone query -> retry
    with broadened score threshold on empty results.

    Args:
        question: Natural language question from user/agent.
        filters: Metadata filters (signal_source, preference_tag,
                 neighborhoods, sentiment, category).
        top_k: Override retrieval count.
        min_score: Override minimum similarity threshold.
        skip_hyde: Force direct embedding (skip hypothetical generation).

    Returns:
        QueryResult with matched narratives and their metadata.
    """
    cfg = _cfg()
    ret = cfg.get("retrieval", {})
    log = logger.bind(service="pinecone_search", query="narratives")

    if not cfg.get("enabled", True):
        return QueryResult(success=False, query_type="search_narratives",
                           error="pinecone_search service is disabled")

    top_k = top_k or ret.get("top_k", 10)
    min_score = min_score or ret.get("min_score", 0.65)
    max_retries = ret.get("max_retries", 3)
    score_decay = ret.get("retry_score_decay", 0.05)

    start = time.perf_counter()

    # Step 1: HyDE or direct embedding
    embed_text = question
    hyde_used = False
    if not skip_hyde:
        hypothetical = _generate_hypothetical(question)
        if hypothetical:
            embed_text = hypothetical
            hyde_used = True

    # Step 2: Embed
    try:
        vector = _embed(embed_text)
    except Exception as e:
        log.error("embedding_failed", error=str(e)[:200])
        return QueryResult(success=False, query_type="search_narratives",
                           error=f"Embedding failed: {str(e)[:300]}")

    # Step 3: Query with retry -- lower score threshold on empty results
    matches = []
    current_score = min_score
    attempt = 0

    while attempt <= max_retries:
        matches = _query_pinecone(
            vector,
            top_k=top_k,
            min_score=current_score,
            filters=filters,
        )

        if matches:
            break

        attempt += 1
        if attempt > max_retries:
            break

        # Broaden: lower score threshold
        current_score = max(0.3, current_score - score_decay)

        log.info("retry_broadened", attempt=attempt,
                 new_min_score=current_score)

    ms = int((time.perf_counter() - start) * 1000)

    warnings = []
    if attempt > 0 and matches:
        warnings.append(
            f"Broadened search after {attempt} retries "
            f"(score threshold lowered to {current_score:.2f})."
        )
    if not matches:
        warnings.append("No matching narratives found after all retries.")

    # Format results for agent consumption
    data = []
    for m in matches:
        meta = m["metadata"]
        data.append({
            "signal_id": m["id"],
            "score": m["score"],
            "source": meta.get("signal_source", ""),
            "preference_tag": meta.get("preference_tag", ""),
            "sentiment": meta.get("sentiment", ""),
            "neighborhoods": meta.get("neighborhoods", []),
            "category": meta.get("category", ""),
            "url": meta.get("url", ""),
        })

    log.info("complete", matches=len(data), hyde=hyde_used,
             retries=attempt, ms=ms)

    return QueryResult(
        success=True, query_type="search_narratives",
        data=data, total_count=len(data), duration_ms=ms,
        warnings=warnings,
    )


# -- Multi-query search ---------------------------------------------------

def search_narratives_multi(
    questions: list[str],
    *,
    filters: Optional[dict] = None,
    top_k: Optional[int] = None,
    min_score: Optional[float] = None,
) -> QueryResult:
    """Run multiple retrieval passes and deduplicate results.

    Used when a single query might miss relevant narratives due to
    phrasing. The agent generates 2-3 angle variations and this
    function merges and deduplicates by signal_id, keeping the
    highest score per unique result.

    Args:
        questions: List of query variations (max from config).
        filters: Shared metadata filters across all passes.
        top_k: Per-query retrieval count.
        min_score: Per-query minimum threshold.
    """
    cfg = _cfg()
    mq = cfg.get("multi_query", {})
    log = logger.bind(service="pinecone_search", query="multi")

    if not mq.get("enabled", True):
        return QueryResult(success=False, query_type="search_narratives_multi",
                           error="Multi-query is disabled in config")

    max_queries = mq.get("max_queries", 3)
    deduplicate = mq.get("deduplicate", True)
    questions = questions[:max_queries]

    start = time.perf_counter()
    all_results = {}

    for q in questions:
        result = search_narratives(
            q, filters=filters, top_k=top_k, min_score=min_score,
        )
        if not result.success:
            continue

        for item in result.data:
            sid = item["signal_id"]
            if deduplicate:
                # Keep highest score per signal
                if sid not in all_results or item["score"] > all_results[sid]["score"]:
                    all_results[sid] = item
            else:
                all_results[sid] = item

    # Sort by score descending
    merged = sorted(all_results.values(), key=lambda x: x["score"], reverse=True)
    ms = int((time.perf_counter() - start) * 1000)

    log.info("complete", queries=len(questions), unique_results=len(merged), ms=ms)

    return QueryResult(
        success=True, query_type="search_narratives_multi",
        data=merged, total_count=len(merged), duration_ms=ms,
    )