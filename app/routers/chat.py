"""Chat router — SSE streaming endpoint for the Vicinity agent graph.

Two modes:
    Anonymous:     No Authorization header. Read tools only. Organizer
                   returns a "please sign in" message on write attempts.
    Authenticated: Valid JWT. Full context loaded. All tools available.

The chat is stateful per session. The server maintains a ChatPipeline
instance per (user_id, session_id) pair. LangGraph's MemorySaver
checkpointer preserves conversation state within a session.

Endpoints:
    POST /chat/send    — send a message, receive SSE event stream
    POST /chat/resume  — resume from an organizer interrupt (approve/reject)

SSE event types (same as scripts/chat.py):
    token         — streaming text chunk
    tool_start    — tool invocation started
    tool_end      — tool invocation completed
    interrupt     — organizer waiting for confirmation
    done          — turn complete with stats
    error         — unrecoverable error
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.auth import get_optional_user
from app.core.database import get_cursor

logger = structlog.get_logger()

router = APIRouter()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ChatSendRequest(BaseModel):
    """Chat message from the user."""
    message: str = Field(..., min_length=1, max_length=10000)
    session_id: Optional[str] = Field(
        None,
        description="Session ID for conversation continuity. "
                    "Omit to start a new session.",
    )


class ChatResumeRequest(BaseModel):
    """Resume from an organizer interrupt (approve or reject a write)."""
    session_id: str
    thread_id: str
    response: str = Field(
        ...,
        description="User's response: 'yes', 'no', 'confirm', 'cancel', etc.",
    )


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

# In-memory session store. Keyed by session_id → ChatPipeline instance.
# Production would use Redis or a session backend. For a capstone project
# with single-server deployment, this is sufficient.
#
# TTL cleanup: stale sessions evicted when store exceeds max size.
_sessions: dict[str, dict] = {}
_MAX_SESSIONS = 200


def _evict_stale():
    """Remove oldest sessions if the store exceeds capacity."""
    if len(_sessions) <= _MAX_SESSIONS:
        return
    # Sort by last_used, drop oldest half
    sorted_keys = sorted(
        _sessions.keys(),
        key=lambda k: _sessions[k].get("last_used", 0),
    )
    for key in sorted_keys[: len(sorted_keys) // 2]:
        _sessions.pop(key, None)
    logger.info("sessions_evicted", remaining=len(_sessions))


async def _get_or_create_pipeline(
    session_id: str,
    user_id: Optional[str],
    cursor,
) -> tuple:
    """Get existing pipeline or create a new one.

    Returns (pipeline, session_id, is_new).
    """
    from app.agents.tools.read_tools import set_cursor_provider
    from app.core.database import _connect

    # Cursor provider: creates a fresh Snowflake cursor per tool invocation.
    # Each tool closes its cursor after use. The provider must return a new
    # one each time, not reuse the request cursor.
    def _cursor_factory():
        conn = _connect()
        return conn.cursor()

    set_cursor_provider(_cursor_factory)

    # Check for existing session
    if session_id and session_id in _sessions:
        entry = _sessions[session_id]
        import time
        entry["last_used"] = time.time()
        return entry["pipeline"], session_id, False

    # New session
    if not session_id:
        session_id = str(uuid.uuid4())

    # Build user context
    user_context = {}
    if user_id:
        try:
            from app.services.user_data import load_user_session
            user_context = load_user_session(cursor, user_id)
        except ValueError:
            logger.warning("user_not_found_falling_back_anonymous", user_id=user_id)
            user_id = None

    # Import ChatPipeline — deferred to avoid circular imports at module level
    from scripts.chat import ChatPipeline

    pipeline = ChatPipeline(
        user_id=user_id,
        session_id=session_id,
        user_context=user_context,
    )
    await pipeline.initialize()

    import time
    _sessions[session_id] = {
        "pipeline": pipeline,
        "user_id": user_id,
        "last_used": time.time(),
    }
    _evict_stale()

    return pipeline, session_id, True


# ---------------------------------------------------------------------------
# SSE streaming helper
# ---------------------------------------------------------------------------

async def _stream_events(pipeline, message: str, session_id: str):
    """Yield SSE-formatted events from the chat pipeline.

    Each event is a Server-Sent Event with format:
        data: {"type": "...", "data": {...}}\n\n

    The frontend's EventSource or fetch+ReadableStream parses these.
    """
    try:
        async for event in pipeline.send(message):
            payload = json.dumps(event, default=str)
            yield f"data: {payload}\n\n"
    except Exception as e:
        logger.error("stream_error", session_id=session_id, error=str(e)[:200])
        error_event = json.dumps({
            "type": "error",
            "data": {"error": str(e)[:500]},
        })
        yield f"data: {error_event}\n\n"

    # Final event to signal stream end (helps frontend close cleanly)
    yield f"data: {json.dumps({'type': 'stream_end', 'data': {}})}\n\n"


# ---------------------------------------------------------------------------
# POST /chat/send
# ---------------------------------------------------------------------------

@router.post(
    "/send",
    summary="Send a chat message and receive streaming response",
    responses={
        200: {
            "description": "SSE event stream",
            "content": {"text/event-stream": {}},
        },
    },
)
async def send_message(
    body: ChatSendRequest,
    user_id: Optional[str] = Depends(get_optional_user),
    cursor=Depends(get_cursor),
):
    """Send a message to the Vicinity agent and receive a streaming response.

    Anonymous mode (no Authorization header):
        Read tools work. Write attempts return a "please sign in" message.
        Session state is maintained for the duration of the session_id.

    Authenticated mode (valid JWT):
        Full user context loaded. All tools available.
        Conversation history persisted across sessions.

    Returns a Server-Sent Event stream. Events are JSON objects with
    ``type`` and ``data`` fields. See module docstring for event types.
    """
    log = logger.bind(
        endpoint="chat_send",
        user_id=user_id or "anonymous",
        session_id=body.session_id or "new",
    )

    pipeline, session_id, is_new = await _get_or_create_pipeline(
        session_id=body.session_id,
        user_id=user_id,
        cursor=cursor,
    )

    log.info("chat_send", message_len=len(body.message), new_session=is_new)

    return StreamingResponse(
        _stream_events(pipeline, body.message, session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Session-Id": session_id,
        },
    )


# ---------------------------------------------------------------------------
# POST /chat/resume
# ---------------------------------------------------------------------------

@router.post(
    "/resume",
    summary="Resume from an organizer interrupt",
    responses={
        200: {
            "description": "SSE event stream with write result",
            "content": {"text/event-stream": {}},
        },
        404: {"description": "Session not found"},
    },
)
async def resume_interrupt(
    body: ChatResumeRequest,
    user_id: Optional[str] = Depends(get_optional_user),
):
    """Resume the agent graph after an organizer interrupt.

    When the organizer proposes a write (bookmark, profile update, etc.),
    the graph pauses and emits an ``interrupt`` event. The frontend shows
    an approve/reject UI. The user's choice comes back through this endpoint.

    The response is the same SSE stream as /chat/send — the organizer
    executes (or cancels) the write and the chat agent synthesizes the result.
    """
    log = logger.bind(
        endpoint="chat_resume",
        session_id=body.session_id,
        response=body.response[:50],
    )

    if body.session_id not in _sessions:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found. It may have expired.",
        )

    entry = _sessions[body.session_id]
    pipeline = entry["pipeline"]

    import time
    entry["last_used"] = time.time()

    log.info("chat_resume")

    return StreamingResponse(
        _stream_events(pipeline, body.response, body.session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Session-Id": body.session_id,
        },
    )