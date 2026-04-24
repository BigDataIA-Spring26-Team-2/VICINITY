"""Shared fixtures for Vicinity integration tests.

Isolation strategy:
  - READS hit real Snowflake (real data shapes surface real bugs).
  - WRITES are replaced with a sentinel (ScriptedWriteTool). Every write
    attempt is RECORDED but NOT executed. If any prod-write reaches the
    DB during tests, that is a test-setup bug — the sentinel raises.
  - LLM calls go through FakeLLM. Behavior is scripted per-test:
    route / tool_calls / content are queued up front, consumed in order.

All four suites share this file via `conftest.py` discovery at the
integration/ level. No test should ever construct ChatOpenAI / DeepSeek /
MagicMock LLMs directly — use FakeLLM or build on it.

Env required:
  DEEPSEEK_API_KEY (unused but imported by llm.py)
  SNOWFLAKE_* (real, read-only user recommended)
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import pytest
import snowflake.connector
from langchain_core.messages import AIMessage, BaseMessage

from app.agents.state import create_initial_state
from app.agents.tools.read_tools import set_cursor_provider


# =====================================================================
# Guard: prevent the integration suite from running against prod
# =====================================================================

def _assert_safe_environment():
    """Refuse to run if SNOWFLAKE_DATABASE looks like prod.

    The suite is designed for a read-only user. We cannot prevent a
    privileged user from writing via run_sql or similar, but we can
    fail loudly if someone points this at an obviously-prod database.
    """
    db = (os.getenv("SNOWFLAKE_DATABASE") or "").upper()
    flag = os.getenv("VICINITY_INTEGRATION_ALLOW_PROD", "").lower()
    if flag in ("1", "true", "yes"):
        return
    if any(tok in db for tok in ("PROD", "PRODUCTION", "LIVE")):
        raise RuntimeError(
            f"Refusing to run integration tests against {db!r}. "
            "Set SNOWFLAKE_DATABASE to a dev/staging DB, or set "
            "VICINITY_INTEGRATION_ALLOW_PROD=1 to override."
        )


_assert_safe_environment()


# =====================================================================
# FakeLLM — deterministic, scriptable
# =====================================================================

@dataclass
class FakeLLMResponse:
    """One LLM call's scripted outcome.

    content:     AI message text (for synthesis / no-tool responses)
    tool_calls:  list of {name, args, id} — triggers tool execution
    is_json:     True when the response is a JSON string (input_gate)
    """
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    is_json: bool = False


@dataclass
class FakeLLM:
    """Drop-in replacement for langchain LLM objects.

    Implements ainvoke (used by agent nodes) and bind_tools (used by
    chains). Returns queued responses in order. Raises if queue is
    exhausted — that's a test-setup bug, not a silent skip.

    Also exposes `calls` — a list of (messages, response) tuples for
    post-hoc assertions about what was sent to which LLM.
    """
    responses: list[FakeLLMResponse] = field(default_factory=list)
    calls: list[tuple[list[BaseMessage], FakeLLMResponse]] = field(default_factory=list)
    name: str = "fake"

    def bind_tools(self, tools):  # langchain compat
        return self

    def with_fallbacks(self, *a, **kw):  # langchain compat
        return self

    def with_structured_output(self, *a, **kw):  # langchain compat
        return self

    async def ainvoke(self, messages, **kwargs) -> AIMessage:
        if not self.responses:
            raise AssertionError(
                f"FakeLLM({self.name}) queue exhausted. "
                f"Got {len(self.calls)} calls but no more responses scripted. "
                "Either your graph is making more LLM calls than you expected, "
                "or you need to queue more responses."
            )
        resp = self.responses.pop(0)
        self.calls.append((list(messages), resp))

        if resp.tool_calls:
            # Build a LangChain-compatible tool_calls list
            tcs = []
            for tc in resp.tool_calls:
                tcs.append({
                    "name": tc["name"],
                    "args": tc.get("args", {}),
                    "id": tc.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                    "type": "tool_call",
                })
            msg = AIMessage(content=resp.content, tool_calls=tcs)
        else:
            msg = AIMessage(content=resp.content)

        # usage_metadata so cost_tracker (if invoked) doesn't blow up
        msg.usage_metadata = {"input_tokens": 10, "output_tokens": 5,
                              "total_tokens": 15}
        msg.response_metadata = {"model_name": "fake-llm"}
        return msg

    # Some paths call the LLM through an object that has
    # ainvoke_with_fallback — provide a pass-through.
    async def ainvoke_with_fallback(self, messages, **kwargs):
        return await self.ainvoke(messages, **kwargs)


@pytest.fixture
def fake_gate_llm():
    """FakeLLM for the input_gate. Queue JSON route strings via .queue_route()."""
    llm = FakeLLM(name="gate")

    def queue_route(route: str, reason: str = "test"):
        llm.responses.append(FakeLLMResponse(
            content=f'{{"route": "{route}", "reason": "{reason}"}}',
            is_json=True,
        ))
    llm.queue_route = queue_route  # type: ignore[attr-defined]
    return llm


@pytest.fixture
def fake_agent_llm():
    """FakeLLM for chat / search / report / organizer nodes.

    Helpers:
      .queue_text(content)                 — plain synthesis response
      .queue_tool_call(name, args)         — single tool-call response
      .queue_tool_calls([(name, args), ...]) — multi tool-call response
    """
    llm = FakeLLM(name="agent")

    def queue_text(content: str):
        llm.responses.append(FakeLLMResponse(content=content))

    def queue_tool_call(name: str, args: Optional[dict] = None):
        llm.responses.append(FakeLLMResponse(
            tool_calls=[{"name": name, "args": args or {}}],
        ))

    def queue_tool_calls(calls: list[tuple[str, dict]]):
        llm.responses.append(FakeLLMResponse(
            tool_calls=[{"name": n, "args": a} for n, a in calls],
        ))

    llm.queue_text = queue_text                  # type: ignore[attr-defined]
    llm.queue_tool_call = queue_tool_call        # type: ignore[attr-defined]
    llm.queue_tool_calls = queue_tool_calls      # type: ignore[attr-defined]
    return llm


# =====================================================================
# Write-tool sentinel — records attempts, never touches Snowflake
# =====================================================================

@dataclass
class WriteAttempt:
    tool_name: str
    args: dict


class WriteToolSentinel:
    """Singleton recorder. Any production write tool invoked during an
    integration test routes through here instead of hitting Snowflake."""
    def __init__(self):
        self.attempts: list[WriteAttempt] = []
        self.armed = False

    def record(self, tool_name: str, args: dict) -> dict:
        if not self.armed:
            raise RuntimeError(
                f"Write tool {tool_name!r} invoked outside of an armed "
                "integration test. This indicates missing fixture setup."
            )
        self.attempts.append(WriteAttempt(tool_name=tool_name, args=dict(args)))
        # Match the shape real write tools return so the graph continues.
        return {
            "success": True,
            "sentinel": True,
            "tool": tool_name,
            "args": dict(args),
            "message": f"[sentinel] {tool_name} recorded but not executed.",
        }

    def reset(self):
        self.attempts.clear()
        self.armed = False


_sentinel = WriteToolSentinel()


@pytest.fixture
def write_sentinel():
    """Arms the write-tool sentinel for one test.

    Every production write StructuredTool has its underlying `.func`
    replaced with the sentinel recorder. StructuredTool invokes `.func`
    when executed (sync or async paths both go through it), so this
    intercepts at the function level without touching Pydantic-field
    semantics on the tool object itself.

    We use object.__setattr__ directly to bypass the Pydantic model's
    __setattr__ guard (which rejects fields like .ainvoke). The
    original .func is restored in a finally block.
    """
    import app.agents.tools.write_tools as wt

    _sentinel.reset()
    _sentinel.armed = True

    write_tool_names = [
        "manage_profile",
        "manage_bookmarks",
        "manage_destinations",
        "manage_conversations",
        "flag_data",
        "update_pipeline_queries",
    ]

    originals: list[tuple[Any, str, Any]] = []

    try:
        for name in write_tool_names:
            tool = getattr(wt, name, None)
            if tool is None:
                continue

            def _record(_tool_name=name, **kwargs):
                return _sentinel.record(_tool_name, dict(kwargs))

            # Save original
            originals.append((tool, "func", tool.func))
            # Bypass Pydantic validation to rebind .func
            object.__setattr__(tool, "func", _record)

        yield _sentinel
    finally:
        for obj, attr, val in originals:
            object.__setattr__(obj, attr, val)
        _sentinel.reset()


# =====================================================================
# Real-Snowflake read cursor
# =====================================================================

def _create_real_connection():
    """Connect to the configured Snowflake instance. Read-only recommended."""
    from app.config import get_settings
    settings = get_settings()
    return snowflake.connector.connect(
        account=settings.snowflake_account,
        user=settings.snowflake_user,
        password=settings.snowflake_password.get_secret_value(),
        database=settings.snowflake_database,
        warehouse=settings.snowflake_warehouse,
        role=settings.snowflake_role,
    )


def _real_cursor_provider():
    """Create a real cursor whose close() also closes its connection."""
    conn = _create_real_connection()
    cur = conn.cursor()
    orig_close = cur.close

    def close_with_conn():
        try:
            orig_close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    cur.close = close_with_conn
    return cur


@pytest.fixture(scope="session")
def real_snowflake_available() -> bool:
    """Probe Snowflake once per session. If it fails, tests that need it skip."""
    try:
        conn = _create_real_connection()
        conn.cursor().execute("SELECT 1")
        conn.close()
        return True
    except Exception:
        return False


@pytest.fixture
def real_reads(real_snowflake_available):
    """Route read tools to real Snowflake."""
    if not real_snowflake_available:
        pytest.skip("Snowflake unavailable in this environment")
    set_cursor_provider(_real_cursor_provider)
    yield


# =====================================================================
# Graph + state helpers
# =====================================================================

@pytest.fixture
def thread_id():
    """Unique thread_id per test so checkpointer state doesn't leak."""
    return f"itest-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def checkpointer():
    from langgraph.checkpoint.memory import MemorySaver
    return MemorySaver()


@pytest.fixture
def anon_context() -> dict:
    """Anonymous user context — no user_id, read-only."""
    return {}


@pytest.fixture
def auth_context() -> dict:
    """Authenticated user context with bookmarks for report-flow tests.

    NOTE: user_id here is synthetic. Write tools never execute because the
    sentinel intercepts, so no row ever lands in USER_DATA.*.
    """
    return {
        "user_id": "u-integration-test",
        "session_id": "s-integration-test",
        "display_name": "Integration Tester",
        "email": "itest@example.invalid",
        "budget_min": 2000,
        "budget_max": 3200,
        "bedrooms_min": 2,
        "work_address": "1 Memorial Dr, Cambridge, MA",
        "work_lat": 42.3615,
        "work_lon": -71.0832,
        "preference_tags": ["safe", "transit"],
        "active_bookmarks": [
            {"listing_id": "lst-test-001", "street": "100 Commonwealth Ave",
             "neighborhood": "Back Bay", "price": 2800},
            {"listing_id": "lst-test-002", "street": "50 Harvard St",
             "neighborhood": "Allston", "price": 2400},
        ],
        "recent_summaries": [],
    }


def make_state(user_context: dict, message: str) -> dict:
    return create_initial_state(user_context, message)


# =====================================================================
# Graph builder with patched LLMs
# =====================================================================

@pytest.fixture
def build_test_graph(fake_gate_llm, fake_agent_llm, checkpointer, monkeypatch):
    """Returns a builder callable: build_test_graph() -> compiled graph.

    All LLMs resolve to fakes. Reads / writes are NOT patched here — use
    the real_reads and write_sentinel fixtures for those.
    """
    from app.agents import graph as graph_mod
    from app.agents import chat_agent, organizer, search_supervisor, report_generator

    # input_gate uses create_chain — the fake handles ainvoke_with_fallback
    monkeypatch.setattr(graph_mod, "create_chain", lambda *a, **k: fake_gate_llm)

    # Agent nodes use create_llm
    monkeypatch.setattr(chat_agent, "create_llm", lambda *a, **k: fake_agent_llm)
    monkeypatch.setattr(organizer, "create_llm", lambda *a, **k: fake_agent_llm)
    monkeypatch.setattr(search_supervisor, "create_llm", lambda *a, **k: fake_agent_llm)
    monkeypatch.setattr(report_generator, "create_llm", lambda *a, **k: fake_agent_llm)

    def _build():
        return graph_mod.build_graph(checkpointer=checkpointer)

    return _build