-- Agentic Memory System Schema
-- PostgreSQL + pgvector
--
-- Architecture:
--   memory.*          — Core shared layer (all skills)
--   skill_reminder.*  — Reminder & calendar skill
--   skill_task.*      — Async task queue

CREATE EXTENSION IF NOT EXISTS vector;

-- ================================================================
-- CORE: memory.* — shared by all skills
-- ================================================================
CREATE SCHEMA IF NOT EXISTS memory;

-- Vector memories (important information, insights, decisions)
CREATE TABLE IF NOT EXISTS memory.embeddings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content         TEXT NOT NULL,
    summary         TEXT,
    embedding       vector(1024) NOT NULL,
    category        TEXT NOT NULL DEFAULT 'general',
    tags            TEXT[] DEFAULT '{}',
    importance      SMALLINT DEFAULT 5 CHECK (importance BETWEEN 1 AND 10),
    access_count    INT DEFAULT 0,
    source          TEXT,
    source_ref      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_accessed   TIMESTAMPTZ DEFAULT now(),
    decay_anchor    TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_archived     BOOLEAN DEFAULT false
);

ALTER TABLE memory.embeddings ADD COLUMN IF NOT EXISTS content_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;

CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw ON memory.embeddings
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 200);
CREATE INDEX IF NOT EXISTS idx_embeddings_fts ON memory.embeddings USING gin(content_tsv);
CREATE INDEX IF NOT EXISTS idx_embeddings_category ON memory.embeddings (category) WHERE NOT is_archived;
CREATE INDEX IF NOT EXISTS idx_embeddings_created ON memory.embeddings (created_at DESC);

-- Structured facts (contacts, preferences, schedules)
CREATE TABLE IF NOT EXISTS memory.facts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain          TEXT NOT NULL,
    key             TEXT NOT NULL,
    value           JSONB NOT NULL,
    confidence      REAL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
    source          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ,
    is_active       BOOLEAN DEFAULT true,
    UNIQUE(domain, key)
);

CREATE INDEX IF NOT EXISTS idx_facts_domain ON memory.facts (domain) WHERE is_active;
CREATE INDEX IF NOT EXISTS idx_facts_expires ON memory.facts (expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_facts_value_gin ON memory.facts USING gin(value);

-- File metadata (photos, documents in MinIO)
CREATE TABLE IF NOT EXISTS memory.files (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    minio_key       TEXT NOT NULL UNIQUE,
    original_name   TEXT NOT NULL,
    mime_type       TEXT NOT NULL,
    size_bytes      BIGINT NOT NULL,
    description     TEXT,
    tags            TEXT[] DEFAULT '{}',
    metadata        JSONB DEFAULT '{}',
    embedding       vector(1024),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_deleted      BOOLEAN DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_files_mime ON memory.files (mime_type) WHERE NOT is_deleted;
CREATE INDEX IF NOT EXISTS idx_files_tags ON memory.files USING gin(tags) WHERE NOT is_deleted;
CREATE INDEX IF NOT EXISTS idx_files_embedding_hnsw ON memory.files
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 100);

-- Access log (for decay calculations)
CREATE TABLE IF NOT EXISTS memory.access_log (
    id              BIGSERIAL PRIMARY KEY,
    memory_id       UUID NOT NULL,
    memory_type     TEXT NOT NULL,
    accessed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    query_text      TEXT,
    score           REAL
);

CREATE INDEX IF NOT EXISTS idx_access_log_memory ON memory.access_log (memory_id, accessed_at DESC);

-- ================================================================
-- SKILL: skill_reminder.* — reminders & calendar feeds
-- ================================================================
CREATE SCHEMA IF NOT EXISTS skill_reminder;

CREATE TABLE IF NOT EXISTS skill_reminder.reminders (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           TEXT NOT NULL,
    description     TEXT,
    remind_at       TIMESTAMPTZ NOT NULL,
    repeat_rule     TEXT,
    repeat_until    TIMESTAMPTZ,
    category        TEXT DEFAULT 'general',
    tags            TEXT[] DEFAULT '{}',
    importance      SMALLINT DEFAULT 5 CHECK (importance BETWEEN 1 AND 10),
    cron_job_id     TEXT,
    source          TEXT DEFAULT 'manual',
    source_ref      TEXT,
    status          TEXT DEFAULT 'active',
    snoozed_until   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_reminders_remind_at ON skill_reminder.reminders (remind_at) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_reminders_status ON skill_reminder.reminders (status, remind_at);
CREATE INDEX IF NOT EXISTS idx_reminders_source ON skill_reminder.reminders (source, source_ref);

CREATE TABLE IF NOT EXISTS skill_reminder.calendar_feeds (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    url             TEXT NOT NULL,
    sync_interval   INT DEFAULT 3600,
    last_synced_at  TIMESTAMPTZ,
    last_sync_hash  TEXT,
    is_active       BOOLEAN DEFAULT true,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- ================================================================
-- SKILL: skill_task.* — async task queue
-- ================================================================
CREATE SCHEMA IF NOT EXISTS skill_task;

CREATE TABLE IF NOT EXISTS skill_task.queue (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           TEXT NOT NULL,
    command         TEXT NOT NULL,
    status          TEXT DEFAULT 'pending',
    result          TEXT,
    error           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    notify          BOOLEAN DEFAULT true
);

-- ================================================================
-- WORKSPACE: memory.workspace_* — workspace file indexing
-- Replaces OpenClaw's built-in SQLite memory-core with PostgreSQL
-- ================================================================

CREATE TABLE IF NOT EXISTS memory.workspace_files (
    path TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'memory',
    hash TEXT NOT NULL,
    mtime BIGINT NOT NULL,
    size BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory.workspace_chunks (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'memory',
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    hash TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT 'bge-m3',
    text TEXT NOT NULL,
    embedding vector(1024) NOT NULL,
    content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ws_chunks_hnsw
    ON memory.workspace_chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

CREATE INDEX IF NOT EXISTS idx_ws_chunks_fts
    ON memory.workspace_chunks
    USING gin (content_tsv);

CREATE INDEX IF NOT EXISTS idx_ws_chunks_path
    ON memory.workspace_chunks (path);

-- ================================================================
-- KNOWLEDGE: memory.hot_knowledge — frequently accessed knowledge
-- ================================================================

CREATE TABLE IF NOT EXISTS memory.hot_knowledge (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL,
    access_count INT DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ================================================================
-- LEARNINGS: memory.learnings — self-improvement records
-- ================================================================

CREATE TABLE IF NOT EXISTS memory.learnings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    category TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
