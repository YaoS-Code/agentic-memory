"""Hybrid retrieval pipeline: vector + FTS + facts, with decay and MMR."""

from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np

from config import HALF_LIFE_MAP, settings
from embeddings import embed_single


def temporal_decay(created_at: datetime, half_life_days: float) -> float:
    """Exponential decay: score *= 2^(-age / half_life)."""
    now = datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_days = (now - created_at).total_seconds() / 86400
    return 2 ** (-age_days / half_life_days)


def reciprocal_rank_fusion(
    ranked_lists: list[list[dict]],
    id_key: str = "id",
    k: int = 60,
) -> list[dict]:
    """Merge multiple ranked lists using RRF."""
    scores: dict[str, float] = {}
    items: dict[str, dict] = {}

    for ranked in ranked_lists:
        for rank, item in enumerate(ranked):
            item_id = str(item[id_key])
            scores[item_id] = scores.get(item_id, 0) + 1.0 / (k + rank + 1)
            items[item_id] = item

    sorted_ids = sorted(scores, key=lambda x: scores[x], reverse=True)
    result = []
    for item_id in sorted_ids:
        item = items[item_id]
        item["_rrf_score"] = scores[item_id]
        result.append(item)
    return result


def apply_decay_and_boosts(items: list[dict]) -> list[dict]:
    """Apply temporal decay, importance boost, and access frequency boost."""
    for item in items:
        score = item.get("_rrf_score", item.get("similarity", item.get("rank", 0.5)))

        # Temporal decay
        category = item.get("category", "general")
        half_life = HALF_LIFE_MAP.get(category, HALF_LIFE_MAP["general"])
        decay_anchor = item.get("decay_anchor") or item.get("created_at")
        if decay_anchor:
            score *= temporal_decay(decay_anchor, half_life)

        # Importance boost
        importance = item.get("importance", 5)
        score *= importance / 5.0

        # Access frequency boost
        access_count = item.get("access_count", 0)
        score *= math.log2(1 + access_count) * 0.1 + 1.0

        item["_final_score"] = score

    items.sort(key=lambda x: x.get("_final_score", 0), reverse=True)
    return items


def mmr_rerank(
    items: list[dict],
    query_embedding: list[float],
    lambda_param: float = 0.7,
    top_k: int = 10,
) -> list[dict]:
    """Maximal Marginal Relevance re-ranking for diversity."""
    if len(items) <= 1:
        return items[:top_k]

    selected = []
    candidates = list(items)
    query_vec = np.array(query_embedding)

    while candidates and len(selected) < top_k:
        best_score = -float("inf")
        best_idx = 0

        for i, cand in enumerate(candidates):
            # Relevance to query (use final score as proxy)
            relevance = cand.get("_final_score", 0)

            # Max similarity to already selected items
            max_sim = 0.0
            if selected:
                cand_content = cand.get("content", "")
                for sel in selected:
                    sel_content = sel.get("content", "")
                    # Simple overlap-based similarity for efficiency
                    cand_words = set(cand_content.lower().split())
                    sel_words = set(sel_content.lower().split())
                    if cand_words:
                        overlap = len(cand_words & sel_words) / len(cand_words)
                        max_sim = max(max_sim, overlap)

            mmr_score = lambda_param * relevance - (1 - lambda_param) * max_sim
            if mmr_score > best_score:
                best_score = mmr_score
                best_idx = i

        selected.append(candidates.pop(best_idx))

    return selected


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for mixed EN/CN."""
    return max(1, len(text) // 3)


def pack_results_within_budget(
    items: list[dict],
    token_budget: int,
) -> tuple[list[dict], int]:
    """Pack results into token budget, preferring summaries over full content."""
    packed = []
    total_tokens = 0

    for item in items:
        # Prefer summary, fall back to truncated content
        text = item.get("summary") or item.get("content", "")
        tokens = estimate_tokens(text)

        # Truncate if over 200 tokens and no summary available
        if not item.get("summary") and tokens > 200:
            # Approximate truncation
            char_limit = 200 * 3
            text = text[:char_limit] + "..."
            tokens = 200

        if total_tokens + tokens > token_budget:
            # Try to fit at least something
            remaining = token_budget - total_tokens
            if remaining > 50:
                char_limit = remaining * 3
                text = text[:char_limit] + "..."
                tokens = remaining
            else:
                break

        item["_display_content"] = text
        item["_tokens"] = tokens
        total_tokens += tokens
        packed.append(item)

    return packed, total_tokens


async def hybrid_search(
    query: str,
    query_embedding: list[float],
    max_results: int = 5,
    categories: list[str] | None = None,
    min_importance: int | None = None,
    include_archived: bool = False,
    token_budget: int | None = None,
) -> tuple[list[dict], int]:
    """Full hybrid retrieval pipeline."""
    from storage import vector_search, fts_search

    budget = token_budget or settings.default_token_budget

    # Parallel searches (run sequentially for simplicity, both are fast)
    vec_results = await vector_search(
        query_embedding, limit=20,
        categories=categories, min_importance=min_importance,
        include_archived=include_archived,
    )
    fts_results = await fts_search(query, limit=20, include_archived=include_archived)

    # RRF merge
    merged = reciprocal_rank_fusion([vec_results, fts_results])

    # Apply decay and boosts
    scored = apply_decay_and_boosts(merged)

    # MMR re-rank
    reranked = mmr_rerank(scored, query_embedding, top_k=max_results * 2)

    # Pack within budget
    packed, total_tokens = pack_results_within_budget(reranked[:max_results * 2], budget)

    return packed[:max_results], total_tokens
