"""Config writer service — append search queries to pipeline YAML configs.

A dumb writer. Takes fully-formed queries and appends them to the correct
YAML file. No query generation logic — the Organizer LLM generates queries
with neighborhood context, livability/lifestyle awareness, and platform-
appropriate phrasing. This module just validates and writes.

Called from the `update_pipeline_queries` Organizer tool.

Pipeline config format:
    reddit.yml      — queries.{tag}: ["natural language search string", ...]
    google_news.yml — queries.{tag}: ["journalistic search phrase", ...]
    eventbrite.yml  — queries.{tag}: ["url-slug", ...]

Safety:
    - Existing tags and queries are NEVER modified.
    - New YAML is validated before writing — invalid writes are aborted.
    - Idempotent: existing tags are skipped.
    - Timestamped comment for auditability.

Usage:
    from app.services.config_writer import write_queries

    # Write Reddit queries for a new tag
    write_queries("reddit", "bharatanatyam", [
        "bharatanatyam classes Allston",
        "Indian classical dance near Allston Brighton",
    ])

    # Write to multiple pipelines at once
    write_queries_bulk({
        "reddit": {"bharatanatyam": ["bharatanatyam Allston", "Indian dance classes"]},
        "google_news": {"bharatanatyam": ["Boston Allston bharatanatyam", "Indian dance Boston"]},
        "eventbrite": {"bharatanatyam": ["bharatanatyam", "indian-dance"]},
    })
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import structlog
import yaml

from app.core.config_loader import CONFIG_DIR

logger = structlog.get_logger()

_SOURCES_DIR = CONFIG_DIR / "sources"
_PIPELINE_FILES = {
    "reddit": _SOURCES_DIR / "reddit.yml",
    "google_news": _SOURCES_DIR / "google_news.yml",
    "eventbrite": _SOURCES_DIR / "eventbrite.yml",
}

# Query validation constraints
_MAX_QUERY_LENGTH = 120
_MAX_QUERIES_PER_TAG = 5
_MAX_TAG_LENGTH = 50


# =====================================================================
# Validation
# =====================================================================

def _validate_tag(tag: str) -> str:
    """Normalize and validate a preference tag.

    Returns cleaned tag or raises ValueError.
    """
    tag = tag.strip().lower().replace(" ", "_")
    if not tag or len(tag) > _MAX_TAG_LENGTH:
        raise ValueError(f"Invalid tag: '{tag}' (empty or > {_MAX_TAG_LENGTH} chars)")
    if not all(c.isalnum() or c == "_" for c in tag):
        raise ValueError(f"Invalid tag: '{tag}' (only alphanumeric and underscores)")
    return tag


def _validate_queries(queries: list[str]) -> list[str]:
    """Validate and clean a list of search queries.

    Returns cleaned list or raises ValueError.
    """
    if not queries:
        raise ValueError("Empty query list")

    cleaned = []
    for q in queries[:_MAX_QUERIES_PER_TAG]:
        q = q.strip()
        if not q:
            continue
        if len(q) > _MAX_QUERY_LENGTH:
            q = q[:_MAX_QUERY_LENGTH]
        cleaned.append(q)

    if not cleaned:
        raise ValueError("No valid queries after cleaning")

    return cleaned


# =====================================================================
# YAML writer — safe, idempotent, validates before writing
# =====================================================================

def _append_to_yaml(
    config_path: Path,
    tags_with_queries: dict[str, list[str]],
) -> dict:
    """Append new tag queries to a YAML config file.

    Args:
        config_path: Path to the pipeline YAML file.
        tags_with_queries: {tag: [query1, query2, ...]}

    Returns:
        {"added": [...], "skipped": [...]}

    Raises:
        FileNotFoundError: Config file missing.
        ValueError: Validation failed (write aborted).
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    with open(config_path, encoding="utf-8") as f:
        raw_text = f.read()

    config = yaml.safe_load(raw_text)
    if not config or "queries" not in config:
        raise ValueError(f"Invalid config: {config_path}")

    existing = set(config["queries"].keys())
    added, skipped, to_write = [], [], {}

    for tag, queries in tags_with_queries.items():
        tag = _validate_tag(tag)
        if tag in existing:
            skipped.append(tag)
            continue
        queries = _validate_queries(queries)
        to_write[tag] = queries
        added.append(tag)

    if not to_write:
        return {"added": [], "skipped": skipped}

    # Build YAML lines
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    new_lines = [f"\n  # -- Added by Organizer ({now}) --"]
    for tag, queries in to_write.items():
        new_lines.append(f"  {tag}:")
        for q in queries:
            new_lines.append(f'    - "{q}"')

    # Find end of queries block
    raw_lines = raw_text.split("\n")
    insert_at = None
    in_queries = False
    for i, line in enumerate(raw_lines):
        if line.startswith("queries:"):
            in_queries = True
            continue
        if in_queries:
            if line.strip() == "" or line.startswith("  "):
                insert_at = i + 1
            else:
                break

    if insert_at is None:
        insert_at = len(raw_lines)

    result_lines = raw_lines[:insert_at] + new_lines + [""] + raw_lines[insert_at:]
    result_text = "\n".join(result_lines)

    # Validate before writing
    try:
        validated = yaml.safe_load(result_text)
        for tag in added:
            assert tag in validated.get("queries", {}), f"Missing: {tag}"
        for tag in existing:
            assert tag in validated.get("queries", {}), f"Lost: {tag}"
    except (yaml.YAMLError, AssertionError) as e:
        raise ValueError(f"YAML validation failed: {e}")

    with open(config_path, "w", encoding="utf-8") as f:
        f.write(result_text)

    return {"added": added, "skipped": skipped}


# =====================================================================
# Public API
# =====================================================================

def write_queries(pipeline: str, tag: str, queries: list[str]) -> dict:
    """Write queries for a single tag to a single pipeline config.

    Args:
        pipeline: "reddit", "google_news", or "eventbrite".
        tag: Preference tag (e.g. "bharatanatyam").
        queries: Search queries generated by the Organizer LLM.

    Returns:
        {"added": [...], "skipped": [...]} or {"error": "..."}
    """
    log = logger.bind(op="write_queries", pipeline=pipeline, tag=tag)

    path = _PIPELINE_FILES.get(pipeline)
    if not path:
        return {"error": f"Unknown pipeline: {pipeline}"}

    try:
        return _append_to_yaml(path, {tag: queries})
    except Exception as e:
        log.error("write_failed", error=str(e)[:200])
        return {"error": str(e)[:200]}


def write_queries_bulk(pipeline_queries: dict[str, dict[str, list[str]]]) -> dict:
    """Write queries for multiple tags across multiple pipelines.

    Args:
        pipeline_queries: {
            "reddit": {"tag1": ["q1", "q2"], "tag2": ["q3"]},
            "google_news": {"tag1": ["q4", "q5"]},
            "eventbrite": {"tag1": ["slug1"]},
        }

    Returns:
        Summary dict keyed by pipeline name.
    """
    log = logger.bind(op="write_queries_bulk")
    results = {}

    for pipeline, tags_queries in pipeline_queries.items():
        path = _PIPELINE_FILES.get(pipeline)
        if not path:
            results[pipeline] = {"error": f"Unknown pipeline: {pipeline}"}
            continue
        try:
            result = _append_to_yaml(path, tags_queries)
            results[pipeline] = result
            if result["added"]:
                log.info("updated", pipeline=pipeline, added=result["added"])
        except Exception as e:
            log.error("failed", pipeline=pipeline, error=str(e)[:200])
            results[pipeline] = {"error": str(e)[:200]}

    return results


def get_existing_tags() -> dict[str, set[str]]:
    """Return all existing tags across all pipeline configs.

    Useful for the Organizer to check what's already covered before
    deciding to generate new queries.

    Returns:
        {"reddit": {"safety", "noise", ...}, "google_news": {...}, ...}
    """
    result = {}
    for pipeline, path in _PIPELINE_FILES.items():
        if path.exists():
            try:
                with open(path) as f:
                    cfg = yaml.safe_load(f)
                result[pipeline] = set((cfg or {}).get("queries", {}).keys())
            except Exception:
                result[pipeline] = set()
        else:
            result[pipeline] = set()
    return result