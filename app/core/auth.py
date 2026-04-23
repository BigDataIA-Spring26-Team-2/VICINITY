"""Authentication utilities — password hashing, JWT tokens, FastAPI dependency.

Provides the auth foundation for all entry points (React frontend, MCP server,
terminal chat). No database access — pure cryptographic operations and token
management. Service functions in user_data.py handle the DB interaction.

Password hashing: bcrypt with automatic salt generation.
Tokens: HS256 JWT with configurable expiry, signed with JWT_SECRET from env.

Usage:
    from app.core.auth import hash_password, verify_password, create_token
    from app.core.auth import get_current_user, get_optional_user

    # Registration
    hashed = hash_password("plaintext")

    # Login
    if verify_password("plaintext", hashed):
        token = create_token(user_id="u-123")

    # FastAPI protected endpoint
    @router.get("/me")
    def me(user_id: str = Depends(get_current_user)):
        ...

    # FastAPI optional auth endpoint (anonymous allowed)
    @router.post("/chat")
    def chat(user_id: str | None = Depends(get_optional_user)):
        ...
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# JWT config: read from app.config.Settings (populated from .env).
# Lazy-loaded on first use to avoid import-time Settings() construction
# before .env is loaded in some entry points (Airflow, CLI scripts).
_JWT_ALGORITHM = "HS256"


def _jwt_secret() -> str:
    from app.config import get_settings
    return get_settings().jwt_secret


def _jwt_expiry_hours() -> int:
    from app.config import get_settings
    return get_settings().jwt_expiry_hours

# HTTPBearer scheme — auto_error=False so anonymous requests pass through
# instead of raising 403 before the endpoint even runs.
_bearer_scheme = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    """Hash a plaintext password with bcrypt. Returns the hash as a string.

    Uses bcrypt's built-in salt generation (12 rounds by default).
    The returned string includes the salt, algorithm, and hash — everything
    needed to verify later.
    """
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a bcrypt hash.

    Returns True if the password matches, False otherwise.
    Never raises on mismatch — caller decides what to do.
    """
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        # Malformed hash, encoding error, etc. — treat as mismatch.
        return False


# ---------------------------------------------------------------------------
# JWT tokens
# ---------------------------------------------------------------------------

def create_token(user_id: str, email: str) -> str:
    """Create a signed JWT containing the user's identity.

    Payload:
        sub: user_id (primary identifier for all downstream lookups)
        email: for display / debugging (not used for auth decisions)
        iat: issued-at timestamp
        exp: expiry timestamp (configurable via JWT_EXPIRY_HOURS env var)

    Returns the encoded token string.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "email": email,
        "iat": now,
        "exp": now + timedelta(hours=_jwt_expiry_hours()),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=_JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and validate a JWT. Returns the payload dict.

    Raises jwt.ExpiredSignatureError if the token has expired.
    Raises jwt.InvalidTokenError for any other validation failure.
    """
    return jwt.decode(token, _jwt_secret(), algorithms=[_JWT_ALGORITHM])


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> str:
    """FastAPI dependency — requires a valid JWT. Returns user_id.

    Use on endpoints that must be authenticated (profile writes, bookmarks, etc.).
    Raises 401 if the token is missing, expired, or invalid.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decode_token(credentials.credentials)
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: missing subject",
            )
        return user_id
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as e:
        logger.warning("invalid_token", error=str(e)[:200])
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> Optional[str]:
    """FastAPI dependency — returns user_id if authenticated, None if anonymous.

    Use on endpoints that support both modes (chat, public listing search).
    Never raises 401 — anonymous access is explicitly allowed.
    Invalid/expired tokens are treated as anonymous (logged, not rejected).
    """
    if credentials is None:
        return None

    try:
        payload = decode_token(credentials.credentials)
        return payload.get("sub")
    except jwt.ExpiredSignatureError:
        logger.debug("expired_token_treated_as_anonymous")
        return None
    except jwt.InvalidTokenError:
        logger.debug("invalid_token_treated_as_anonymous")
        return None