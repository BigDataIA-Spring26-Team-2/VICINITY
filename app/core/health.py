"""Health check — component-level status with latency tracking.

Usage:
    from app.core.health import check_health

    # From FastAPI endpoint:
    result = check_health(
        client_ip=request.client.host,
        user_agent=request.headers.get("user-agent"),
    )

    # Returns:
    {
        "status": "ok",
        "response_ms": 87,
        "components": {
            "snowflake": {"status": "ok", "ms": 45},
            "redis": {"status": "ok", "ms": 2},
            "pinecone": {"status": "ok", "ms": 40},
        }
    }

Writes every check to RAW.HEALTHZ for audit trail.
Component failures degrade status to "degraded" — only total
Snowflake failure returns "down".
"""

import time
import uuid
import json

import structlog

logger = structlog.get_logger()


def _check_snowflake() -> dict:
    """Ping Snowflake with a trivial query."""
    try:
        import snowflake.connector
        from app.config import get_settings

        settings = get_settings()
        start = time.perf_counter()
        conn = snowflake.connector.connect(
            account=settings.snowflake_account,
            user=settings.snowflake_user,
            password=settings.snowflake_password.get_secret_value(),
            database=settings.snowflake_database,
            warehouse=settings.snowflake_warehouse,
            role=settings.snowflake_role,
        )
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.close()
        conn.close()
        ms = int((time.perf_counter() - start) * 1000)
        return {"status": "ok", "ms": ms}
    except Exception as e:
        return {"status": "down", "ms": 0, "error": str(e)[:100]}


def _check_redis() -> dict:
    """Ping Redis via the cache singleton."""
    try:
        from app.core.cache import get_cache

        cache = get_cache()
        healthy, ms = cache.ping()
        if healthy:
            return {"status": "ok", "ms": ms}
        return {"status": "down", "ms": 0}
    except Exception as e:
        return {"status": "down", "ms": 0, "error": str(e)[:100]}


def _check_pinecone() -> dict:
    """Verify Pinecone index is reachable."""
    try:
        import os
        import httpx

        api_key = os.getenv("PINECONE_API_KEY", "")
        if not api_key:
            return {"status": "skip", "ms": 0, "error": "no api key"}

        start = time.perf_counter()
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(
                "https://api.pinecone.io/indexes",
                headers={"Api-Key": api_key},
            )
            resp.raise_for_status()
        ms = int((time.perf_counter() - start) * 1000)
        return {"status": "ok", "ms": ms}
    except Exception as e:
        return {"status": "down", "ms": 0, "error": str(e)[:100]}


def _write_healthz(
    status: str,
    response_ms: int,
    components: dict,
    client_ip: str | None,
    user_agent: str | None,
):
    """Write health check record to RAW.HEALTHZ. Best-effort."""
    try:
        import snowflake.connector
        from app.config import get_settings

        settings = get_settings()
        conn = snowflake.connector.connect(
            account=settings.snowflake_account,
            user=settings.snowflake_user,
            password=settings.snowflake_password.get_secret_value(),
            database=settings.snowflake_database,
            warehouse=settings.snowflake_warehouse,
            role=settings.snowflake_role,
        )
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO RAW.HEALTHZ "
            "(id, status, client_ip, user_agent, response_ms, details) "
            "VALUES (%s, %s, %s, %s, %s, PARSE_JSON(%s))",
            (
                str(uuid.uuid4()),
                status,
                client_ip,
                user_agent,
                response_ms,
                json.dumps(components, default=str),
            ),
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logger.warning("healthz_write_failed", error=str(e)[:100])


def check_health(
    client_ip: str | None = None,
    user_agent: str | None = None,
    write_record: bool = True,
) -> dict:
    """Run all component checks. Returns structured health report.

    Status logic:
        - All ok         -> "ok"
        - Snowflake down -> "down" (nothing works without it)
        - Others down    -> "degraded" (core queries still work)
    """
    start = time.perf_counter()

    components = {
        "snowflake": _check_snowflake(),
        "redis": _check_redis(),
        "pinecone": _check_pinecone(),
    }

    response_ms = int((time.perf_counter() - start) * 1000)

    # Snowflake is critical — everything else is degraded
    if components["snowflake"]["status"] != "ok":
        status = "down"
    elif any(
        c["status"] not in ("ok", "skip")
        for name, c in components.items()
        if name != "snowflake"
    ):
        status = "degraded"
    else:
        status = "ok"

    result = {
        "status": status,
        "response_ms": response_ms,
        "components": components,
    }

    if write_record:
        _write_healthz(status, response_ms, components, client_ip, user_agent)

    logger.info(
        "health_check",
        status=status,
        response_ms=response_ms,
        snowflake=components["snowflake"]["status"],
        redis=components["redis"]["status"],
        pinecone=components["pinecone"]["status"],
    )

    return result