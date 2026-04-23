"""MCP auth — session management and JWT validation.

Handles three auth modes:
  stdio:  --email flag at startup, implicit trust (local Claude Desktop)
  token:  Bearer token in HTTP headers (remote streamable-http)
  login:  Tool-based auth for clients that can't set headers

The session holds user_id, email, and the loaded user_context.
Write tools (via send_message) require an authenticated session.
Read tools work without auth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import structlog

logger = structlog.get_logger()


@dataclass
class MCPSession:
    """Per-connection session state."""
    user_id: Optional[str] = None
    email: Optional[str] = None
    session_id: Optional[str] = None
    user_context: dict = field(default_factory=dict)
    pipeline: object = None

    @property
    def authenticated(self) -> bool:
        return bool(self.user_id)


def authenticate_by_email(email: str) -> MCPSession:
    """Load user context by email. Used for --email flag and login tool."""
    from app.core.database import _connect
    from app.services.user_data import load_user_session

    session = MCPSession(email=email)
    conn = None

    try:
        conn = _connect()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT id FROM USER_DATA.USERS WHERE email = %s",
                (email.strip().lower(),),
            )
            row = cursor.fetchone()
            if row:
                session.user_id = row[0]
                session.user_context = load_user_session(cursor, session.user_id)
                session.session_id = session.user_context.get("session_id")
                logger.info("mcp_auth_success", user_id=session.user_id, email=email)
            else:
                logger.warning("mcp_auth_user_not_found", email=email)
        finally:
            cursor.close()
    except Exception as e:
        logger.error("mcp_auth_failed", email=email, error=str(e)[:200])
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return session


def authenticate_by_token(token: str) -> MCPSession:
    """Validate a JWT and load user context. Used for HTTP Bearer auth."""
    from app.core.auth import decode_token

    session = MCPSession()

    try:
        payload = decode_token(token)
        user_id = payload.get("sub")
        email = payload.get("email", "")

        if not user_id:
            logger.warning("mcp_auth_invalid_token", reason="missing_sub")
            return session

        session.user_id = user_id
        session.email = email

        # Load full context
        from app.core.database import _connect
        from app.services.user_data import load_user_session

        conn = _connect()
        try:
            cursor = conn.cursor()
            try:
                session.user_context = load_user_session(cursor, user_id)
                session.session_id = session.user_context.get("session_id")
            finally:
                cursor.close()
        finally:
            conn.close()

        logger.info("mcp_token_auth_success", user_id=user_id)

    except Exception as e:
        logger.warning("mcp_token_auth_failed", error=str(e)[:200])

    return session


def authenticate_by_credentials(email: str, password: str) -> MCPSession:
    """Verify email + password. Used by the login tool."""
    from app.core.database import _connect
    from app.services.user_data import authenticate_user, load_user_session
    from app.core.auth import create_token

    session = MCPSession(email=email)
    conn = None

    try:
        conn = _connect()
        cursor = conn.cursor()
        try:
            result = authenticate_user(cursor, email=email, password=password)
            if not result.success:
                logger.warning("mcp_login_failed", email=email)
                return session

            user = result.data[0]
            session.user_id = user["user_id"]
            session.user_context = load_user_session(cursor, session.user_id)
            session.session_id = session.user_context.get("session_id")
            logger.info("mcp_login_success", user_id=session.user_id)
        finally:
            cursor.close()
    except Exception as e:
        logger.error("mcp_login_error", error=str(e)[:200])
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return session