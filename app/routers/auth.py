"""Auth router — registration, login, session resume.

All credential handling lives here. The agent layer never sees passwords.
Frontend, MCP clients, and CLI tools all authenticate through these endpoints
and receive a JWT for subsequent requests.

Endpoints:
    POST /auth/register  — create account, returns JWT + user_context
    POST /auth/login     — verify credentials, returns JWT + user_context
    GET  /auth/me        — validate JWT, returns current user_context

Usage:
    # Register
    POST /auth/register
    {"email": "neha@example.com", "password": "securepass", "display_name": "Neha"}

    # Login
    POST /auth/login
    {"email": "neha@example.com", "password": "securepass"}

    # Session resume (JWT in Authorization header)
    GET /auth/me
    Authorization: Bearer <token>
"""

from __future__ import annotations

import re
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.core.auth import create_token, get_current_user
from app.core.database import get_cursor

logger = structlog.get_logger()

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    """Registration payload. Email must be valid, password >= 8 chars."""
    email: str = Field(..., max_length=255)
    password: str = Field(..., min_length=8, max_length=128)
    display_name: Optional[str] = Field(None, max_length=100)

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", v):
            raise ValueError("Invalid email address")
        return v


class LoginRequest(BaseModel):
    """Login payload. Email + password, no other fields."""
    email: str = Field(..., max_length=255)
    password: str = Field(..., min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", v):
            raise ValueError("Invalid email address")
        return v


class AuthResponse(BaseModel):
    """Successful auth response. Token + full user context for session init."""
    token: str
    user_id: str
    email: str
    display_name: Optional[str] = None
    user_context: dict


class UserContextResponse(BaseModel):
    """Session resume response. Full user context without re-authenticating."""
    user_id: str
    email: str
    display_name: Optional[str] = None
    user_context: dict


# ---------------------------------------------------------------------------
# POST /auth/register
# ---------------------------------------------------------------------------

@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new account",
    responses={
        409: {"description": "Email already registered"},
        422: {"description": "Validation error (email format, password length)"},
    },
)
def register(body: RegisterRequest, cursor=Depends(get_cursor)):
    """Register a new user account.

    Creates the user, hashes the password, generates a JWT, and loads
    the full user context (which will be empty for a new user — no
    profile, no bookmarks, no history).

    The frontend should store the returned token and include it as
    ``Authorization: Bearer <token>`` on all subsequent requests.
    """
    from app.services.user_data import create_user, load_user_session

    log = logger.bind(endpoint="register", email=body.email[:50])

    # Create user (service handles hashing + uniqueness check)
    result = create_user(
        cursor,
        email=body.email,
        password=body.password,
        display_name=body.display_name,
    )

    if not result.success:
        # Duplicate email → 409 Conflict
        if "already registered" in (result.error or "").lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email already registered",
            )
        # Other DB errors → 500
        log.error("create_failed", error=result.error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Registration failed",
        )

    user = result.data[0]
    user_id = user["user_id"]

    # Load session context (empty for a brand new user, but the
    # structure is consistent with what authenticated endpoints return)
    try:
        user_context = load_user_session(cursor, user_id)
    except ValueError:
        # Should never happen — we just created the user
        log.error("session_load_failed_after_create", user_id=user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Account created but session load failed",
        )

    # Issue JWT
    token = create_token(user_id=user_id, email=user["email"])

    log.info("registered", user_id=user_id)

    return AuthResponse(
        token=token,
        user_id=user_id,
        email=user["email"],
        display_name=user.get("display_name"),
        user_context=user_context,
    )


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------

@router.post(
    "/login",
    response_model=AuthResponse,
    summary="Log in with email and password",
    responses={
        401: {"description": "Invalid email or password"},
    },
)
def login(body: LoginRequest, cursor=Depends(get_cursor)):
    """Authenticate with email + password.

    On success, returns a JWT and the full user context (profile,
    bookmarks, session summaries). The frontend uses the context
    to populate the sidebar and personalize the chat.

    On failure, returns a generic 401 — never reveals whether the
    email exists or the password was wrong.
    """
    from app.services.user_data import authenticate_user, load_user_session

    log = logger.bind(endpoint="login")

    # Verify credentials
    result = authenticate_user(cursor, email=body.email, password=body.password)

    if not result.success:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    user = result.data[0]
    user_id = user["user_id"]

    # Load full session context
    try:
        user_context = load_user_session(cursor, user_id)
    except ValueError:
        log.error("session_load_failed", user_id=user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication succeeded but session load failed",
        )

    # Issue JWT
    token = create_token(user_id=user_id, email=user["email"])

    log.info("login_success", user_id=user_id)

    return AuthResponse(
        token=token,
        user_id=user_id,
        email=user["email"],
        display_name=user.get("display_name"),
        user_context=user_context,
    )


# ---------------------------------------------------------------------------
# GET /auth/me
# ---------------------------------------------------------------------------

@router.get(
    "/me",
    response_model=UserContextResponse,
    summary="Get current user context (session resume)",
    responses={
        401: {"description": "Missing or invalid token"},
        404: {"description": "User not found"},
    },
)
def me(user_id: str = Depends(get_current_user), cursor=Depends(get_cursor)):
    """Resume an existing session using a stored JWT.

    The frontend calls this on app load with the stored token to check
    if the session is still valid and reload the user's context (profile,
    bookmarks, history may have changed since last visit).

    No credentials needed — the JWT in the Authorization header is sufficient.
    """
    from app.services.user_data import load_user_session, get_user_by_id

    log = logger.bind(endpoint="me", user_id=user_id)

    # Verify user still exists (could have been deleted)
    user_result = get_user_by_id(cursor, user_id)
    if not user_result.success or not user_result.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    user = user_result.data[0]

    # Load full context
    try:
        user_context = load_user_session(cursor, user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    log.info("session_resumed", user_id=user_id)

    return UserContextResponse(
        user_id=user_id,
        email=user.get("email", ""),
        display_name=user.get("display_name"),
        user_context=user_context,
    )