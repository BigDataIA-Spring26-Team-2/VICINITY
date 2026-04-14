"""Reddit config validator — check subreddits exist, prune dead entries.

Pre-flight for ingest_reddit.  Hits /r/{sub}/about.json for each
subreddit in reddit.yml.  Removes 404s via surgical text replacement
(preserves comments and all other YAML content).

Optionally validates queries return >0 results (--check-queries).

Wired as the first task in the ingest_reddit DAG:
    validate_reddit_config >> load_reddit_signals

Usage:
    python -m app.pipelines.validate_reddit_config --dry-run
    python -m app.pipelines.validate_reddit_config
    python -m app.pipelines.validate_reddit_config --check-queries --dry-run
"""

import argparse
import time
from pathlib import Path

import httpx
import structlog

from app.core.config_loader import load_source_config

logger = structlog.get_logger()
log = logger.bind(component="reddit_config_validator")

CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "sources" / "reddit.yml"
)


# ── Validation checks ───────────────────────────────────────

def validate_subreddit(
    sub: str, headers: dict, timeout: int = 10,
) -> bool:
    """Return True if subreddit exists (200 from about.json)."""
    try:
        resp = httpx.get(
            f"https://old.reddit.com/r/{sub}/about.json",
            headers=headers, timeout=timeout, follow_redirects=True,
        )
        return resp.status_code == 200
    except (httpx.TimeoutException, httpx.RequestError):
        return True  # fail-open: network error → don't remove


def validate_query(
    sub: str, query: str, headers: dict, timeout: int = 10,
) -> int:
    """Return result count for a limit-1 search. -1 on error."""
    try:
        resp = httpx.get(
            f"https://old.reddit.com/r/{sub}/search.json",
            params={"q": query, "restrict_sr": "on", "limit": 1, "t": "year"},
            headers=headers, timeout=timeout, follow_redirects=True,
        )
        if resp.status_code != 200:
            return -1
        data = resp.json()
        return len(data.get("data", {}).get("children", []))
    except (httpx.TimeoutException, httpx.RequestError):
        return -1


# ── YAML surgery ─────────────────────────────────────────────

def rewrite_subreddits(
    config_path: Path,
    valid_subs: list[str],
    removed_subs: list[str],
):
    """Replace livability_subreddits list in-place, preserving everything else.

    Strategy: find the `livability_subreddits:` line, eat subsequent
    list-item lines (  - "..."), write valid subs, then resume copying.
    Comments above/below the block are untouched.
    """
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    new_lines: list[str] = []
    in_block = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("livability_subreddits:"):
            in_block = True
            new_lines.append(line)
            for sub in valid_subs:
                new_lines.append(f'  - "{sub}"\n')
            if removed_subs:
                new_lines.append(
                    f"  # Removed by validator: {', '.join(removed_subs)}\n"
                )
            continue

        if in_block:
            if stripped.startswith("- "):
                continue  # skip old list items
            in_block = False

        new_lines.append(line)

    config_path.write_text("".join(new_lines), encoding="utf-8")


# ── Entrypoint ───────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Validate Reddit config subreddits and queries",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report only, don't modify config",
    )
    parser.add_argument(
        "--check-queries", action="store_true",
        help="Also validate each query returns results",
    )
    args = parser.parse_args()

    config = load_source_config("reddit")
    conn = config.get("connection", {})
    headers = {"User-Agent": conn.get(
        "user_agent", "Vicinity/1.0 (educational research)",
    )}

    # ── Validate subreddits ──────────────────────────────────
    subreddits = config.get("livability_subreddits", [])
    log.info("validating_subreddits", count=len(subreddits))

    valid: list[str] = []
    removed: list[str] = []

    for sub in subreddits:
        exists = validate_subreddit(sub, headers)
        if exists:
            valid.append(sub)
            log.info("subreddit_ok", sub=sub)
        else:
            removed.append(sub)
            log.warning("subreddit_404", sub=sub)
        time.sleep(1)

    # ── Validate queries (optional) ──────────────────────────
    if args.check_queries:
        test_sub = valid[0] if valid else "boston"
        queries = config.get("queries", {})
        for tag, q_list in queries.items():
            for q in q_list:
                count = validate_query(test_sub, q, headers)
                level = "ok" if count > 0 else (
                    "empty" if count == 0 else "error"
                )
                log.info("query_checked", tag=tag, query=q,
                         results=count, status=level)
                time.sleep(1)

    # ── Report + write ───────────────────────────────────────
    log.info("validation_complete",
             valid=len(valid), removed=len(removed),
             valid_subs=valid, removed_subs=removed)

    if removed and not args.dry_run:
        rewrite_subreddits(CONFIG_PATH, valid, removed)
        log.info("config_updated", path=str(CONFIG_PATH))
    elif removed and args.dry_run:
        log.info("dry_run_would_remove", subs=removed)
    else:
        log.info("config_clean_no_changes")


if __name__ == "__main__":
    main()
