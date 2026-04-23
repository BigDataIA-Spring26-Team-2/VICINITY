"""Test fixtures for the Vicinity agent layer.

Provides:
  - MockLLM: configurable async LLM that returns preset AIMessages
  - sample_user_context: realistic UserContext dict
  - sample_state: AgentState factory with sensible defaults
  - Config patching: redirects YAML loading to test config
  - Cache reset: clears all module-level caches between tests
"""

import asyncio
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage


# -- MockLLM -----------------------------------------------------------

class MockLLM:
    """Configurable mock LLM for testing agent nodes.

    Supports ainvoke (async), bind_tools, and configurable responses.
    Set .response before each test to control what the LLM returns.
    """

    def __init__(self, response: Optional[AIMessage] = None):
        self.response = response or AIMessage(content="Mock response")
        self.last_messages = None
        self.bound_tools = None
        self.invoke_count = 0

    async def ainvoke(self, messages, **kwargs):
        self.last_messages = messages
        self.invoke_count += 1
        return self.response

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self


# -- Mock ProviderChainLLM ---------------------------------------------

class MockChain:
    """Mock for ProviderChainLLM with fallback tracking."""

    def __init__(self, response: Optional[AIMessage] = None):
        self.response = response or AIMessage(content='{"route": "chat", "reason": "default"}')
        self.active = MockLLM(self.response)
        self.invoke_count = 0

    async def ainvoke_with_fallback(self, messages, **kwargs):
        self.invoke_count += 1
        return self.response


# -- Test config -------------------------------------------------------

TEST_CONFIG = {
    "llm": {
        "providers": [
            {"name": "test", "type": "openai", "model": "test-model", "env_key": "TEST_KEY"},
        ],
        "temperature": 0.0,
        "max_tokens": 100,
        "gate_max_tokens": 50,
    },
    "input_gate": {
        "enabled": True,
        "max_query_length": 5000,
        "system_prompt": "Test gate prompt",
    },
    "tools": {"max_calls_per_turn": 15, "timeout_seconds": 30, "max_result_chars": 50000},
    "react": {"max_iterations": 10, "recursion_limit": 50},
    "guardrail": {"empty_response_retries": 1, "max_response_length": 15000},
    "chat_agent": {"system_prompt": "You are a test chat agent."},
    "organizer": {"system_prompt": "You are a test organizer."},
    "search_supervisor": {"system_prompt": "You are a test search supervisor."},
    "report_generator": {"system_prompt": "You are a test report generator."},
    "cost": {"track_usage": False, "warn_threshold_usd": 1.0},
}


# -- Fixtures ----------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_caches():
    """Clear all module-level caches between tests."""
    yield
    # Reset llm.py caches
    import app.agents.llm as llm_mod
    llm_mod._config_cache = None
    llm_mod._chain_cache = None
    llm_mod._gate_chain_cache = None

    # Reset graph.py cache
    import app.agents.graph as graph_mod
    graph_mod._config = None

    # Reset agent prompt caches
    import app.agents.chat_agent as chat_mod
    chat_mod._system_prompt = None

    import app.agents.organizer as org_mod
    org_mod._system_prompt = None

    import app.agents.search_supervisor as search_mod
    search_mod._system_prompt = None

    import app.agents.report_generator as report_mod
    report_mod._system_prompt = None


@pytest.fixture
def mock_config():
    """Patch YAML loading to return TEST_CONFIG across all modules."""
    def fake_open(*args, **kwargs):
        import io
        import yaml
        return io.StringIO(yaml.dump(TEST_CONFIG))

    with patch("builtins.open", side_effect=fake_open):
        yield TEST_CONFIG


@pytest.fixture
def sample_user_context():
    return {
        "user_id": "u-test-001",
        "session_id": "sess-001",
        "email": "test@example.com",
        "display_name": "Test User",
        "profile_id": "p-001",
        "work_address": "77 Massachusetts Ave, Cambridge, MA",
        "work_lat": 42.3601,
        "work_lon": -71.0942,
        "budget_min": 1500,
        "budget_max": 3000,
        "bedrooms_min": 2,
        "bedrooms_max": 4,
        "max_commute_min": 30,
        "preferences_text": "Korean food, gym, quiet neighborhood",
        "preference_tags": ["korean_food", "gym", "safety"],
        "recent_summaries": [
            {"summary": "User searched for 2BR in Allston under $2500", "session_id": "prev-1"},
        ],
        "active_bookmarks": [
            {"listing_id": "lst-abc", "street": "123 Main St", "neighborhood": "Allston", "price": 2200},
            {"listing_id": "lst-def", "street": "456 Oak Ave", "neighborhood": "Brighton", "price": 2400},
        ],
    }


@pytest.fixture
def sample_state(sample_user_context):
    """Build a minimal AgentState for testing."""
    def _build(message="Show me listings in Allston", **overrides):
        state = {
            "messages": [HumanMessage(content=message)],
            "route": "",
            "user_context": sample_user_context,
            "sub_agent_result": None,
            "pending_confirmation": None,
            "tool_call_count": 0,
            "tool_call_ledger": [],
            "query_cost_usd": 0.0,
            "trace_id": "test-trace-001",
            "is_valid": True,
            "empty_retries": 0,
            "error": None,
        }
        state.update(overrides)
        return state
    return _build


@pytest.fixture
def mock_llm():
    """Reusable MockLLM factory."""
    def _build(content="Mock response", tool_calls=None):
        response = AIMessage(content=content, tool_calls=tool_calls or [])
        return MockLLM(response)
    return _build


@pytest.fixture
def mock_chain():
    """Reusable MockChain factory."""
    def _build(content='{"route": "chat", "reason": "test"}'):
        return MockChain(AIMessage(content=content))
    return _build


def make_tool_call(name, args=None, call_id="call_001"):
    """Helper to build a tool_call dict matching LangChain format."""
    return {"name": name, "args": args or {}, "id": call_id, "type": "tool_call"}