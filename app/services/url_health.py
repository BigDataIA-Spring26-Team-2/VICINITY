"""URL health service -- flag, validate, and track broken URLs.

Two entry points:
  flag_url()      -- user reports a broken link via agent
  validate_url()  -- HEAD-request a URL and update its status in Snowflake

The preflight DAG task calls validate_flagged_urls() to batch-check
all flagged URLs that haven't been rechecked within the revalidation
interval.

Usage:
    from app.services.url_health import flag_url, validate_url
    with snowflake_cursor() as cursor:
        flag_url(cursor, table="RAW.LISTINGS", record_id="abc", url="https://...")
        validate_url("https://realtor.com/listing/123")
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import httpx
import structlog

from app.core.config_loader import CONFIG_DIR
from app.services.listing_queries import QueryResult

logger = structlog.get_logger()


# -- Config ------------------------------------------------------

def _cfg() -> dict:
    if not hasattr(_cfg, "_cache"):
        import yaml
        with open(CONFIG_DIR / "services.yml", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        _cfg._cache = raw.get("url_health", {})
    return _cfg._cache


def reload_config():
    if hasattr(_cfg, "_cache"):
        del _cfg._cache


# -- URL Validation (no DB) --------------------------------------

def validate_url(url: str) -> dict:
    """HEAD-request a URL. Returns status dict. No database interaction.

    Returns:
        {"url": str, "alive": bool, "status_code": int|None,
         "duration_ms": int, "error": str|None}
    """
    cfg = _cfg()
    val = cfg.get("validation", {})
    timeout = val.get("timeout", 10)
    max_retries = val.get("max_retries", 2)
    retry_delay = val.get("retry_delay", 3.0)
    valid_codes = set(val.get("valid_status_codes", [200, 301, 302, 307, 308]))
    user_agent = val.get("user_agent", "VicinityBot/1.0")

    log = logger.bind(service="url_health", op="validate")
    last_error = None
    last_code = None

    for attempt in range(max_retries + 1):
        start = time.perf_counter()
        try:
            resp = httpx.head(
                url,
                timeout=timeout,
                headers={"User-Agent": user_agent},
                follow_redirects=True,
            )
            ms = int((time.perf_counter() - start) * 1000)
            last_code = resp.status_code

            if resp.status_code in valid_codes:
                log.info("alive", url=url[:80], status=resp.status_code, ms=ms)
                return {
                    "url": url, "alive": True,
                    "status_code": resp.status_code,
                    "duration_ms": ms, "error": None,
                }

            # Non-valid status -- retry
            last_error = f"HTTP {resp.status_code}"

        except httpx.TimeoutException:
            ms = int((time.perf_counter() - start) * 1000)
            last_error = "timeout"
        except Exception as e:
            ms = int((time.perf_counter() - start) * 1000)
            last_error = str(e)[:200]

        if attempt < max_retries:
            time.sleep(retry_delay)

    log.info("dead", url=url[:80], last_code=last_code, error=last_error)
    return {
        "url": url, "alive": False,
        "status_code": last_code,
        "duration_ms": ms, "error": last_error,
    }


# -- Flag URL (agent-facing) ------------------------------------

def flag_url(
    cursor,
    table: str,
    record_id: str,
    url: str,
) -> QueryResult:
    """User reports a broken URL. Updates url_status and optionally validates.

    Resolves the target table and columns from config. If auto_check_on_flag
    is enabled, immediately HEAD-checks the URL and sets status accordingly.
    Otherwise sets status to 'flagged'.
    """
    cfg = _cfg()
    flag_cfg = cfg.get("flagging", {})
    log = logger.bind(service="url_health", op="flag")

    if not cfg.get("enabled", True):
        return QueryResult(success=False, query_type="flag_url",
                           error="url_health service is disabled")

    # Resolve table config
    targets = cfg.get("targets", [])
    target = next((t for t in targets if t["table"] == table), None)
    if not target:
        return QueryResult(
            success=False, query_type="flag_url",
            error=f"Table '{table}' not configured for URL health checks"
        )

    url_col = target["url_column"]
    id_col = target["id_column"]
    auto_check = flag_cfg.get("auto_check_on_flag", True)

    start = time.perf_counter()

    # Determine new status
    if auto_check:
        check = validate_url(url)
        new_status = "active" if check["alive"] else "confirmed_dead"
    else:
        new_status = "flagged"

    try:
        cursor.execute(
            f"UPDATE {table} SET url_status = %s, url_flagged_at = %s "
            f"WHERE {id_col} = %s",
            (
                new_status,
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                record_id,
            ),
        )
        affected = cursor.rowcount
        ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        log.error("failed", table=table, record_id=record_id,
                  error=str(e)[:200])
        return QueryResult(success=False, query_type="flag_url",
                           error=str(e)[:500])

    if affected == 0:
        return QueryResult(success=False, query_type="flag_url",
                           error=f"Record {record_id} not found in {table}")

    log.info("complete", table=table, record_id=record_id,
             new_status=new_status, ms=ms)

    return QueryResult(
        success=True, query_type="flag_url",
        data=[{
            "record_id": record_id,
            "new_status": new_status,
            "auto_checked": auto_check,
        }],
        total_count=1, duration_ms=ms,
    )


# -- Batch Validation (DAG preflight) ---------------------------

def validate_flagged_urls(cursor) -> QueryResult:
    """Check all flagged URLs that are due for revalidation.

    Called by the preflight DAG task. For each configured table,
    finds rows where url_status='flagged' and url_flagged_at is older
    than the revalidation interval, HEAD-checks each, and updates status.

    Returns summary of checks performed.
    """
    cfg = _cfg()
    flag_cfg = cfg.get("flagging", {})
    targets = cfg.get("targets", [])
    log = logger.bind(service="url_health", op="batch_validate")

    if not cfg.get("enabled", True):
        return QueryResult(success=False, query_type="batch_validate",
                           error="url_health service is disabled")

    interval_hours = flag_cfg.get("revalidation_interval_hours", 24)
    results = []

    for target in targets:
        table = target["table"]
        url_col = target["url_column"]
        id_col = target["id_column"]

        try:
            cursor.execute(
                f"SELECT {id_col}, {url_col} FROM {table} "
                f"WHERE url_status = 'flagged' "
                f"AND (url_flagged_at IS NULL OR "
                f"     url_flagged_at < DATEADD(hour, -{interval_hours}, CURRENT_TIMESTAMP()))"
            )
            rows = cursor.fetchall()
        except Exception as e:
            log.error("fetch_failed", table=table, error=str(e)[:200])
            continue

        for record_id, url in rows:
            if not url:
                continue

            check = validate_url(url)
            new_status = "active" if check["alive"] else "confirmed_dead"

            try:
                cursor.execute(
                    f"UPDATE {table} SET url_status = %s, url_flagged_at = %s "
                    f"WHERE {id_col} = %s",
                    (
                        new_status,
                        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        record_id,
                    ),
                )
            except Exception as e:
                log.error("update_failed", table=table, record_id=record_id,
                          error=str(e)[:200])
                continue

            results.append({
                "table": table,
                "record_id": record_id,
                "url": url[:100],
                "alive": check["alive"],
                "new_status": new_status,
            })

    log.info("batch_complete", checked=len(results),
             alive=sum(1 for r in results if r["alive"]),
             dead=sum(1 for r in results if not r["alive"]))

    return QueryResult(
        success=True, query_type="batch_validate",
        data=results, total_count=len(results),
    )