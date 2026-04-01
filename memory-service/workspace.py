"""Workspace file indexing for OpenClaw memory-core replacement.

Scans workspace markdown files, chunks them, embeds with bge-m3,
stores in PostgreSQL pgvector — fully replaces SQLite-based memory-core.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# Chunking params — match OpenClaw defaults
CHUNK_TOKENS = 512
CHUNK_OVERLAP = 50
CHARS_PER_TOKEN = 4  # OpenClaw uses this estimate


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def chunk_markdown(content: str, tokens: int = CHUNK_TOKENS, overlap: int = CHUNK_OVERLAP) -> list[dict]:
    """Split markdown content into chunks, mimicking OpenClaw's chunkMarkdown."""
    lines = content.split("\n")
    if not lines:
        return []

    max_chars = max(32, tokens * CHARS_PER_TOKEN)
    overlap_chars = max(0, overlap * CHARS_PER_TOKEN)
    chunks: list[dict] = []
    current: list[tuple[str, int]] = []  # (line_text, 1-based line_no)
    current_chars = 0

    def flush():
        if not current:
            return
        text = "\n".join(line for line, _ in current)
        start_line = current[0][1]
        end_line = current[-1][1]
        chunks.append({
            "start_line": start_line,
            "end_line": end_line,
            "text": text,
            "hash": _hash_text(text),
        })

    def carry_overlap():
        nonlocal current, current_chars
        if overlap_chars <= 0 or not current:
            current = []
            current_chars = 0
            return
        acc = 0
        kept = []
        for line, line_no in reversed(current):
            acc += len(line) + 1
            kept.insert(0, (line, line_no))
            if acc >= overlap_chars:
                break
        current = kept
        current_chars = sum(len(l) + 1 for l, _ in kept)

    for i, line in enumerate(lines):
        line_no = i + 1
        line_chars = len(line) + 1
        if current_chars + line_chars > max_chars and current:
            flush()
            carry_overlap()
        current.append((line, line_no))
        current_chars += line_chars

    flush()
    return chunks


def list_workspace_files(workspace_dir: str) -> list[dict]:
    """List all .md files in workspace (matching OpenClaw's listMemoryFiles)."""
    workspace = Path(workspace_dir)
    if not workspace.is_dir():
        return []

    files = []
    for md_file in sorted(workspace.rglob("*.md")):
        # Skip hidden dirs and _legacy
        parts = md_file.relative_to(workspace).parts
        if any(p.startswith(".") for p in parts):
            continue

        try:
            stat = md_file.stat()
            content = md_file.read_text(encoding="utf-8")
            rel_path = str(md_file.relative_to(workspace))
            files.append({
                "path": rel_path,
                "abs_path": str(md_file),
                "hash": _hash_text(content),
                "mtime": int(stat.st_mtime),
                "size": stat.st_size,
                "content": content,
            })
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("Skipping %s: %s", md_file, e)
            continue

    return files


async def sync_workspace(pool, workspace_dir: str, embed_fn, force: bool = False) -> dict:
    """Sync workspace files to PostgreSQL.

    Returns: {indexed: int, unchanged: int, deleted: int, chunks: int}
    """
    files = list_workspace_files(workspace_dir)
    active_paths = {f["path"] for f in files}

    async with pool.acquire() as conn:
        # Get existing file hashes
        existing = await conn.fetch(
            "SELECT path, hash FROM memory.workspace_files"
        )
        existing_hashes = {r["path"]: r["hash"] for r in existing}

        indexed = 0
        unchanged = 0
        total_chunks = 0

        for file_entry in files:
            path = file_entry["path"]
            file_hash = file_entry["hash"]

            # Skip unchanged files
            if not force and existing_hashes.get(path) == file_hash:
                unchanged += 1
                continue

            # Chunk the file
            chunks = chunk_markdown(file_entry["content"])
            if not chunks:
                continue

            # Embed all chunks
            texts = [c["text"] for c in chunks]
            embeddings = embed_fn(texts)

            # Delete old chunks for this file
            await conn.execute(
                "DELETE FROM memory.workspace_chunks WHERE path = $1", path
            )

            # Insert new chunks
            for chunk, embedding in zip(chunks, embeddings):
                chunk_id = str(uuid.uuid4())
                embedding_str = f"[{','.join(str(x) for x in embedding)}]"
                await conn.execute(
                    """
                    INSERT INTO memory.workspace_chunks
                        (id, path, source, start_line, end_line, hash, text, embedding)
                    VALUES ($1, $2, 'memory', $3, $4, $5, $6, $7::vector)
                    """,
                    chunk_id, path, chunk["start_line"], chunk["end_line"],
                    chunk["hash"], chunk["text"], embedding_str,
                )
                total_chunks += 1

            # Upsert file record
            await conn.execute(
                """
                INSERT INTO memory.workspace_files (path, source, hash, mtime, size)
                VALUES ($1, 'memory', $2, $3, $4)
                ON CONFLICT (path) DO UPDATE SET
                    hash = EXCLUDED.hash, mtime = EXCLUDED.mtime, size = EXCLUDED.size
                """,
                path, file_hash, file_entry["mtime"], file_entry["size"],
            )
            indexed += 1

        # Delete stale files
        deleted = 0
        for stale_path in set(existing_hashes.keys()) - active_paths:
            await conn.execute(
                "DELETE FROM memory.workspace_chunks WHERE path = $1", stale_path
            )
            await conn.execute(
                "DELETE FROM memory.workspace_files WHERE path = $1", stale_path
            )
            deleted += 1

    return {
        "indexed": indexed,
        "unchanged": unchanged,
        "deleted": deleted,
        "chunks": total_chunks,
    }


async def search_workspace(pool, query_embedding: list[float], query_text: str,
                           max_results: int = 10, min_score: float = 0.0) -> list[dict]:
    """Hybrid search: vector (pgvector HNSW) + FTS (tsvector), merged."""
    embedding_str = f"[{','.join(str(x) for x in query_embedding)}]"

    async with pool.acquire() as conn:
        # Vector search
        vec_rows = await conn.fetch(
            """
            SELECT id, path, start_line, end_line, text, source,
                   1 - (embedding <=> $1::vector) AS score
            FROM memory.workspace_chunks
            ORDER BY embedding <=> $1::vector
            LIMIT $2
            """,
            embedding_str, max_results * 2,
        )

        # FTS search
        fts_rows = await conn.fetch(
            """
            SELECT id, path, start_line, end_line, text, source,
                   ts_rank_cd(content_tsv, plainto_tsquery('english', $1)) AS score
            FROM memory.workspace_chunks
            WHERE content_tsv @@ plainto_tsquery('english', $1)
            ORDER BY score DESC
            LIMIT $2
            """,
            query_text, max_results * 2,
        )

        # Merge: RRF (Reciprocal Rank Fusion)
        scores: dict[str, dict] = {}

        for rank, row in enumerate(vec_rows):
            rid = row["id"]
            rrf_score = 1.0 / (60 + rank)
            if rid not in scores:
                scores[rid] = {
                    "id": rid,
                    "path": row["path"],
                    "startLine": row["start_line"],
                    "endLine": row["end_line"],
                    "snippet": row["text"][:700],
                    "source": row["source"],
                    "score": 0.0,
                    "vec_score": float(row["score"]),
                }
            scores[rid]["score"] += rrf_score

        for rank, row in enumerate(fts_rows):
            rid = row["id"]
            rrf_score = 1.0 / (60 + rank)
            if rid not in scores:
                scores[rid] = {
                    "id": rid,
                    "path": row["path"],
                    "startLine": row["start_line"],
                    "endLine": row["end_line"],
                    "snippet": row["text"][:700],
                    "source": row["source"],
                    "score": 0.0,
                    "vec_score": 0.0,
                }
            scores[rid]["score"] += rrf_score

        results = sorted(scores.values(), key=lambda x: x["score"], reverse=True)
        results = [r for r in results if r.get("vec_score", 0) >= min_score]
        return results[:max_results]


async def workspace_status(pool, workspace_dir: str) -> dict:
    """Return workspace indexing status."""
    async with pool.acquire() as conn:
        file_count = await conn.fetchval(
            "SELECT COUNT(*) FROM memory.workspace_files"
        )
        chunk_count = await conn.fetchval(
            "SELECT COUNT(*) FROM memory.workspace_chunks"
        )
        dirty_files = 0
        # Check if any workspace files have changed
        files = list_workspace_files(workspace_dir)
        existing = await conn.fetch(
            "SELECT path, hash FROM memory.workspace_files"
        )
        existing_hashes = {r["path"]: r["hash"] for r in existing}
        active_paths = {f["path"] for f in files}

        for f in files:
            if existing_hashes.get(f["path"]) != f["hash"]:
                dirty_files += 1
        # Stale files count as dirty too
        stale = len(set(existing_hashes.keys()) - active_paths)
        dirty_files += stale

    return {
        "files": file_count,
        "chunks": chunk_count,
        "dirty": dirty_files > 0,
        "dirty_count": dirty_files,
        "workspace_dir": workspace_dir,
        "total_workspace_files": len(files),
    }
