"""Redis cache layer."""

from __future__ import annotations

import hashlib
import json
import logging

import redis.asyncio as redis

from config import settings

logger = logging.getLogger(__name__)

_pool: redis.Redis | None = None


async def get_redis() -> redis.Redis:
    global _pool
    if _pool is None:
        _pool = redis.from_url(settings.redis_url, decode_responses=True)
    return _pool


async def close_redis():
    global _pool
    if _pool:
        await _pool.aclose()
        _pool = None


def _cache_key(prefix: str, query: str, **kwargs) -> str:
    raw = json.dumps({"q": query, **kwargs}, sort_keys=True)
    h = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"mem:{prefix}:{h}"


async def get_cached_search(query: str, **filters) -> str | None:
    r = await get_redis()
    key = _cache_key("search", query, **filters)
    return await r.get(key)


async def set_cached_search(query: str, result_json: str, **filters):
    r = await get_redis()
    key = _cache_key("search", query, **filters)
    await r.set(key, result_json, ex=settings.cache_ttl_seconds)


async def store_conversation_cache(session_id: str, data: str):
    r = await get_redis()
    key = f"conv:{session_id}"
    await r.set(key, data, ex=settings.conversation_cache_ttl)


async def get_conversation_cache(session_id: str) -> str | None:
    r = await get_redis()
    key = f"conv:{session_id}"
    return await r.get(key)


async def check_dedup(content_hash: str) -> bool:
    """Returns True if content was recently stored (dedup hit)."""
    r = await get_redis()
    key = f"mem:dedup:{content_hash}"
    exists = await r.exists(key)
    if not exists:
        await r.set(key, "1", ex=300)  # 5 min dedup window
    return bool(exists)


async def invalidate_reminders():
    """Clear all reminder and search caches when reminder data changes."""
    r = await get_redis()
    keys = []
    # Clear all search caches (they use hashed keys, not readable names)
    async for key in r.scan_iter("mem:search:*"):
        keys.append(key)
    # Clear reminder-specific caches
    async for key in r.scan_iter("rem:*"):
        keys.append(key)
    if keys:
        await r.delete(*keys)


async def ping() -> bool:
    try:
        r = await get_redis()
        return await r.ping()
    except Exception:
        return False


# ── Hot Knowledge Cache ───────────────────────────────────

async def get_hot_cache(query_normalized: str) -> str | None:
    r = await get_redis()
    return await r.get(f"hot:{hashlib.sha256(query_normalized.encode()).hexdigest()[:16]}")


async def set_hot_cache(query_normalized: str, answer: str):
    r = await get_redis()
    # Hot knowledge stays cached for 24h
    await r.set(
        f"hot:{hashlib.sha256(query_normalized.encode()).hexdigest()[:16]}",
        answer,
        ex=86400,
    )
