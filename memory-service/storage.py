"""PostgreSQL operations for memory storage and retrieval."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

import asyncpg

from config import settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=settings.pg_host,
            port=settings.pg_port,
            user=settings.pg_user,
            password=settings.pg_password,
            database=settings.pg_database,
            min_size=2,
            max_size=10,
        )
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def ping() -> bool:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:
        return False


# ── Vector Memory Operations ──────────────────────────────

async def store_embedding(
    content: str,
    embedding: list[float],
    category: str = "general",
    importance: int = 5,
    tags: list[str] | None = None,
    source: str | None = None,
    source_ref: str | None = None,
    summary: str | None = None,
) -> str:
    pool = await get_pool()
    row_id = str(uuid.uuid4())
    embedding_str = f"[{','.join(str(x) for x in embedding)}]"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memory.embeddings
                (id, content, summary, embedding, category, tags, importance, source, source_ref)
            VALUES ($1, $2, $3, $4::vector, $5, $6, $7, $8, $9)
            """,
            uuid.UUID(row_id), content, summary, embedding_str,
            category, tags or [], importance, source, source_ref,
        )
    return row_id


async def vector_search(
    query_embedding: list[float],
    limit: int = 20,
    categories: list[str] | None = None,
    min_importance: int | None = None,
    include_archived: bool = False,
) -> list[dict]:
    pool = await get_pool()
    embedding_str = f"[{','.join(str(x) for x in query_embedding)}]"

    conditions = []
    params: list = [embedding_str, limit]
    param_idx = 3

    if not include_archived:
        conditions.append("NOT is_archived")
    if categories:
        conditions.append(f"category = ANY(${param_idx})")
        params.append(categories)
        param_idx += 1
    if min_importance is not None:
        conditions.append(f"importance >= ${param_idx}")
        params.append(min_importance)
        param_idx += 1

    where = " AND ".join(conditions) if conditions else "TRUE"

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, content, summary, category, importance, access_count,
                   tags, source, created_at, last_accessed, decay_anchor,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM memory.embeddings
            WHERE {where}
            ORDER BY embedding <=> $1::vector
            LIMIT $2
            """,
            *params,
        )

    return [dict(r) for r in rows]


async def fts_search(query: str, limit: int = 20, include_archived: bool = False) -> list[dict]:
    pool = await get_pool()
    archive_filter = "" if include_archived else "AND NOT is_archived"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, content, summary, category, importance, access_count,
                   tags, source, created_at, last_accessed, decay_anchor,
                   ts_rank_cd(content_tsv, plainto_tsquery('english', $1)) AS rank
            FROM memory.embeddings
            WHERE content_tsv @@ plainto_tsquery('english', $1) {archive_filter}
            ORDER BY rank DESC
            LIMIT $2
            """,
            query, limit,
        )
    return [dict(r) for r in rows]


async def log_access(memory_id: str, memory_type: str, query_text: str | None, score: float | None):
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO memory.access_log (memory_id, memory_type, query_text, score)
                VALUES ($1, $2, $3, $4)
                """,
                uuid.UUID(memory_id), memory_type, query_text, score,
            )
            await conn.execute(
                """
                UPDATE memory.embeddings
                SET access_count = access_count + 1, last_accessed = now()
                WHERE id = $1
                """,
                uuid.UUID(memory_id),
            )
    except Exception as e:
        logger.warning("Failed to log access: %s", e)


# ── Fact Operations ───────────────────────────────────────

async def upsert_fact(
    domain: str,
    key: str,
    value,
    source: str | None = None,
    confidence: float = 1.0,
    expires_at: datetime | None = None,
) -> tuple[str, bool]:
    """Upsert a fact. Returns (id, was_created)."""
    pool = await get_pool()
    value_json = json.dumps(value)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO memory.facts (domain, key, value, source, confidence, expires_at)
            VALUES ($1, $2, $3::jsonb, $4, $5, $6)
            ON CONFLICT (domain, key) DO UPDATE SET
                value = EXCLUDED.value,
                source = COALESCE(EXCLUDED.source, memory.facts.source),
                confidence = EXCLUDED.confidence,
                expires_at = EXCLUDED.expires_at,
                updated_at = now(),
                is_active = true
            RETURNING id, (xmax = 0) AS was_created
            """,
            domain, key, value_json, source, confidence, expires_at,
        )
    return str(row["id"]), row["was_created"]


async def query_facts(
    domain: str | None = None,
    key: str | None = None,
    search: str | None = None,
    limit: int = 50,
) -> list[dict]:
    pool = await get_pool()
    conditions = ["is_active"]
    params: list = []
    idx = 1

    if domain:
        conditions.append(f"domain = ${idx}")
        params.append(domain)
        idx += 1
    if key:
        conditions.append(f"key = ${idx}")
        params.append(key)
        idx += 1
    if search:
        conditions.append(f"(key ILIKE ${idx} OR value::text ILIKE ${idx})")
        params.append(f"%{search}%")
        idx += 1

    where = " AND ".join(conditions)
    params.append(limit)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, domain, key, value, confidence, source, created_at, updated_at, expires_at
            FROM memory.facts
            WHERE {where}
            ORDER BY updated_at DESC
            LIMIT ${idx}
            """,
            *params,
        )
    return [dict(r) for r in rows]


async def delete_fact(fact_id: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE memory.facts SET is_active = false WHERE id = $1",
            uuid.UUID(fact_id),
        )
    return "UPDATE 1" in result


# ── Maintenance ───────────────────────────────────────────

async def archive_old_memories():
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE memory.embeddings SET is_archived = true
            WHERE NOT is_archived
              AND importance < 5
              AND access_count < 3
              AND created_at < now() - interval '90 days'
              AND last_accessed < now() - interval '60 days'
            """
        )
    return result


async def expire_facts():
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE memory.facts SET is_active = false WHERE expires_at < now() AND is_active"
        )
    return result


# ── Hot Knowledge Operations ──────────────────────────────

async def get_hot(query_pattern: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE memory.hot_knowledge SET hit_count = hit_count + 1, last_hit = now()
            WHERE query_pattern = $1
            RETURNING id, query_pattern, answer, source_ids, hit_count, last_hit, created_at
            """,
            query_pattern,
        )
    return dict(row) if row else None


async def upsert_hot(query_pattern: str, answer: str, source_ids: list[str] | None = None) -> str:
    pool = await get_pool()
    import uuid as uuid_mod
    src_uuids = [uuid.UUID(s) for s in (source_ids or [])]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO memory.hot_knowledge (query_pattern, answer, source_ids)
            VALUES ($1, $2, $3)
            ON CONFLICT (query_pattern) DO UPDATE SET
                answer = EXCLUDED.answer,
                source_ids = EXCLUDED.source_ids,
                hit_count = memory.hot_knowledge.hit_count + 1,
                last_hit = now()
            RETURNING id
            """,
            query_pattern, answer, src_uuids,
        )
    return str(row["id"])


async def check_query_frequency(query_normalized: str, threshold: int = 3) -> int:
    """Check how many times a normalized query appears in access_log."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            """
            SELECT count(*) FROM memory.access_log
            WHERE query_text = $1
            AND accessed_at > now() - interval '30 days'
            """,
            query_normalized,
        )
    return count or 0


# ── Learning Operations ───────────────────────────────────

async def store_learning(
    type: str,
    summary: str,
    details: str | None = None,
    area: str | None = None,
    priority: str = "medium",
    pattern_key: str | None = None,
    source: str | None = None,
    tags: list[str] | None = None,
) -> tuple[str, int]:
    """Store a learning. Returns (id, recurrence_count).
    If pattern_key matches existing, increments recurrence."""
    pool = await get_pool()

    if pattern_key:
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id, recurrence FROM memory.learnings WHERE pattern_key = $1 AND status = 'pending'",
                pattern_key,
            )
            if existing:
                new_count = existing["recurrence"] + 1
                await conn.execute(
                    """
                    UPDATE memory.learnings
                    SET recurrence = $1, last_seen = now(), details = COALESCE($2, details)
                    WHERE id = $3
                    """,
                    new_count, details, existing["id"],
                )
                return str(existing["id"]), new_count

    row_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memory.learnings (id, type, summary, details, area, priority, pattern_key, source, tags)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            uuid.UUID(row_id), type, summary, details, area, priority, pattern_key, source, tags or [],
        )
    return row_id, 1


async def query_learnings(
    type: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict]:
    pool = await get_pool()
    conditions = []
    params = []
    idx = 1
    if type:
        conditions.append(f"type = ${idx}")
        params.append(type)
        idx += 1
    if status:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    params.append(limit)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT * FROM memory.learnings WHERE {where}
            ORDER BY recurrence DESC, created_at DESC LIMIT ${idx}
            """,
            *params,
        )
    return [dict(r) for r in rows]


async def resolve_learning(learning_id: str, notes: str | None = None) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE memory.learnings SET status = 'resolved' WHERE id = $1",
            uuid.UUID(learning_id),
        )
    return "UPDATE 1" in result


async def get_promotable_learnings(min_recurrence: int = 3, days: int = 30) -> list[dict]:
    """Find learnings ready for promotion: recurring 3+ times within N days."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM memory.learnings
            WHERE status = 'pending'
              AND recurrence >= $1
              AND last_seen > now() - make_interval(days => $2)
            ORDER BY recurrence DESC
            """,
            min_recurrence, days,
        )
    return [dict(r) for r in rows]


async def mark_promoted(learning_id: str, promoted_to: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE memory.learnings SET status = 'promoted', promoted_to = $1 WHERE id = $2",
            promoted_to, uuid.UUID(learning_id),
        )
    return "UPDATE 1" in result
