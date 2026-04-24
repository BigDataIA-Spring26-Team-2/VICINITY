"""Chat router — SSE streaming endpoint for the Vicinity agent graph.

Two modes:
    Anonymous:     No Authorization header. Read tools only. Organizer
                   returns a "please sign in" message on write attempts.
    Authenticated: Valid JWT. Full context loaded. All tools available.

Session lifecycle:
    1. Every turn writes user + assistant messages to USER_DATA.CONVERSATIONS
       via ChatPipeline._persist_exchange (authenticated only).
    2. Every N messages (configured by summary_trigger_count, default 20)
       a session summary is LLM-generated and written.
    3. ChatPipeline.close() writes a FINAL summary. This is called in
       three places to make sure no session exits without closure:
           - DELETE /chat/session/{id}  — explicit client close (logout)
           - _evict_stale()             — LRU eviction when cap is hit
           - FastAPI lifespan shutdown  — process-wide drain on stop

Endpoints:
    POST   /chat/send                — send message, receive SSE event stream
    POST   /chat/resume              — resume from an organizer interrupt
    DELETE /chat/session/{session_id} — close a session, flush final summary

SSE event types:
    route, node_start, node_end, tool_start, tool_end, token,
    interrupt, done, error, stream_end,
    log       — structlog event from any service during this turn

TOKEN PACING:
    LangGraph's astream_events sometimes fires a burst of on_chat_model_stream
    events back-to-back after a tool call completes (the model already
    generated those chunks internally while we were waiting on the tool
    result). If we just dump them onto the wire, they hit the client in
    a single TCP frame and React sees 50 state updates in one tick,
    which auto-batches into one render = "everything at once."

    We fix this with TOKEN_STAGGER_SEC — a small asyncio.sleep after each
    token is placed on the queue. This spaces out the burst over real
    wall-clock time so each frame hits the client as its own paint.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.auth import get_optional_user
from app.core.database import get_cursor
from app.core.log_streaming import start_log_capture, stop_log_capture

logger = structlog.get_logger()

router = APIRouter()


# ---------------------------------------------------------------------------
# Pacing config
# ---------------------------------------------------------------------------

# Small delay inserted between token queue-puts. 12ms (0.012s) gives us
# a max of ~80 tokens/sec — fast enough to feel like live generation,
# slow enough that React can commit each render and the user sees a
# smooth typewriter effect instead of a single dump.
#
# If you ever find the stream is capped at this rate when the model
# is actually producing fewer tokens/sec, the sleep has no effect
# (get() already waits on whatever arrives). This only caps BURSTS.
TOKEN_STAGGER_SEC = 0.012


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
# Session store
# ---------------------------------------------------------------------------

_sessions: dict[str, dict] = {}
_MAX_SESSIONS = 200


async def _close_pipeline_safe(session_id: str, entry: dict) -> None:
    try:
        await entry["pipeline"].close()
    except Exception as e:
        logger.warning(
            "pipeline_close_failed",
            session_id=session_id, error=str(e)[:200],
        )


async def _evict_stale() -> None:
    if len(_sessions) <= _MAX_SESSIONS:
        return

    sorted_keys = sorted(
        _sessions.keys(),
        key=lambda k: _sessions[k].get("last_used", 0),
    )
    to_evict = sorted_keys[: len(sorted_keys) // 2]

    for key in to_evict:
        entry = _sessions.pop(key, None)
        if entry:
            await _close_pipeline_safe(key, entry)

    logger.info(
        "sessions_evicted",
        evicted=len(to_evict), remaining=len(_sessions),
    )


async def close_all_sessions() -> None:
    if not _sessions:
        return
    logger.info("sessions_draining_on_shutdown", count=len(_sessions))
    items = list(_sessions.items())
    _sessions.clear()
    for session_id, entry in items:
        await _close_pipeline_safe(session_id, entry)
    logger.info("sessions_drained")


async def _get_or_create_pipeline(
    session_id: Optional[str],
    user_id: Optional[str],
    cursor,
) -> tuple:
    """Get existing pipeline or create a new one. Returns (pipeline, session_id, is_new)."""
    from app.agents.tools.read_tools import set_cursor_provider
    from app.core.database import _connect

    def _cursor_factory():
        conn = _connect()
        return conn.cursor()

    set_cursor_provider(_cursor_factory)

    if session_id and session_id in _sessions:
        entry = _sessions[session_id]
        entry["last_used"] = time.time()
        return entry["pipeline"], session_id, False

    if not session_id:
        session_id = str(uuid.uuid4())

    user_context = {}
    if user_id:
        try:
            from app.services.user_data import load_user_session
            user_context = load_user_session(cursor, user_id)
        except ValueError:
            logger.warning("user_not_found_falling_back_anonymous", user_id=user_id)
            user_id = None

    from scripts.chat import ChatPipeline

    pipeline = ChatPipeline(
        user_id=user_id,
        session_id=session_id,
        user_context=user_context,
    )
    await pipeline.initialize()

    _sessions[session_id] = {
        "pipeline": pipeline,
        "user_id": user_id,
        "last_used": time.time(),
    }
    await _evict_stale()

    return pipeline, session_id, True


# ---------------------------------------------------------------------------
# SSE streaming — merges pipeline events with captured structlog events
# ---------------------------------------------------------------------------

async def _stream_events(pipeline, message: str, session_id: str):
    """Yield SSE-formatted events from the pipeline AND from any structlog
    event fired by any service during this turn.

    Token pacing:
        Tokens are staggered with TOKEN_STAGGER_SEC between queue puts
        to prevent post-tool burst coalescing on the client side.
        Non-token events are not paced — they stream as fast as they arrive.
    """
    log_queue = start_log_capture(maxsize=4096)

    async def _drain_pipeline():
        try:
            async for event in pipeline.send(message):
                await log_queue.put({"__p__": True, "event": event})
                # Only pace token events. Other events (tool_start, tool_end,
                # route, etc) go as fast as they arrive — they're sparse
                # anyway and pacing them would add latency with no benefit.
                if event.get("type") == "token":
                    await asyncio.sleep(TOKEN_STAGGER_SEC)
        except Exception as e:
            logger.error("stream_error", session_id=session_id, error=str(e)[:200])
            await log_queue.put({"__p__": True, "event": {
                "type": "error", "data": {"error": str(e)[:500]},
            }})
        finally:
            await log_queue.put(None)

    task = asyncio.create_task(_drain_pipeline())

    try:
        while True:
            item = await log_queue.get()
            if item is None:
                break
            if isinstance(item, dict) and item.get("__p__"):
                payload = json.dumps(item["event"], default=str)
            else:
                payload = json.dumps({"type": "log", "data": item}, default=str)
            yield f"data: {payload}\n\n"

        # Drain any remaining logs
        while not log_queue.empty():
            try:
                leftover = log_queue.get_nowait()
                if leftover is None:
                    continue
                if isinstance(leftover, dict) and leftover.get("__p__"):
                    payload = json.dumps(leftover["event"], default=str)
                else:
                    payload = json.dumps({"type": "log", "data": leftover}, default=str)
                yield f"data: {payload}\n\n"
            except asyncio.QueueEmpty:
                break
    finally:
        stop_log_capture()
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    yield f"data: {json.dumps({'type': 'stream_end', 'data': {}})}\n\n"


# ---------------------------------------------------------------------------
# POST /chat/send
# ---------------------------------------------------------------------------

@router.post(
    "/send",
    summary="Send a chat message and receive streaming response",
    responses={200: {"description": "SSE event stream",
                     "content": {"text/event-stream": {}}}},
)
async def send_message(
    body: ChatSendRequest,
    user_id: Optional[str] = Depends(get_optional_user),
    cursor=Depends(get_cursor),
):
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
            "X-Accel-Buffering": "no",       # disable nginx buffering if any
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
        200: {"description": "SSE event stream with write result",
              "content": {"text/event-stream": {}}},
        404: {"description": "Session not found"},
    },
)
async def resume_interrupt(
    body: ChatResumeRequest,
    user_id: Optional[str] = Depends(get_optional_user),
):
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
    entry["last_used"] = time.time()
    pipeline = entry["pipeline"]

    log.info("chat_resume")

    return StreamingResponse(
        _stream_events(pipeline, body.response, body.session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Session-Id": body.session_id,
        },
    )


# ---------------------------------------------------------------------------
# DELETE /chat/session/{session_id}
# ---------------------------------------------------------------------------

@router.delete(
    "/session/{session_id}",
    summary="Close a chat session — flushes final summary to Snowflake",
)
async def end_session(
    session_id: str,
    user_id: Optional[str] = Depends(get_optional_user),
):
    entry = _sessions.pop(session_id, None)
    if not entry:
        return {"success": True, "existed": False}

    await _close_pipeline_safe(session_id, entry)
    logger.info("session_closed_by_client", session_id=session_id)
    return {"success": True, "existed": True}