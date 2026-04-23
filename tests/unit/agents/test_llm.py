"""Tests for app.agents.llm — provider chain, fallback, caching."""

import os
from unittest.mock import patch, MagicMock

import pytest
from langchain_core.messages import AIMessage

from app.agents.llm import (
    ProviderChainLLM,
    _build_provider,
    _get_chain,
    _get_gate_chain,
    create_chain,
    create_llm,
    create_gate_llm,
    reload_config,
)


class TestBuildProvider:

    @patch.dict(os.environ, {"TEST_DEEPSEEK_KEY": "sk-test-123"})
    @patch("langchain_deepseek.ChatDeepSeek")
    def test_deepseek_provider(self, mock_cls, mock_config):
        entry = {"name": "ds", "type": "deepseek", "model": "deepseek-chat", "env_key": "TEST_DEEPSEEK_KEY"}
        result = _build_provider(entry, max_tokens=4096, temperature=0.1)
        mock_cls.assert_called_once()
        kwargs = mock_cls.call_args[1]
        assert kwargs["model"] == "deepseek-chat"
        assert kwargs["temperature"] == 0.1
        assert kwargs["max_tokens"] == 4096

    @patch.dict(os.environ, {"TEST_OAI_KEY": "sk-oai-456"})
    @patch("langchain_openai.ChatOpenAI")
    def test_openai_provider(self, mock_cls, mock_config):
        entry = {"name": "oai", "type": "openai", "model": "gpt-4o", "env_key": "TEST_OAI_KEY"}
        result = _build_provider(entry, max_tokens=4096, temperature=0.1)
        mock_cls.assert_called_once()
        kwargs = mock_cls.call_args[1]
        assert kwargs["model"] == "gpt-4o"

    @patch.dict(os.environ, {}, clear=True)
    def test_skips_missing_key(self, mock_config):
        entry = {"name": "ds", "type": "deepseek", "model": "deepseek-chat", "env_key": "NONEXISTENT_KEY"}
        result = _build_provider(entry, max_tokens=100, temperature=0.0)
        assert result is None


class TestProviderChainLLM:

    def test_raises_on_empty_providers(self):
        with pytest.raises(RuntimeError, match="No LLM providers"):
            ProviderChainLLM([], [])

    def test_active_returns_first(self):
        p1 = MagicMock()
        p2 = MagicMock()
        chain = ProviderChainLLM([p1, p2], ["primary", "fallback"])
        assert chain.active is p1

    @pytest.mark.asyncio
    async def test_fallback_on_primary_failure(self):
        p1 = MagicMock()
        p1.ainvoke = MagicMock(side_effect=Exception("primary down"))
        p2 = MagicMock()
        expected = AIMessage(content="from fallback")
        p2.ainvoke = MagicMock(return_value=expected)

        # Make ainvoke awaitable
        import asyncio
        p1.ainvoke = lambda *a, **k: (_ for _ in ()).throw(Exception("primary down"))

        async def p1_fail(*a, **k):
            raise Exception("primary down")

        async def p2_ok(*a, **k):
            return expected

        p1.ainvoke = p1_fail
        p2.ainvoke = p2_ok

        chain = ProviderChainLLM([p1, p2], ["primary", "fallback"])
        result = await chain.ainvoke_with_fallback([])
        assert result.content == "from fallback"

    @pytest.mark.asyncio
    async def test_raises_when_all_fail(self):
        async def fail(*a, **k):
            raise Exception("down")

        p1 = MagicMock()
        p1.ainvoke = fail
        p2 = MagicMock()
        p2.ainvoke = fail

        chain = ProviderChainLLM([p1, p2], ["a", "b"])
        with pytest.raises(RuntimeError, match="All LLM providers failed"):
            await chain.ainvoke_with_fallback([])


class TestCaching:

    @patch("app.agents.llm._build_chain")
    def test_get_chain_caches(self, mock_build, mock_config):
        mock_build.return_value = MagicMock()
        import app.agents.llm as mod
        mod._chain_cache = None

        c1 = _get_chain()
        c2 = _get_chain()
        assert mock_build.call_count == 1
        assert c1 is c2

    def test_reload_clears_all(self, mock_config):
        import app.agents.llm as mod
        mod._config_cache = {"some": "data"}
        mod._chain_cache = MagicMock()
        mod._gate_chain_cache = MagicMock()

        reload_config()

        assert mod._config_cache is None
        assert mod._chain_cache is None
        assert mod._gate_chain_cache is None