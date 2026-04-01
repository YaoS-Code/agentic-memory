"""Agentic Memory Service — FastAPI application.

Production-grade personalized memory for AI agents.
PostgreSQL + pgvector + bge-m3 + Redis + MinIO.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, File, Form, UploadFile, Body

from config import settings
from models import (
    CompactRequest, CompactResponse,
    ExtractRequest, ExtractResponse,
    FactQueryRequest, FactResult, FactStoreRequest, FactStoreResponse,
    FileResult, FileSearchRequest,
    HealthResponse, RecallRequest, RecallResponse,
    SearchRequest, SearchResponse, SearchResult,
    StoreRequest, StoreResponse,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting memory service...")
    import embeddings
    embeddings.preload()

    import storage
    await storage.get_pool()

    import cache
    await cache.get_redis()

    import files
    files.get_minio()

    logger.info("Memory service ready on %s:%d", settings.host, settings.port)
    yield
    # Shutdown
    await storage.close_pool()
    await cache.close_redis()


app = FastAPI(title="Agentic Memory Service", lifespan=lifespan)


# ── Health ─────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
async def health():
    import cache, files, storage
    from embeddings import _model
    return HealthResponse(
        status="ok",
        pg=await storage.ping(),
        redis=await cache.ping(),
        minio=files.ping(),
        embedding_model_loaded=_model is not None,
    )


# ── Store ──────────────────────────────────────────────────

@app.get("/store")
async def store_list(limit: int = 20, category: str | None = None):
    """List recent memories."""
    import storage
    pool = await storage.get_pool()
    conditions = ["NOT is_archived"]
    params = []
    idx = 1
    if category:
        conditions.append(f"category = ${idx}")
        params.append(category)
        idx += 1
    where = " AND ".join(conditions)
    params.append(min(limit, 50))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, content, summary, category, importance, access_count,
                   tags, source, created_at, last_accessed
            FROM memory.embeddings
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ${idx}
            """,
            *params,
        )
    results = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        for k in ("created_at", "last_accessed"):
            if d.get(k):
                d[k] = d[k].isoformat()
        results.append(d)
    return {"memories": results, "total": len(results)}


@app.post("/store", response_model=StoreResponse)
async def store(req: StoreRequest):
    import cache as cache_mod
    import storage
    from embeddings import embed_single

    # Dedup check
    content_hash = hashlib.sha256(req.content.encode()).hexdigest()[:16]
    if await cache_mod.check_dedup(content_hash):
        return StoreResponse(id="dedup", tier="skipped", summary="Duplicate content within 5min window")

    tier = req.tier or _classify_tier(req)

    if tier == "cache":
        await cache_mod.store_conversation_cache(
            content_hash, req.content
        )
        return StoreResponse(id=content_hash, tier="cache")

    elif tier == "vector":
        embedding = embed_single(req.content)
        row_id = await storage.store_embedding(
            content=req.content,
            embedding=embedding,
            category=req.category,
            importance=req.importance,
            tags=req.tags,
            source=req.source,
            source_ref=req.source_ref,
        )
        return StoreResponse(id=row_id, tier="vector")

    elif tier == "fact":
        if not req.domain or not req.key:
            return StoreResponse(id="error", tier="fact", summary="domain and key required for fact tier")
        fact_id, created = await storage.upsert_fact(
            domain=req.domain,
            key=req.key,
            value=req.value if req.value is not None else req.content,
            source=req.source,
            expires_at=req.expires_at,
        )
        return StoreResponse(id=fact_id, tier="fact", summary=f"{'created' if created else 'updated'}")

    return StoreResponse(id="error", tier="unknown", summary=f"Unknown tier: {tier}")


def _classify_tier(req: StoreRequest) -> str:
    """Auto-classify tier when not explicitly specified."""
    if req.domain and req.key:
        return "fact"
    if req.importance >= 7 or req.category in ("skill", "decision", "insight"):
        return "vector"
    if len(req.content) < 200 and req.importance <= 3:
        return "cache"
    return "vector"


def normalize_query(q: str) -> str:
    """Normalize query for hot knowledge matching."""
    import re as _re
    q = q.lower().strip()
    q = _re.sub(r'[^\w\s]', '', q)
    q = _re.sub(r'\s+', ' ', q)
    return q


# ── Search ─────────────────────────────────────────────────

@app.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest):
    import cache as cache_mod
    import storage
    from embeddings import embed_single
    from retrieval import hybrid_search

    # Check cache
    cache_key_data = {"tiers": req.tiers, "cats": req.categories, "min_imp": req.min_importance}
    cached = await cache_mod.get_cached_search(req.query, **cache_key_data)
    if cached:
        data = json.loads(cached)
        return SearchResponse(**data)

    results: list[SearchResult] = []
    total_tokens = 0

    # Vector + FTS search
    if "vector" in req.tiers:
        query_emb = embed_single(req.query, is_query=True)
        items, tokens = await hybrid_search(
            query=req.query,
            query_embedding=query_emb,
            max_results=req.max_results,
            categories=req.categories,
            min_importance=req.min_importance,
            include_archived=req.include_archived,
            token_budget=req.token_budget,
        )
        total_tokens += tokens

        for item in items:
            now = datetime.now(timezone.utc)
            created = item.get("created_at")
            if created and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = (now - created).total_seconds() / 86400 if created else None

            results.append(SearchResult(
                id=str(item["id"]),
                content=item.get("_display_content", item.get("summary", item["content"])),
                score=round(item.get("_final_score", 0), 4),
                category=item.get("category"),
                age_days=round(age, 1) if age is not None else None,
                tier="vector",
            ))

            await storage.log_access(
                str(item["id"]), "embedding", req.query, item.get("_final_score")
            )

    # Fact search
    if "fact" in req.tiers:
        facts = await storage.query_facts(search=req.query, limit=req.max_results)
        for f in facts:
            val_str = json.dumps(f["value"]) if isinstance(f["value"], (dict, list)) else str(f["value"])
            results.append(SearchResult(
                id=str(f["id"]),
                content=f"{f['domain']}/{f['key']}: {val_str}",
                score=f.get("confidence", 1.0),
                tier="fact",
                domain=f["domain"],
                key=f["key"],
            ))

    response = SearchResponse(results=results, total_tokens_estimate=total_tokens)

    if results:
        await cache_mod.set_cached_search(
            req.query, response.model_dump_json(), **cache_key_data
        )

    # Auto-warmup: if same query searched 3+ times, promote to hot knowledge
    try:
        import storage as _st
        normalized = normalize_query(req.query)
        freq = await _st.check_query_frequency(normalized, threshold=3)
        if freq >= 3 and results:
            top_answer = " | ".join(r.content[:100] for r in results[:3])
            source_ids = [r.id for r in results[:3] if r.tier == "vector"]
            await _st.upsert_hot(normalized, top_answer, source_ids)
            import cache as _cm
            await _cm.set_hot_cache(normalized, top_answer)
    except Exception:
        pass

    return response


# ── Recall ─────────────────────────────────────────────────

@app.post("/recall", response_model=RecallResponse)
async def recall(req: RecallRequest):
    import storage
    from embeddings import embed_single
    from retrieval import hybrid_search

    all_db_facts = await storage.query_facts(limit=100)
    fact_dicts = [{"domain": f["domain"], "key": f["key"], "value": f["value"]} for f in all_db_facts]

    pool = await storage.get_pool()
    async with pool.acquire() as conn:
        recent_rows = await conn.fetch(
            """
            SELECT id, content, summary, category, importance, access_count,
                   created_at, last_accessed, decay_anchor
            FROM memory.embeddings
            WHERE NOT is_archived
            ORDER BY created_at DESC
            LIMIT 20
            """
        )

    memories = []
    now = datetime.now(timezone.utc)
    for item in recent_rows:
        created = item.get("created_at")
        if created and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = (now - created).total_seconds() / 86400 if created else None

        memories.append(SearchResult(
            id=str(item["id"]),
            content=item.get("summary") or item["content"],
            score=round(item.get("importance", 5) / 10, 2),
            category=item.get("category"),
            age_days=round(age, 1) if age is not None else None,
            tier="vector",
        ))

    if req.context:
        query_emb = embed_single(req.context, is_query=True)
        items, _ = await hybrid_search(
            query=req.context,
            query_embedding=query_emb,
            max_results=5,
            token_budget=settings.recall_token_budget,
        )
        seen_ids = {m.id for m in memories}
        for item in items:
            item_id = str(item["id"])
            if item_id in seen_ids:
                continue
            created = item.get("created_at")
            if created and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = (now - created).total_seconds() / 86400 if created else None
            memories.append(SearchResult(
                id=item_id,
                content=item.get("_display_content", item.get("summary", item["content"])),
                score=round(item.get("_final_score", 0), 4),
                category=item.get("category"),
                age_days=round(age, 1) if age is not None else None,
                tier="vector",
            ))

    return RecallResponse(
        facts=fact_dicts,
        memories=memories,
        total_tokens_estimate=sum(len(str(f)) // 3 for f in fact_dicts) + sum(len(m.content) // 3 for m in memories),
    )


# ── Compact ───────────────────────────────────────────────

@app.post("/compact", response_model=CompactResponse)
async def compact(req: CompactRequest):
    """Compact a long conversation into a structured summary."""
    from compact import compact_messages, should_compact

    if not req.force and not should_compact(
        req.messages, req.msg_threshold, req.token_threshold
    ):
        from compact import estimate_messages_tokens
        return CompactResponse(
            summary="",
            token_estimate=0,
            original_tokens=estimate_messages_tokens(req.messages),
            original_count=len(req.messages),
            compression_ratio=1.0,
            compacted=False,
        )

    result = await compact_messages(req.messages, req.max_summary_tokens)
    return CompactResponse(**result, compacted=True)


# ── Extract ───────────────────────────────────────────────

@app.post("/extract", response_model=ExtractResponse)
async def extract(req: ExtractRequest):
    """Extract memorable information from a conversation."""
    from extract import extract_memories, should_extract

    if not req.force and not should_extract(
        req.messages, req.last_extract_index,
        req.token_threshold, req.msg_threshold,
    ):
        return ExtractResponse(skipped=True)

    items = await extract_memories(req.messages, req.last_extract_index)

    stored_count = 0
    if req.auto_store and items:
        for item in items:
            try:
                store_req = StoreRequest(**item)
                await store(store_req)
                stored_count += 1
            except Exception as e:
                logger.warning("Failed to auto-store extraction: %s", e)

    return ExtractResponse(
        extracted=items,
        stored_count=stored_count,
        skipped=False,
    )


# ── Hot Knowledge ──────────────────────────────────────────

@app.get("/hot")
async def hot_query(q: str):
    """Fast hot knowledge lookup. Returns cached answer or null."""
    import cache as cache_mod
    import storage

    normalized = normalize_query(q)
    if not normalized:
        return {"answer": None}

    cached = await cache_mod.get_hot_cache(normalized)
    if cached:
        return {"answer": cached, "source": "redis_hot", "latency": "fast"}

    hot = await storage.get_hot(normalized)
    if hot:
        answer = hot["answer"]
        await cache_mod.set_hot_cache(normalized, answer)
        return {"answer": answer, "source": "db_hot", "hit_count": hot["hit_count"], "latency": "medium"}

    return {"answer": None, "source": "miss"}


# ── Learnings ──────────────────────────────────────────────

@app.post("/learnings")
async def learning_create(data: dict = Body(...)):
    """Record a learning (error, correction, knowledge_gap, best_practice)."""
    import storage
    learning_id, recurrence = await storage.store_learning(
        type=data["type"],
        summary=data["summary"],
        details=data.get("details"),
        area=data.get("area"),
        priority=data.get("priority", "medium"),
        pattern_key=data.get("pattern_key"),
        source=data.get("source", "conversation"),
        tags=data.get("tags", []),
    )
    result = {"id": learning_id, "recurrence": recurrence}
    if recurrence >= 3:
        result["promotion_candidate"] = True
        result["message"] = f"Pattern seen {recurrence} times — eligible for promotion"
    return result


@app.get("/learnings")
async def learning_list(type: str | None = None, status: str | None = None, limit: int = 20):
    import storage
    items = await storage.query_learnings(type=type, status=status, limit=limit)
    for item in items:
        for k in ("first_seen", "last_seen", "created_at"):
            if item.get(k):
                item[k] = item[k].isoformat()
        item["id"] = str(item["id"])
    return items


# ── Facts ──────────────────────────────────────────────────

@app.post("/facts", response_model=FactStoreResponse)
async def fact_store(req: FactStoreRequest):
    import storage
    fact_id, created = await storage.upsert_fact(
        domain=req.domain,
        key=req.key,
        value=req.value,
        source=req.source,
        confidence=req.confidence,
        expires_at=req.expires_at,
    )
    return FactStoreResponse(id=fact_id, domain=req.domain, key=req.key, upserted=True)


@app.get("/facts", response_model=list[FactResult])
async def fact_query(domain: str | None = None, key: str | None = None, search: str | None = None):
    import storage
    rows = await storage.query_facts(domain=domain, key=key, search=search)
    return [
        FactResult(
            id=str(r["id"]),
            domain=r["domain"],
            key=r["key"],
            value=r["value"],
            confidence=r["confidence"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


@app.delete("/facts/{fact_id}")
async def fact_delete(fact_id: str):
    import storage
    ok = await storage.delete_fact(fact_id)
    return {"deleted": ok}


# ── Files ──────────────────────────────────────────────────

@app.post("/files/upload")
async def file_upload(
    file: UploadFile = File(...),
    description: str = Form(default=""),
    tags: str = Form(default=""),
):
    import storage as storage_mod
    import files as files_mod
    from embeddings import embed_single

    data = await file.read()
    mime = file.content_type or "application/octet-stream"
    name = file.filename or "unknown"

    minio_key, size = files_mod.upload_file(data, name, mime)

    embedding = None
    if description:
        embedding = embed_single(description)

    pool = await storage_mod.get_pool()
    import uuid as uuid_mod
    row_id = str(uuid_mod.uuid4())
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    embedding_str = f"[{','.join(str(x) for x in embedding)}]" if embedding else None

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memory.files (id, minio_key, original_name, mime_type, size_bytes, description, tags, embedding)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::vector)
            """,
            uuid_mod.UUID(row_id), minio_key, name, mime, size, description or None,
            tag_list, embedding_str,
        )

    url = files_mod.get_presigned_url(minio_key)
    return {"id": row_id, "minio_key": minio_key, "presigned_url": url}


@app.post("/files/search", response_model=list[FileResult])
async def file_search(req: FileSearchRequest):
    import storage as storage_mod
    import files as files_mod

    pool = await storage_mod.get_pool()
    conditions = ["NOT is_deleted"]
    params: list = []
    idx = 1

    if req.mime_type:
        conditions.append(f"mime_type = ${idx}")
        params.append(req.mime_type)
        idx += 1
    if req.tags:
        conditions.append(f"tags && ${idx}")
        params.append(req.tags)
        idx += 1

    order_clause = "ORDER BY created_at DESC"
    if req.query:
        from embeddings import embed_single
        query_emb = embed_single(req.query, is_query=True)
        emb_str = f"[{','.join(str(x) for x in query_emb)}]"
        conditions.append("embedding IS NOT NULL")
        order_clause = f"ORDER BY embedding <=> ${idx}::vector"
        params.append(emb_str)
        idx += 1

    where = " AND ".join(conditions)
    params.append(req.max_results)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, minio_key, original_name, mime_type, description, created_at
            FROM memory.files
            WHERE {where}
            {order_clause}
            LIMIT ${idx}
            """,
            *params,
        )

    results = []
    for r in rows:
        url = files_mod.get_presigned_url(r["minio_key"])
        results.append(FileResult(
            id=str(r["id"]),
            original_name=r["original_name"],
            mime_type=r["mime_type"],
            description=r["description"],
            presigned_url=url,
            created_at=r["created_at"],
        ))

    return results


# ── OpenAI-compatible Embeddings ──────────────────────────

@app.post("/v1/embeddings")
async def openai_embeddings(data: dict = Body(...)):
    """OpenAI-compatible /v1/embeddings endpoint.

    Wraps local bge-m3 in OpenAI's format so any tool expecting
    the OpenAI embedding API can use local embeddings instead.
    """
    from embeddings import embed_texts

    raw_input = data.get("input", "")
    if isinstance(raw_input, str):
        inputs = [raw_input]
    else:
        inputs = list(raw_input)

    if not inputs:
        return {"object": "list", "data": [], "model": "bge-m3", "usage": {"prompt_tokens": 0, "total_tokens": 0}}

    vectors = embed_texts(inputs)
    embedding_data = [
        {"object": "embedding", "embedding": vec, "index": i}
        for i, vec in enumerate(vectors)
    ]
    prompt_tokens = sum(len(t) // 4 for t in inputs)

    return {
        "object": "list",
        "data": embedding_data,
        "model": "bge-m3",
        "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
    }


# ── Workspace File Indexing (replaces SQLite memory-core) ─────────────────────

WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", os.path.expanduser("~/.openclaw/workspace"))


@app.post("/workspace/sync")
async def workspace_sync(data: dict = Body(None)):
    """Sync workspace files to pgvector. Replaces `openclaw memory index`."""
    import storage
    import workspace
    from embeddings import embed_texts

    pool = await storage.get_pool()
    force = (data or {}).get("force", False)
    ws_dir = (data or {}).get("workspace_dir", WORKSPACE_DIR)

    result = await workspace.sync_workspace(pool, ws_dir, embed_texts, force=force)
    return result


@app.post("/workspace/search")
async def workspace_search(data: dict = Body(...)):
    """Search workspace files via pgvector + FTS hybrid."""
    import storage
    import workspace
    from embeddings import embed_single

    pool = await storage.get_pool()
    query = data.get("query", "")
    max_results = data.get("max_results", 10)
    min_score = data.get("min_score", 0.0)

    if not query.strip():
        return {"results": []}

    query_emb = embed_single(query, is_query=True)
    results = await workspace.search_workspace(
        pool, query_emb, query, max_results=max_results, min_score=min_score
    )
    return {"results": results}


@app.get("/workspace/status")
async def workspace_status_endpoint(workspace_dir: str = WORKSPACE_DIR):
    """Get workspace indexing status."""
    import storage
    import workspace

    pool = await storage.get_pool()
    status = await workspace.workspace_status(pool, workspace_dir)
    return status


@app.get("/workspace/read")
async def workspace_read(path: str, from_line: int = 0, lines: int = 0):
    """Read a workspace file by relative path."""
    from pathlib import Path as P

    ws = P(WORKSPACE_DIR)
    target = (ws / path).resolve()

    # Security: must be within workspace
    if not str(target).startswith(str(ws.resolve())):
        return {"text": "", "path": path, "error": "path outside workspace"}

    if not target.exists() or not target.suffix == ".md":
        return {"text": "", "path": path}

    try:
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"text": "", "path": path}

    if from_line or lines:
        file_lines = content.split("\n")
        start = max(0, from_line)
        count = lines if lines > 0 else len(file_lines)
        content = "\n".join(file_lines[start:start + count])

    return {"text": content, "path": path}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
