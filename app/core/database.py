"""Snowflake database dependency for FastAPI.

Usage:
    from app.core.database import get_cursor, get_conn

    @router.get("/listings")
    def search_listings(cursor=Depends(get_cursor)):
        cursor.execute("SELECT ...")
        return cursor.fetchall()

Connection is created per request, closed on response.

Startup:
    run_migrations() executes alembic upgrade head.
    Idempotent — no-op if schema is current, catches up if behind.
"""

from typing import Generator
from contextlib import contextmanager

import snowflake.connector
import structlog

from app.config import get_settings

logger = structlog.get_logger()


def _connect() -> snowflake.connector.SnowflakeConnection:
    """Create a Snowflake connection from settings."""
    settings = get_settings()
    return snowflake.connector.connect(
        account=settings.snowflake_account,
        user=settings.snowflake_user,
        password=settings.snowflake_password.get_secret_value(),
        database=settings.snowflake_database,
        warehouse=settings.snowflake_warehouse,
        role=settings.snowflake_role,
    )


def get_cursor() -> Generator:
    """FastAPI dependency — yields a cursor, closes connection on exit."""
    conn = _connect()
    cursor = conn.cursor()
    try:
        yield cursor
    finally:
        cursor.close()
        conn.close()


def get_conn() -> Generator:
    """FastAPI dependency — yields a connection with auto-commit off.

    Use when you need transaction control (commit/rollback).
    """
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def snowflake_cursor():
    """Context manager for use outside FastAPI (scripts, CLI).

    with snowflake_cursor() as cursor:
        cursor.execute("SELECT ...")
    """
    conn = _connect()
    cursor = conn.cursor()
    try:
        yield cursor
    finally:
        cursor.close()
        conn.close()


def run_migrations():
    """Run alembic upgrade head on app startup.

    Idempotent — if schema is current, this is a no-op.
    If behind, it catches up automatically.
    """
    from alembic.config import Config
    from alembic import command

    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, "head")
    logger.info("migrations_complete")