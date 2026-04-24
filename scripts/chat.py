"""Vicinity chat pipeline — core orchestration + terminal interface.

ChatPipeline is the reusable core used by three entry points:
  - FastAPI chat router  (app/routers/chat.py)
  - MCP server           (future)
  - Terminal interface    (this file's __main__)

Capabilities:
  - Anonymous mode: read-only tools, no user_id required.
  - Authenticated mode: full context (profile, bookmarks, summaries),
    all tools including writes via Organizer.
  - Mid-session upgrade: anonymous → authenticated without reconnecting.
  - Streams graph execution with full step observability (10 event types).
  - Persists every exchange to USER_DATA.CONVERSATIONS (authenticated only).
  - Logs per-turn LLM token usage + cost to RAW.LLM_USAGE_LOG
    (authenticated only — matches _persist_exchange semantics).
  - Generates session summaries at configurable message threshold.
  - Handles organizer interrupt/resume for write confirmations.
  - Cursor provider creates isolated connections per tool call — no shared
    connection state, no silent failures on connection drop.

Event types yielded by send():
  route        — input gate classified the intent
  node_start   — agent node began execution
  node_end     — non-streaming node produced output (block, organizer guard)
  tool_start   — tool invocation started (name + summarized args)
  tool_end     — tool invocation completed (size + error flag)
  token        — streaming text chunk from the chat agent
  interrupt    — organizer waiting for user confirmation
  done         — turn complete with timing + tool stats + LLM usage
  error        — unrecoverable error
  stream_end   — final sentinel (helps HTTP/SSE clients close cleanly)

Usage:
    # Terminal
    python -m scripts.chat                                     # anonymous
    python -m scripts.chat --email neha@example.com            # authenticated
    python -m scripts.chat --user-id u-123                     # direct ID
    python -m scripts.chat --email neha@example.com -m "Show listings"
    python -m scripts.chat --verbose                           # show all events

    # Programmatic (from router / MCP)
    pipeline = ChatPipeline()                                  # anonymous
    pipeline = ChatPipeline(user_id="u-123", user_context={…}) # pre-loaded
    await pipeline.initialize()
    async for event in pipeline.send("Show me listings in Allston"):
        handle(event)
    await pipeline.close()
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
import uuid
from typing import AsyncGenerator, Optional

import structlog
import yaml

from app.agents.cost_tracker import (
    ChatCostTracker,
    ChatUsageAccumulator,
    extract_usage_from_event,
)
from app.agents.graph import build_graph
from app.agents.llm import create_chain
from app.agents.state import UserContext, create_initial_state
from app.agents.tools.read_tools import set_cursor_provider
from app.config import get_settings
from app.core.config_loader import CONFIG_DIR

logger = structlog.get_logger()


# =====================================================================
# Config
# =====================================================================

def _load_agents_config() -> dict:
    with open(CONFIG_DIR / "agents.yml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_services_config() -> dict:
    with open(CONFIG_DIR / "services.yml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# =====================================================================
# Snowflake connection factory
# =====================================================================

def _create_connection():
    """Create a fresh Snowflake connection from settings."""
    import snowflake.connector
    settings = get_settings()
    return snowflake.connector.connect(
        account=settings.snowflake_account,
        user=settings.snowflake_user,
        password=settings.snowflake_password.get_secret_value(),
        database=settings.snowflake_database,
        warehouse=settings.snowflake_warehouse,
        role=settings.snowflake_role,
    )


def _create_tool_cursor():
    """Cursor factory for agent tool calls.

    Creates a fresh connection + cursor per invocation. The cursor's
    close() is patched to also close the connection — total isolation.
    """
    conn = _create_connection()
    cursor = conn.cursor()
    _original_close = cursor.close

    def _close_with_conn():
        try:
            _original_close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    cursor.close = _close_with_conn
    return cursor


# =====================================================================
# Session summary generation
# =====================================================================

_SUMMARY_PROMPT = (
    "You are a session summarizer for Vicinity, a Boston housing assistant. "
    "Given a conversation transcript, produce a concise summary (3-5 sentences) "
    "covering: what the user searched for, key findings, decisions made, and "
    "any pending actions. Include specific listing IDs, neighborhoods, and "
    "price points mentioned. Return ONLY the summary text."
)


async def _generate_summary(messages: list, listings: set[str]) -> dict:
    """LLM-generate a session summary from recent message history."""
    from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

    transcript = []
    for msg in messages[-30:]:
        if isinstance(msg, HumanMessage):
            transcript.append(f"User: {msg.content[:500]}")
        elif isinstance(msg, AIMessage) and msg.content:
            transcript.append(f"Assistant: {msg.content[:500]}")

    if not transcript:
        return {"summary": "Empty session.", "listings_discussed": []}

    chain = create_chain()
    try:
        response = await chain.ainvoke_with_fallback([
            SystemMessage(content=_SUMMARY_PROMPT),
            HumanMessage(content="\n".join(transcript)),
        ])
        return {
            "summary": response.content.strip(),
            "listings_discussed": list(listings),
        }
    except Exception as e:
        logger.warning("summary_generation_failed", error=str(e)[:200])
        return {
            "summary": f"Session with {len(transcript)} exchanges.",
            "listings_discussed": list(listings),
        }


# =====================================================================
# Nodes that produce non-streaming output (need special event capture)
# =====================================================================

# Nodes whose on_chain_end output should be captured as a node_end event.
# These nodes return AIMessages directly without going through chat_react's
# LLM streaming, so their content would otherwise be invisible to the client.
_DIRECT_OUTPUT_NODES = frozenset({"block", "organizer_plan", "organizer_confirm"})


# =====================================================================
# ChatPipeline
# =====================================================================

class ChatPipeline:
    """Core chat orchestration. Used by FastAPI router, MCP server, and terminal.

    Lifecycle:
        pipeline = ChatPipeline(user_id=..., user_context=...)
        await pipeline.initialize()
        async for event in pipeline.send("hello"):
            ...
        await pipeline.close()
    """

    def __init__(
        self,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        user_context: Optional[dict] = None,
        checkpointer=None,
    ):
        self.user_id = user_id
        self.session_id = session_id or str(uuid.uuid4())
        self.message_count = 0
        self.listings_discussed: set[str] = set()

        from langgraph.checkpoint.memory import MemorySaver
        self.checkpointer = checkpointer or MemorySaver()

        self._user_context: dict = user_context or {}
        self._graph = None
        self._agents_cfg = _load_agents_config()
        self._services_cfg = _load_services_config()
        self._initialized = False
        self._has_interrupt = False

    @property
    def is_authenticated(self) -> bool:
        return bool(self.user_id)

    async def initialize(self):
        """Connect, load context if needed, build graph. Idempotent."""
        if self._initialized:
            return

        set_cursor_provider(_create_tool_cursor)

        if self.user_id and not self._user_context:
            conn = await asyncio.to_thread(_create_connection)
            try:
                cursor = conn.cursor()
                from app.services.user_data import load_user_session
                self._user_context = await asyncio.to_thread(
                    load_user_session, cursor, self.user_id,
                )
                cursor.close()
            except Exception as e:
                logger.error("context_load_failed", user_id=self.user_id,
                             error=str(e)[:200])
                self._user_context = {}
                self.user_id = None
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        if self._user_context:
            self._user_context.setdefault("session_id", self.session_id)

        self._graph = build_graph(checkpointer=self.checkpointer)
        self._initialized = True

        logger.info(
            "pipeline_ready",
            user_id=self.user_id or "anonymous",
            session_id=self.session_id,
            has_profile=bool(self._user_context.get("profile_id")),
            bookmarks=len(self._user_context.get("active_bookmarks", [])),
            summaries=len(self._user_context.get("recent_summaries", [])),
            mode="authenticated" if self.is_authenticated else "anonymous",
        )

    def upgrade_session(self, user_id: str, user_context: dict):
        """Upgrade anonymous → authenticated without reconnecting."""
        log = logger.bind(
            op="session_upgrade",
            old_user=self.user_id or "anonymous",
            new_user=user_id,
            session_id=self.session_id,
        )
        self.user_id = user_id
        self._user_context = user_context
        self._user_context.setdefault("session_id", self.session_id)
        log.info("session_upgraded",
                 has_profile=bool(user_context.get("profile_id")),
                 bookmarks=len(user_context.get("active_bookmarks", [])))

    def _invoke_config(self) -> dict:
        recursion_limit = self._agents_cfg.get("react", {}).get("recursion_limit", 60)
        return {
            "configurable": {"thread_id": self.session_id},
            "recursion_limit": recursion_limit,
        }

    # -----------------------------------------------------------------
    # Core send
    # -----------------------------------------------------------------

    async def send(self, message: str) -> AsyncGenerator[dict, None]:
        """Send a message and stream typed events.

        Yields dicts: {type: str, data: dict}
        Event types: route, node_start, node_end, tool_start, tool_end,
                     token, interrupt, done, error
        """
        if not self._initialized:
            await self.initialize()

        config = self._invoke_config()

        if self._has_interrupt:
            from langgraph.types import Command
            invocation = Command(resume=message)
            self._has_interrupt = False
        else:
            graph_state = await self._graph.aget_state(config)
            if graph_state.values:
                from langchain_core.messages import HumanMessage
                invocation = {"messages": [HumanMessage(content=message)]}
            else:
                invocation = create_initial_state(self._user_context, message)

        # Per-turn trace_id is generated by create_initial_state for new
        # turns; for continued turns we pull the trace_id from state after
        # the first event. Keep a local variable so we can flush cost even
        # if state inspection fails at the end.
        turn_trace_id: Optional[str] = (
            invocation.get("trace_id")
            if isinstance(invocation, dict) else None
        )

        start_time = time.perf_counter()
        final_content = ""
        tool_calls_made = 0
        tool_errors = 0

        # Per-turn LLM usage accumulator. Populated from on_chat_model_end
        # events across every agent node that made an LLM call during this
        # turn (input_gate's LLM call is inside a separate code path that
        # does not go through the graph's event stream; see _invoke_config
        # notes in guardrail doc).
        usage_acc = ChatUsageAccumulator()
        # Track per-node LLM durations so we can attribute wall time.
        _last_chat_model_start: dict[str, float] = {}

        try:
            async for event in self._graph.astream_events(
                invocation, config=config, version="v2",
            ):
                kind = event.get("event", "")
                name = event.get("name", "")
                meta = event.get("metadata") or {}
                node = meta.get("langgraph_node", "")
                run_id = event.get("run_id", "")

                # --- Input gate result ---
                if kind == "on_chain_end" and node == "input_gate":
                    output = event.get("data", {}).get("output", {})
                    if isinstance(output, dict) and "route" in output:
                        yield {"type": "route", "data": {
                            "route": output["route"],
                            "is_valid": output.get("is_valid", True),
                        }}

                # --- Non-streaming node output (block, organizer guard) ---
                elif kind == "on_chain_end" and node in _DIRECT_OUTPUT_NODES:
                    output = event.get("data", {}).get("output", {})
                    if isinstance(output, dict):
                        msgs = output.get("messages", [])
                        if msgs and hasattr(msgs[-1], "content") and msgs[-1].content:
                            content = msgs[-1].content
                            final_content += content
                            yield {"type": "node_end", "data": {
                                "node": node,
                                "content": content,
                            }}

                # --- Agent node start ---
                elif kind == "on_chain_start" and node and node not in (
                    "input_gate", "guardrail",
                ):
                    yield {"type": "node_start", "data": {"node": node}}

                # --- LLM call start: record wall-clock for duration attribution ---
                elif kind == "on_chat_model_start":
                    if run_id:
                        _last_chat_model_start[run_id] = time.perf_counter()

                # --- LLM call end: capture usage metadata + compute duration ---
                elif kind == "on_chat_model_end":
                    duration_ms = 0
                    if run_id and run_id in _last_chat_model_start:
                        duration_ms = int(
                            (time.perf_counter() - _last_chat_model_start.pop(run_id)) * 1000
                        )
                    usage = extract_usage_from_event(event)
                    if usage:
                        usage_acc.add(
                            model=usage["model"],
                            input_tokens=usage["input_tokens"],
                            output_tokens=usage["output_tokens"],
                            duration_ms=duration_ms,
                        )

                # --- Tool start ---
                elif kind == "on_tool_start":
                    tool_calls_made += 1
                    tool_input = event.get("data", {}).get("input", {})
                    yield {"type": "tool_start", "data": {
                        "tool": name,
                        "args": _summarize_args(tool_input),
                    }}

                # --- Tool end ---
                elif kind == "on_tool_end":
                    output_str = str(event.get("data", {}).get("output", ""))[:300]
                    is_error = '"success": false' in output_str.lower()
                    if is_error:
                        tool_errors += 1
                    yield {"type": "tool_end", "data": {
                        "tool": name,
                        "size": len(output_str),
                        "error": is_error,
                    }}
                    self._extract_listings(output_str)

                # --- Streaming tokens from user-facing agent nodes ---
                # chat_react is the universal spokesperson; chat_react_retry
                # is its empty-response retry variant; report_react produces
                # its own final user-facing output and bypasses chat_react,
                # so its tokens must stream to the UI directly.
                elif kind == "on_chat_model_stream" and node in (
                    "chat_react", "chat_react_retry", "report_react",
                ):
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        final_content += chunk.content
                        yield {"type": "token", "data": {
                            "content": chunk.content,
                        }}

            # --- Post-stream: resolve trace_id from final state if needed ---
            graph_state = await self._graph.aget_state(config)
            if turn_trace_id is None:
                turn_trace_id = graph_state.values.get("trace_id")

            # --- Post-stream: check for interrupt ---
            if graph_state.next:
                self._has_interrupt = True
                pending = graph_state.values.get("pending_confirmation")
                yield {"type": "interrupt", "data": pending or {
                    "summary": "Awaiting confirmation",
                }}
            else:
                elapsed_ms = int((time.perf_counter() - start_time) * 1000)
                usage_snapshot = usage_acc.snapshot()
                done_data = {
                    "elapsed_ms": elapsed_ms,
                    "tool_calls": tool_calls_made,
                    "tool_errors": tool_errors,
                    "message_length": len(final_content),
                }
                if usage_snapshot is not None:
                    done_data["llm_usage"] = {
                        "model": usage_snapshot.model,
                        "input_tokens": usage_snapshot.input_tokens,
                        "output_tokens": usage_snapshot.output_tokens,
                        "total_tokens": usage_snapshot.total_tokens,
                        "cost_usd": usage_snapshot.total_cost_usd,
                        "calls": usage_snapshot.calls,
                    }
                yield {"type": "done", "data": done_data}
                await self._persist_exchange(
                    user_msg=message,
                    assistant_msg=final_content,
                    trace_id=turn_trace_id,
                    usage_snapshot=usage_snapshot,
                )

        except Exception as e:
            logger.error("pipeline_error",
                         session_id=self.session_id,
                         error=str(e)[:300])
            yield {"type": "error", "data": {"error": str(e)[:500]}}

    # -----------------------------------------------------------------
    # Persistence (authenticated only)
    # -----------------------------------------------------------------

    async def _persist_exchange(
        self,
        user_msg: str,
        assistant_msg: str,
        *,
        trace_id: Optional[str] = None,
        usage_snapshot=None,
    ):
        """Write the turn's messages + LLM usage to Snowflake.

        Authenticated-only. One connection shared between the conversation
        log writes and the cost tracker flush — same try/finally, so we
        never leak connections on partial failure.
        """
        if not self.user_id:
            return

        from app.services.user_data import append_message

        try:
            conn = await asyncio.to_thread(_create_connection)
            cursor = conn.cursor()
            try:
                # 1. Conversation log
                await asyncio.to_thread(
                    append_message, cursor,
                    self.user_id, self.session_id, "user", user_msg,
                )
                self.message_count += 1
                if assistant_msg.strip():
                    await asyncio.to_thread(
                        append_message, cursor,
                        self.user_id, self.session_id, "assistant", assistant_msg,
                    )
                    self.message_count += 1

                # 2. LLM usage (fire-and-forget: failure is logged, not raised)
                if usage_snapshot is not None and trace_id:
                    tracker = ChatCostTracker(cursor)
                    await asyncio.to_thread(
                        tracker.flush_turn,
                        trace_id, self.session_id, usage_snapshot,
                    )
            finally:
                cursor.close()
                conn.close()
        except Exception as e:
            logger.warning("persist_failed",
                           session_id=self.session_id,
                           error=str(e)[:200])

        trigger = (
            self._services_cfg.get("user_data", {})
            .get("conversations", {}).get("summary_trigger_count", 20)
        )
        if self.message_count > 0 and self.message_count % trigger == 0:
            await self._generate_and_save_summary()

    async def _generate_and_save_summary(self):
        if not self.user_id:
            return

        from app.services.user_data import write_session_summary

        config = self._invoke_config()
        graph_state = await self._graph.aget_state(config)
        messages = graph_state.values.get("messages", [])
        summary_data = await _generate_summary(messages, self.listings_discussed)

        try:
            conn = await asyncio.to_thread(_create_connection)
            cursor = conn.cursor()
            try:
                await asyncio.to_thread(
                    write_session_summary,
                    cursor, self.user_id, self.session_id,
                    summary_data["summary"],
                    listings_discussed=summary_data.get("listings_discussed"),
                    message_count=self.message_count,
                )
            finally:
                cursor.close()
                conn.close()
            logger.info("session_summary_written",
                        session_id=self.session_id,
                        message_count=self.message_count)
        except Exception as e:
            logger.warning("summary_write_failed",
                           session_id=self.session_id,
                           error=str(e)[:200])

    def _extract_listings(self, output: str):
        for match in re.findall(r'"listing_id":\s*"([^"]+)"', output):
            self.listings_discussed.add(match)

    async def close(self):
        if self.user_id and self.message_count > 0:
            await self._generate_and_save_summary()
        logger.info("session_closed",
                     session_id=self.session_id,
                     user_id=self.user_id or "anonymous",
                     messages=self.message_count,
                     listings=len(self.listings_discussed))


# =====================================================================
# Terminal helpers
# =====================================================================

def _summarize_args(args, max_len: int = 120) -> str:
    if isinstance(args, str):
        return args[:max_len]
    if not isinstance(args, dict):
        return str(args)[:max_len]
    parts = []
    for k, v in args.items():
        if v is None:
            continue
        sv = str(v)
        if len(sv) > 40:
            sv = sv[:37] + "..."
        parts.append(f"{k}={sv}")
    return ", ".join(parts)[:max_len]


COLORS = {
    "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
    "cyan": "\033[36m", "green": "\033[32m", "yellow": "\033[33m",
    "red": "\033[31m", "magenta": "\033[35m", "blue": "\033[34m",
}


def _c(color: str, text: str) -> str:
    return f"{COLORS.get(color, '')}{text}{COLORS['reset']}"


# Node display names for cleaner logging
_NODE_LABELS = {
    "chat_react": "Chat Agent",
    "chat_react_retry": "Chat Agent (retry)",
    "search_react": "Search Supervisor",
    "report_react": "Report Generator",
    "organizer_plan": "Organizer",
    "organizer_confirm": "Organizer (confirm)",
    "org_tools": "Organizer Tools",
    "chat_tools": "Chat Tools",
    "search_tools": "Search Tools",
    "report_tools": "Report Tools",
    "block": "Block",
}


# =====================================================================
# Terminal interface
# =====================================================================

async def run_terminal(
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    verbose: bool = False,
):
    pipeline = ChatPipeline(user_id=user_id, session_id=session_id)

    mode_label = _c("green", "authenticated") if user_id else _c("yellow", "anonymous")
    print(_c("bold", "\n  Vicinity Chat"))
    print(_c("dim", f"  User: {user_id or 'anonymous'} | Session: {pipeline.session_id}"))
    print(f"  Mode: {mode_label}")
    print(_c("dim", "  Type 'quit' to exit, 'status' for session info\n"))

    try:
        await pipeline.initialize()
        ctx = pipeline._user_context
        if ctx.get("profile_id"):
            print(_c("green", f"  Profile: ${ctx.get('budget_min','?')}-${ctx.get('budget_max','?')}, "
                               f"{ctx.get('bedrooms_min','?')}+ beds"))
        if ctx.get("active_bookmarks"):
            print(_c("green", f"  Bookmarks: {len(ctx['active_bookmarks'])} active"))
        if ctx.get("recent_summaries"):
            print(_c("green", f"  Memory: {len(ctx['recent_summaries'])} previous sessions"))
        if not user_id:
            print(_c("yellow", "  Anonymous mode — sign in to save preferences and bookmarks"))
        print()
    except Exception as e:
        print(_c("red", f"  Failed to initialize: {e}"))
        return

    while True:
        try:
            user_input = input(_c("cyan", "You: ")).strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            break
        if user_input.lower() == "status":
            print(_c("dim", f"  Session:    {pipeline.session_id}"))
            print(_c("dim", f"  User:       {pipeline.user_id or 'anonymous'}"))
            print(_c("dim", f"  Mode:       {'authenticated' if pipeline.is_authenticated else 'anonymous'}"))
            print(_c("dim", f"  Messages:   {pipeline.message_count}"))
            print(_c("dim", f"  Listings:   {pipeline.listings_discussed or 'none'}"))
            print(_c("dim", f"  Interrupt:  {'pending' if pipeline._has_interrupt else 'none'}"))
            continue

        print()
        response_started = False

        async for event in pipeline.send(user_input):
            t = event["type"]
            d = event["data"]

            if t == "route":
                route = d["route"]
                color = "green" if d.get("is_valid", True) else "red"
                label = _NODE_LABELS.get(route, route)
                if verbose:
                    print(_c("dim", f"  route → {_c(color, route)}"))
                else:
                    print(_c("dim", f"  [{_c(color, route)}] "), end="")

            elif t == "node_start":
                node = d["node"]
                label = _NODE_LABELS.get(node, node)
                if verbose:
                    print(_c("blue", f"  >> {label}"))

            elif t == "node_end":
                # Direct output from non-streaming nodes (block, organizer guard)
                node = d["node"]
                content = d.get("content", "")
                label = _NODE_LABELS.get(node, node)
                if content:
                    if not response_started:
                        print(_c("bold", "\nVicinity: "), end="")
                        response_started = True
                    print(content, end="", flush=True)
                if verbose:
                    print(_c("dim", f"\n  << {label} ({len(content)} chars)"))

            elif t == "tool_start":
                print(_c("yellow", f"\n  tool: {d['tool']}") +
                      _c("dim", f"({d.get('args','')})"))

            elif t == "tool_end":
                if d.get("error"):
                    print(_c("red", f"  result: error ({d['size']} bytes)"))
                else:
                    print(_c("dim", f"  result: {d['size']} bytes"))

            elif t == "token":
                if not response_started:
                    print(_c("bold", "\nVicinity: "), end="")
                    response_started = True
                print(d["content"], end="", flush=True)

            elif t == "interrupt":
                print(_c("magenta", f"\n\n  Confirm: {d.get('summary', '?')}"))
                print(_c("dim", "  (type 'yes' to approve, anything else to cancel)"))

            elif t == "done":
                if response_started:
                    print()
                ms = d.get("elapsed_ms", 0)
                calls = d.get("tool_calls", 0)
                errs = d.get("tool_errors", 0)
                msg_len = d.get("message_length", 0)
                stats = f"{ms}ms"
                if calls:
                    stats += f" | {calls} tools"
                if errs:
                    stats += f" | {_c('red', f'{errs} errors')}"
                stats += f" | {msg_len} chars"
                usage = d.get("llm_usage")
                if usage:
                    stats += (
                        f" | {usage['total_tokens']} tok"
                        f" | ${usage['cost_usd']:.4f}"
                    )
                print(_c("dim", f"  [{stats}]"))

            elif t == "error":
                print(_c("red", f"\n  Error: {d['error']}"))

        print()

    print(_c("dim", "\n  Closing session..."))
    await pipeline.close()
    print(_c("dim", "  Done.\n"))


async def run_single(
    message: str,
    user_id: Optional[str] = None,
    verbose: bool = False,
):
    pipeline = ChatPipeline(user_id=user_id)
    await pipeline.initialize()

    async for event in pipeline.send(message):
        t = event["type"]
        d = event["data"]
        if t == "token":
            print(d["content"], end="", flush=True)
        elif t == "node_end" and d.get("content"):
            print(d["content"], end="", flush=True)
        elif t == "tool_start" and verbose:
            print(f"\n  [tool: {d['tool']}({d.get('args','')})]", file=sys.stderr)
        elif t == "error":
            print(f"\nError: {d['error']}", file=sys.stderr)

    print()
    await pipeline.close()


# =====================================================================
# CLI entry point
# =====================================================================

def main():
    parser = argparse.ArgumentParser(description="Vicinity Chat")
    parser.add_argument("--user-id", type=str, default=None,
                        help="Direct user ID (dev mode, skips auth)")
    parser.add_argument("--email", type=str, default=None,
                        help="Authenticate with email + password")
    parser.add_argument("--session-id", type=str, default=None,
                        help="Resume an existing session")
    parser.add_argument("-m", "--message", type=str, default=None,
                        help="Single message (non-interactive)")
    parser.add_argument("--verbose", action="store_true",
                        help="Show all graph events")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    user_id = args.user_id

    if args.email and not user_id:
        import getpass
        password = getpass.getpass(f"Password for {args.email}: ")

        conn = _create_connection()
        try:
            cursor = conn.cursor()
            from app.services.user_data import authenticate_user
            result = authenticate_user(cursor, args.email, password)
            cursor.close()
        finally:
            conn.close()

        if not result.success:
            print(f"\n  Authentication failed: {result.error}")
            sys.exit(1)

        user_id = result.data[0]["user_id"]
        print(f"\n  Authenticated as {result.data[0].get('display_name', args.email)}")

    if args.message:
        asyncio.run(run_single(args.message, user_id=user_id, verbose=args.verbose))
    else:
        asyncio.run(run_terminal(user_id=user_id, session_id=args.session_id,
                                 verbose=args.verbose))


if __name__ == "__main__":
    main()