"""Health check router.

GET /healthz — full component check (Snowflake, Redis, Pinecone)
GET /ping    — lightweight liveness probe (no external calls)
"""

from fastapi import APIRouter, Request

from app.core.health import check_health

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz(request: Request):
    """Component-level health check with latency tracking.

    Pings Snowflake, Redis, Pinecone. Writes record to RAW.HEALTHZ.
    Returns 200 even on degraded — caller reads status field.
    """
    return check_health(
        client_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.get("/ping")
def ping():
    """Liveness probe. No external calls, no logging."""
    return {"status": "ok"}