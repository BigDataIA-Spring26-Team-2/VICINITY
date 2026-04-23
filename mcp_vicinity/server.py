"""Vicinity MCP Server — modular entry point.

Usage:
    python -m mcp_vicinity --transport stdio
    python -m mcp_vicinity --transport streamable-http --port 8001
    python -m mcp_vicinity --transport stdio --email neha@vicinity.app

Claude Desktop config:
    {
      "mcpServers": {
        "vicinity": {
          "command": ".../.venv/Scripts/python.exe",
          "args": ["-m", "mcp_vicinity", "--transport", "stdio"],
          "env": {"PYTHONPATH": "C:/path/to/VICINITY"}
        }
      }
    }
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import logging
import sys
from pathlib import Path
from typing import Optional

# ── Load .env BEFORE any app imports ──────────────────────────
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# ── Force ALL logging to stderr (stdio reserves stdout for JSON-RPC) ──
logging.basicConfig(
    stream=sys.stderr,
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

import structlog
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
)

from mcp.server.fastmcp import FastMCP

from mcp_vicinity.instructions import INSTRUCTIONS
from mcp_vicinity.auth import MCPSession, authenticate_by_email, authenticate_by_credentials
from mcp_vicinity.tools import register_read_tools

logger = structlog.get_logger()

_session: MCPSession = MCPSession()


# -- Pipeline lifecycle ------------------------------------------------

async def _ensure_pipeline():
    if _session.pipeline is not None:
        return _session.pipeline

    from app.agents.tools.read_tools import set_cursor_provider
    from app.core.database import _connect
    from scripts.chat import ChatPipeline

    def _cursor_factory():
        conn = _connect()
        cursor = conn.cursor()
        original_close = cursor.close
        def _close_both():
            try:
                original_close()
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        cursor.close = _close_both
        return cursor

    set_cursor_provider(_cursor_factory)

    pipeline = ChatPipeline(
        user_id=_session.user_id,
        session_id=_session.session_id,
        user_context=_session.user_context,
    )
    await pipeline.initialize()
    _session.pipeline = pipeline

    print("mcp_pipeline_ready", file=sys.stderr)
    return pipeline


async def _shutdown():
    if _session.pipeline is not None:
        try:
            await _session.pipeline.close()
        except Exception as e:
            print(f"mcp_shutdown_failed: {e}", file=sys.stderr)


# -- Server assembly ---------------------------------------------------

mcp_app = FastMCP("Vicinity", instructions=INSTRUCTIONS)
register_read_tools(mcp_app)


@mcp_app.tool()
def login(email: str, password: str) -> str:
    """Authenticate to enable write operations (bookmark, profile, routes).

    Read tools work without login. Write operations via send_message
    require authentication.

    Args:
        email: Registered email address.
        password: Account password.

    Returns:
        JSON with auth status and profile summary.
    """
    global _session

    result = authenticate_by_credentials(email, password)
    if not result.authenticated:
        return json.dumps({"success": False, "error": "Invalid email or password"})

    _session = result
    _session.pipeline = None  # rebuild on next send_message

    return json.dumps({
        "success": True,
        "user_id": result.user_id,
        "email": result.email,
        "profile": {
            "budget_min": result.user_context.get("budget_min"),
            "budget_max": result.user_context.get("budget_max"),
            "bedrooms_min": result.user_context.get("bedrooms_min"),
            "work_address": result.user_context.get("work_address"),
            "preference_tags": result.user_context.get("preference_tags", []),
            "bookmarks": len(result.user_context.get("active_bookmarks", [])),
        },
    })


@mcp_app.tool()
async def send_message(message: str) -> str:
    """Conversational agent with memory and write capabilities.

    Use for multi-turn conversations, write operations (bookmark, profile,
    routes), comparison reports, narrative search, and confirmations.

    For simple data lookups, prefer direct tools (search_listings, etc.).

    Args:
        message: Question, request, or confirmation about Boston housing.

    Returns:
        Agent response with data, analysis, or confirmation prompt.
    """
    full_response = []
    interrupt_data = None

    try:
        pipeline = await _ensure_pipeline()
        async for event in pipeline.send(message):
            t = event.get("type", "")
            if t == "token":
                full_response.append(event.get("data", {}).get("content", ""))
            elif t == "interrupt":
                interrupt_data = event.get("data", {})
            elif t == "node_end":
                c = event.get("data", {}).get("content", "")
                if c:
                    full_response.append(c)
            elif t == "error":
                return f"Error: {event.get('data', {}).get('error', 'Unknown')}"
    except Exception as e:
        print(f"mcp_send_failed: {e}", file=sys.stderr)
        return f"Error: {str(e)[:300]}"

    text = "".join(full_response)
    if interrupt_data:
        summary = interrupt_data.get("summary", "")
        text += (
            f"\n\n---\n**Pending confirmation:** {summary}\n"
            f"Reply 'yes' to approve, 'no' to cancel, or describe changes."
        )
    return text or "No response generated. Try rephrasing."


@mcp_app.resource("vicinity://status")
def get_status() -> str:
    """Session status."""
    return json.dumps({
        "authenticated": _session.authenticated,
        "user_id": _session.user_id,
        "email": _session.email,
        "session_id": _session.session_id,
        "pipeline_ready": _session.pipeline is not None,
    }, indent=2)


# -- Entry point -------------------------------------------------------

def main():
    global _session

    parser = argparse.ArgumentParser(description="Vicinity MCP Server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http", "sse"],
                        default="streamable-http")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--email", type=str, default=None)
    args = parser.parse_args()

    if args.email:
        _session = authenticate_by_email(args.email)

    def _sync_shutdown():
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_shutdown())
            else:
                loop.run_until_complete(_shutdown())
        except Exception:
            pass

    atexit.register(_sync_shutdown)

    if args.transport == "stdio":
        mcp_app.run(transport="stdio")
    else:
        mcp_app.settings.host = "0.0.0.0"
        mcp_app.settings.port = args.port
        mcp_app.run(transport=args.transport)


if __name__ == "__main__":
    main()