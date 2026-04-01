"""Pydantic models for request/response."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Store ──────────────────────────────────────────────────

class StoreRequest(BaseModel):
    content: str
    tier: str | None = None  # cache, vector, fact, file
    category: str = "general"
    importance: int = Field(default=5, ge=1, le=10)
    tags: list[str] = []
    source: str | None = None
    source_ref: str | None = None
    # For fact tier
    domain: str | None = None
    key: str | None = None
    value: Any | None = None
    expires_at: datetime | None = None


class StoreResponse(BaseModel):
    id: str
    tier: str
    summary: str | None = None


# ── Search ─────────────────────────────────────────────────

class SearchRequest(BaseModel):
    query: str
    max_results: int = Field(default=5, ge=1, le=20)
    tiers: list[str] = ["vector", "fact"]
    categories: list[str] | None = None
    min_importance: int | None = None
    include_archived: bool = False
    token_budget: int | None = None


class SearchResult(BaseModel):
    id: str
    content: str
    score: float
    category: str | None = None
    age_days: float | None = None
    tier: str
    domain: str | None = None
    key: str | None = None


class SearchResponse(BaseModel):
    results: list[SearchResult]
    total_tokens_estimate: int = 0


# ── Recall ─────────────────────────────────────────────────

class RecallRequest(BaseModel):
    context: str | None = None


class RecallResponse(BaseModel):
    facts: list[dict[str, Any]] = []
    memories: list[SearchResult] = []
    total_tokens_estimate: int = 0


# ── Facts ──────────────────────────────────────────────────

class FactStoreRequest(BaseModel):
    domain: str
    key: str
    value: Any
    source: str | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    expires_at: datetime | None = None


class FactStoreResponse(BaseModel):
    id: str
    domain: str
    key: str
    upserted: bool


class FactQueryRequest(BaseModel):
    domain: str | None = None
    key: str | None = None
    search: str | None = None


class FactResult(BaseModel):
    id: str
    domain: str
    key: str
    value: Any
    confidence: float
    updated_at: datetime


# ── Files ──────────────────────────────────────────────────

class FileSearchRequest(BaseModel):
    query: str | None = None
    mime_type: str | None = None
    tags: list[str] | None = None
    max_results: int = 10


class FileResult(BaseModel):
    id: str
    original_name: str
    mime_type: str
    description: str | None = None
    presigned_url: str | None = None
    created_at: datetime


# ── Extract ───────────────────────────────────────────────

class ExtractRequest(BaseModel):
    messages: list[dict]
    last_extract_index: int = Field(default=0, ge=0)
    token_threshold: int = Field(default=3000, ge=500)
    msg_threshold: int = Field(default=10, ge=3)
    force: bool = False  # Skip threshold check
    auto_store: bool = True  # Automatically store extracted memories


class ExtractResponse(BaseModel):
    extracted: list[dict] = []
    stored_count: int = 0
    skipped: bool = False  # True if below threshold and not forced


# ── Compact ───────────────────────────────────────────────

class CompactRequest(BaseModel):
    messages: list[dict]
    max_summary_tokens: int = Field(default=2000, ge=200, le=8000)
    msg_threshold: int = Field(default=20, ge=5)
    token_threshold: int = Field(default=6000, ge=1000)
    force: bool = False  # Skip threshold check


class CompactResponse(BaseModel):
    summary: str
    token_estimate: int
    original_tokens: int
    original_count: int
    compression_ratio: float
    compacted: bool  # False if below threshold and not forced


# ── Health ─────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    pg: bool = False
    redis: bool = False
    minio: bool = False
    embedding_model_loaded: bool = False
