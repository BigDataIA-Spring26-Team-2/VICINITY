"""Per-request structlog capture for streaming logs over SSE.

The chat SSE stream currently shows only the event types yielded by
ChatPipeline.send() — route, node_start, tool_start, token, etc. But
the uvicorn terminal sees much more: every structlog.info() in every
service that runs during that turn (listing_queries.query_complete,
amenity_lookup.complete, scorecard_history.query_complete, and so on).

This module bridges the two. Install ``capture_logs_processor`` in the
structlog processor chain once at app startup. Then, when a chat
request starts streaming, it calls ``start_log_capture()`` to bind an
asyncio.Queue to the current async task via ContextVar. Every
structlog event fired from that task (and from threads spawned via
asyncio.to_thread, which copies the context) gets pushed into the
queue. The router merges that queue with the pipeline event stream.

Outside a chat request the ContextVar is None and the processor is a
no-op — zero overhead on every other log call in the system.

Usage:

    # app/main.py (or wherever structlog.configure() lives)
    from app.core.log_streaming import capture_logs_processor
    structlog.configure(processors=[
        ...,
        capture_logs_processor,     # BEFORE the final renderer
        structlog.dev.ConsoleRenderer(),
    ])

    # app/routers/chat.py (inside the SSE generator)
    queue = start_log_capture()
    try:
        # pipeline.send() logs flow into queue automatically
        ...
    finally:
        stop_log_capture()
"""

from __future__ import annotations

import asyncio
import contextvars
from typing import Optional


# Bound per async task. Propagates through awaits and into tasks
# created inside the capturing context.
_log_queue_cv: contextvars.ContextVar[Optional[asyncio.Queue]] = \
    contextvars.ContextVar("vicinity_log_queue", default=None)


def _jsonable(v) -> bool:
    """Cheap check — skip values that won't serialize cleanly over SSE."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return True
    if isinstance(v, (list, tuple, dict)):
        return True
    return False


def capture_logs_processor(_logger, method_name: str, event_dict: dict) -> dict:
    """Structlog processor — forwards events to the active per-task queue.

    Must be installed in the structlog processor chain BEFORE any
    final renderer (ConsoleRenderer / JSONRenderer). No-op outside
    a capturing context (common case).
    """
    queue = _log_queue_cv.get()
    if queue is None:
        return event_dict

    try:
        payload = {
            k: v for k, v in event_dict.items()
            if _jsonable(v) and not k.startswith("_")
        }
        payload["_level"] = method_name
        queue.put_nowait(payload)
    except asyncio.QueueFull:
        # Drop the event rather than block the logger. Chat UI is
        # best-effort — losing a log row is preferable to blocking
        # an actual service call.
        pass
    except Exception:
        # Never propagate errors from the logging path.
        pass

    return event_dict


def start_log_capture(maxsize: int = 2048) -> asyncio.Queue:
    """Activate log capture on the current async task.

    Returns the queue that will receive log events. The caller is
    responsible for draining it (usually by merging into an SSE stream)
    and calling ``stop_log_capture()`` in a finally block.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    _log_queue_cv.set(queue)
    return queue


def stop_log_capture() -> None:
    """Deactivate log capture on the current async task."""
    _log_queue_cv.set(None)


def current_log_queue() -> Optional[asyncio.Queue]:
    """Introspection hook — returns the active queue if any."""
    return _log_queue_cv.get()