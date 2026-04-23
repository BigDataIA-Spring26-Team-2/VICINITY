"""Async LLM provider chain for the Vicinity agent layer.

Reads config/agents.yml llm.providers list. Tries each in order;
first successful response wins. Supports ChatDeepSeek (primary)
and ChatOpenAI (fallback). Any additional provider added to the
list is picked up automatically.

Chain instances are cached at module level — HTTP clients are
created once per process. bind_tools() returns a lightweight
wrapper and is safe to call per-invocation.

Usage:
    from app.agents.llm import create_llm, create_gate_llm, create_chain

    llm = create_llm()                      # primary provider
    llm_with_tools = create_llm(tools=...)  # with tool binding
    gate = create_gate_llm()                # lightweight for gate
    chain = create_chain()                  # full fallback chain
"""

from __future__ import annotations

import os
from typing import Optional, Sequence

import structlog
import yaml
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from app.core.config_loader import CONFIG_DIR

logger = structlog.get_logger()

# -- Config (cached) --------------------------------------------------

_config_cache: Optional[dict] = None


def _load_config() -> dict:
    global _config_cache
    if _config_cache is None:
        with open(CONFIG_DIR / "agents.yml", encoding="utf-8") as f:
            _config_cache = yaml.safe_load(f) or {}
    return _config_cache


def reload_config():
    """Force reload. For tests."""
    global _config_cache, _chain_cache, _gate_chain_cache
    _config_cache = None
    _chain_cache = None
    _gate_chain_cache = None


# -- Provider instantiation -------------------------------------------

def _build_provider(entry: dict, max_tokens: int, temperature: float) -> Optional[BaseChatModel]:
    """Instantiate a single LLM provider from a config entry."""
    api_key = os.getenv(entry["env_key"], "").strip()
    if not api_key:
        logger.warning("llm_provider_skipped", name=entry["name"], reason="no_api_key")
        return None

    provider_type = entry.get("type", "openai")
    model = entry["model"]

    if provider_type == "deepseek":
        from langchain_deepseek import ChatDeepSeek
        return ChatDeepSeek(
            model=model,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            max_retries=2,
            timeout=60,
        )

    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=entry.get("base_url"),
        temperature=temperature,
        max_tokens=max_tokens,
        max_retries=2,
        timeout=60,
    )


# -- Provider chain ---------------------------------------------------

class ProviderChainLLM:
    """Ordered fallback chain of LLM providers.

    Not a LangChain Runnable — exposes .active for direct use with
    bind_tools/ainvoke/astream, and .ainvoke_with_fallback for
    critical paths where fallback matters.
    """

    def __init__(self, providers: list[BaseChatModel], names: list[str]):
        if not providers:
            raise RuntimeError("No LLM providers configured with valid API keys")
        self._providers = providers
        self._names = names
        logger.info("llm_chain_ready", providers=names, primary=names[0])

    @property
    def active(self) -> BaseChatModel:
        return self._providers[0]

    async def ainvoke_with_fallback(self, messages, **kwargs):
        """Try each provider in order. First success wins."""
        last_error = None
        for provider, name in zip(self._providers, self._names):
            try:
                return await provider.ainvoke(messages, **kwargs)
            except Exception as e:
                last_error = e
                logger.warning("llm_provider_failed", provider=name, error=str(e)[:200])
        raise RuntimeError(f"All LLM providers failed. Last: {last_error}")


def _build_chain(max_tokens_override: Optional[int] = None) -> ProviderChainLLM:
    """Build provider chain from config."""
    cfg = _load_config()
    llm_cfg = cfg.get("llm", {})
    temperature = llm_cfg.get("temperature", 0.1)
    max_tokens = max_tokens_override or llm_cfg.get("max_tokens", 4096)

    providers, names = [], []
    for entry in llm_cfg.get("providers", []):
        provider = _build_provider(entry, max_tokens, temperature)
        if provider:
            providers.append(provider)
            names.append(entry["name"])

    return ProviderChainLLM(providers, names)


# -- Cached chain instances -------------------------------------------

_chain_cache: Optional[ProviderChainLLM] = None
_gate_chain_cache: Optional[ProviderChainLLM] = None


def _get_chain() -> ProviderChainLLM:
    global _chain_cache
    if _chain_cache is None:
        _chain_cache = _build_chain()
    return _chain_cache


def _get_gate_chain() -> ProviderChainLLM:
    global _gate_chain_cache
    if _gate_chain_cache is None:
        cfg = _load_config()
        gate_tokens = cfg.get("llm", {}).get("gate_max_tokens", 256)
        _gate_chain_cache = _build_chain(max_tokens_override=gate_tokens)
    return _gate_chain_cache


# -- Public API -------------------------------------------------------

def create_llm(tools: Optional[Sequence[BaseTool]] = None) -> BaseChatModel:
    """Return the primary agent LLM, optionally with tools bound.

    The underlying client is cached. bind_tools() returns a lightweight
    wrapper — safe to call per-invocation without allocating new clients.
    """
    llm = _get_chain().active
    if tools:
        return llm.bind_tools(tools)
    return llm


def create_gate_llm() -> BaseChatModel:
    """Return a lightweight LLM for the input gate (reduced max_tokens)."""
    return _get_gate_chain().active


def create_chain(tools: Optional[Sequence[BaseTool]] = None) -> ProviderChainLLM:
    """Return the full fallback chain for critical paths (e.g. input gate)."""
    return _get_chain()