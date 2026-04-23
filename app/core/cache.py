"""Redis cache — singleton client, decorator, prefix invalidation.

Usage:
    from app.core.cache import cached, get_cache

    @cached(ttl=300, prefix="listings")
    def search_listings(neighborhood: str, min_price: int = 0):
        ...

    # After scoring pipeline writes new data:
    get_cache().invalidate("listings")

    # FastAPI dependency:
    cache = Depends(get_cache)

Graceful degradation: if Redis is unreachable, all cache operations
are no-ops. The function executes normally without caching. No
exceptions propagated to callers.
"""

import json
import time
import hashlib
import functools
from typing import Any, Callable

import redis
import structlog

from app.config import get_settings

logger = structlog.get_logger()

# Sentinel value to distinguish "not in cache" from "cached None"
_MISS = object()


class RedisCache:
    """Singleton Redis client with JSON serialization."""

    _instance: "RedisCache | None" = None

    @classmethod
    def get_instance(cls) -> "RedisCache":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls):
        """Tear down singleton. Used in tests."""
        if cls._instance and cls._instance._client:
            try:
                cls._instance._client.close()
            except Exception:
                pass
        cls._instance = None

    def __init__(self):
        settings = get_settings()
        self._enabled = bool(settings.redis_url)

        if not self._enabled:
            self._client = None
            return

        try:
            self._client = redis.Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=2,
                retry_on_timeout=True,
            )
            self._client.ping()
            logger.info("redis_connected", url=self._redact_url(settings.redis_url))
        except Exception as e:
            logger.warning("redis_unavailable", error=str(e)[:100])
            self._client = None
            self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled and self._client is not None

    @staticmethod
    def _redact_url(url: str) -> str:
        """Strip password from URL for logging."""
        if "@" in url:
            return url.split("@", 1)[1]
        return url

    # ── Core operations ──────────────────────────────────────

    def get(self, key: str) -> Any:
        """Fetch and deserialize. Returns _MISS sentinel on miss/error."""
        if not self.enabled:
            return _MISS
        try:
            raw = self._client.get(key)
            if raw is None:
                return _MISS
            return json.loads(raw)
        except Exception as e:
            logger.debug("cache_get_error", key=key, error=str(e)[:80])
            return _MISS

    def set(self, key: str, value: Any, ttl: int = 300):
        """Serialize and store with TTL in seconds."""
        if not self.enabled:
            return
        try:
            self._client.setex(key, ttl, json.dumps(value, default=str))
        except Exception as e:
            logger.debug("cache_set_error", key=key, error=str(e)[:80])

    def delete(self, key: str):
        """Remove a single key."""
        if not self.enabled:
            return
        try:
            self._client.delete(key)
        except Exception:
            pass

    def invalidate(self, prefix: str):
        """Delete all keys matching prefix. Uses SCAN to avoid blocking."""
        if not self.enabled:
            return
        try:
            cursor = 0
            pattern = f"{prefix}:*"
            total = 0
            while True:
                cursor, keys = self._client.scan(cursor, match=pattern, count=200)
                if keys:
                    self._client.delete(*keys)
                    total += len(keys)
                if cursor == 0:
                    break
            if total:
                logger.info("cache_invalidated", prefix=prefix, keys=total)
        except Exception as e:
            logger.warning("cache_invalidate_error", prefix=prefix, error=str(e)[:80])

    def ping(self) -> tuple[bool, int]:
        """Health check. Returns (is_healthy, latency_ms)."""
        if not self.enabled:
            return False, 0
        try:
            start = time.perf_counter()
            self._client.ping()
            ms = int((time.perf_counter() - start) * 1000)
            return True, ms
        except Exception:
            return False, 0

    def flush_db(self):
        """Clear entire database. Use only in tests."""
        if not self.enabled:
            return
        self._client.flushdb()


# ── Convenience accessor ─────────────────────────────────────

def get_cache() -> RedisCache:
    """Return the singleton. Safe to call from FastAPI Depends()."""
    return RedisCache.get_instance()


# ── Cache key builder ────────────────────────────────────────

def _build_key(prefix: str, func: Callable, args: tuple, kwargs: dict) -> str:
    """Deterministic cache key from function signature + arguments.

    Uses MD5 hash of args to keep keys short and safe for Redis.
    Format: prefix:function_name:arg_hash
    """
    parts = [str(a) for a in args]
    parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
    raw = "|".join(parts)
    arg_hash = hashlib.md5(raw.encode()).hexdigest()[:12]
    return f"{prefix}:{func.__name__}:{arg_hash}"


# ── Decorator ────────────────────────────────────────────────

def cached(ttl: int = 300, prefix: str = "default"):
    """Cache function results in Redis.

    Args:
        ttl: Time-to-live in seconds.
        prefix: Key namespace. Use for grouped invalidation.
                cache.invalidate("listings") clears all @cached(prefix="listings").

    Works with sync functions. If Redis is down, the function
    executes normally — cache is always best-effort.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            cache = get_cache()
            if not cache.enabled:
                return func(*args, **kwargs)

            key = _build_key(prefix, func, args, kwargs)

            # Check cache
            hit = cache.get(key)
            if hit is not _MISS:
                logger.debug("cache_hit", key=key)
                return hit

            # Miss — execute and store
            result = func(*args, **kwargs)
            cache.set(key, result, ttl=ttl)
            logger.debug("cache_miss", key=key, ttl=ttl)
            return result

        # Expose manual invalidation on the decorated function
        wrapper.invalidate = lambda: get_cache().invalidate(prefix)
        wrapper.cache_prefix = prefix
        return wrapper

    return decorator