# Agentic Memory

**Production-grade personalized memory system for AI agents.**

Give your AI agent persistent, searchable, evolving memory — powered by PostgreSQL + pgvector + bge-m3. Built as an [OpenClaw](https://github.com/openclaw/openclaw) plugin that fully replaces the built-in SQLite memory with a unified PostgreSQL backend.

```
User: "Do you remember what we discussed about the deployment last week?"

Agent: *searches 44 conversation memories + 118 workspace file chunks*
Agent: "Yes — last Tuesday you decided to move from Vercel to Cloudflare Workers..."
```

## Why This Exists

AI agents forget everything between sessions. Most "memory" solutions are either:
- **Too simple** — just append to a text file
- **Too complex** — require a PhD in vector databases
- **Not unified** — workspace files and conversation history live in separate silos

Agentic Memory solves this with a single PostgreSQL backend that handles:

| Layer | What It Stores | How It's Searched |
|-------|---------------|-------------------|
| **Vector memories** | Conversations, decisions, insights | pgvector HNSW (semantic) |
| **Structured facts** | User preferences, contacts, config | Key-value lookup |
| **Workspace index** | SKILL.md, AGENTS.md, docs/*.md | Hybrid vector + FTS |
| **File attachments** | Images, PDFs, documents | MinIO + metadata search |
| **Access log** | What was recalled and when | Time-decay scoring |

One `memory_search` call queries everything. No need to know which backend holds the answer.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     AI Agent (OpenClaw)                      │
│                                                             │
│   memory_search tool          exec curl tool                │
│        │                           │                        │
│        ▼                           ▼                        │
│  ┌─────────────┐         ┌──────────────────┐               │
│  │ memory-api  │────────▶│  Memory Service  │               │
│  │  plugin     │  curl   │  FastAPI :18800   │               │
│  │             │         │                  │               │
│  │ Registers:  │         │  /search         │  Hybrid       │
│  │ • memory_   │         │  /store          │  vector+FTS   │
│  │   search    │         │  /facts          │               │
│  │ • memory_   │         │  /recall         │               │
│  │   get       │         │  /workspace/*    │               │
│  │ • runtime   │         │  /compact        │               │
│  └─────────────┘         │  /extract        │               │
│                          │  /v1/embeddings  │               │
│                          └────────┬─────────┘               │
│                                   │                         │
│                    ┌──────────────┼──────────────┐          │
│                    ▼              ▼              ▼          │
│              ┌──────────┐  ┌──────────┐  ┌──────────┐      │
│              │PostgreSQL│  │  Redis   │  │  MinIO   │      │
│              │+ pgvector│  │  cache   │  │  files   │      │
│              │+ HNSW    │  │  dedup   │  │          │      │
│              │+ tsvector│  │          │  │          │      │
│              └──────────┘  └──────────┘  └──────────┘      │
│                                                             │
│              Embedding: BAAI/bge-m3 (1024-dim, local)       │
└─────────────────────────────────────────────────────────────┘
```

## Quick Start

### 1. Start infrastructure

```bash
docker compose up -d
```

This starts PostgreSQL (with pgvector), Redis, and MinIO.

### 2. Install Memory Service

```bash
cd memory-service
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Run the service

```bash
uvicorn main:app --host 127.0.0.1 --port 18800
```

On first start, bge-m3 (568MB) downloads automatically. After that, startup takes ~3 seconds.

### 4. Verify

```bash
# Health check
curl http://localhost:18800/health
# → {"status":"ok","pg":true,"redis":true,"minio":true,"embedding_model_loaded":true}

# Store a memory
curl -X POST http://localhost:18800/store \
  -H "Content-Type: application/json" \
  -d '{"content": "User prefers dark mode and vim keybindings", "category": "preference"}'

# Search memories
curl -X POST http://localhost:18800/search \
  -H "Content-Type: application/json" \
  -d '{"query": "editor preferences", "max_results": 5}'
```

### 5. Install OpenClaw plugin

```bash
# Copy plugin to OpenClaw plugins directory
cp -r openclaw-plugin/memory-api ~/.openclaw/plugins/

# Symlink OpenClaw SDK (required for plugin imports)
ln -s $(npm root -g)/openclaw ~/.openclaw/plugins/memory-api/node_modules/openclaw

# Copy example config
cp config/openclaw.json.example ~/.openclaw/openclaw.json
# Edit with your settings

# Restart gateway
openclaw gateway restart

# Verify
openclaw memory status --deep
```

## How It Works — Step by Step

### Memory Storage (5 tiers)

```
User says something → Agent decides what's worth remembering
                           │
                    ┌──────┴──────┐
                    │  Tier Check │
                    └──────┬──────┘
                           │
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
    ┌──────────────┐ ┌──────────┐ ┌──────────────┐
    │    vector    │ │   fact   │ │    cache     │
    │              │ │          │ │              │
    │ Decisions,   │ │ Contacts,│ │ Conversation │
    │ insights,    │ │ prefs,   │ │ highlights   │
    │ project ctx  │ │ schedule │ │ (4hr TTL)    │
    │              │ │          │ │              │
    │ → pgvector   │ │ → facts  │ │ → Redis      │
    │   HNSW index │ │   table  │ │              │
    └──────────────┘ └──────────┘ └──────────────┘
```

**Vector memories** get embedded with bge-m3 (1024-dim) and stored in PostgreSQL with an HNSW index for fast approximate nearest-neighbor search.

**Facts** are structured key-value pairs (e.g., `domain=preference, key=timezone, value="America/Vancouver"`). Exact lookup, no embedding needed.

**Cache** is for short-lived conversation context. Stored in Redis with a 4-hour TTL. Deduplication via content hash prevents storing the same thing twice within 5 minutes.

### Memory Retrieval

When `memory_search("deployment strategy")` is called:

1. **Embed the query** with bge-m3 (query mode)
2. **Vector search** — pgvector HNSW finds top-N semantically similar memories
3. **FTS search** — PostgreSQL tsvector finds keyword matches
4. **Workspace search** — same hybrid search across indexed workspace files
5. **Merge** — Reciprocal Rank Fusion (RRF) combines results from all sources
6. **Time decay** — older memories are scored lower (configurable half-life per category)
7. **Return** — top results with path, line numbers, score, snippet

### Memory Decay

Memories fade over time using exponential decay:

```
score = raw_score × 2^(-age_days / half_life)
```

| Category | Half-life | Meaning |
|----------|-----------|---------|
| conversation_highlight | 14 days | Recent chat context fades fast |
| general | 30 days | General knowledge |
| project | 45 days | Project-specific context |
| insight | 60 days | Learned patterns |
| decision | 90 days | Important decisions persist |
| skill | 180 days | Skills/abilities last longest |

### Workspace File Indexing

The memory service indexes your OpenClaw workspace markdown files into PostgreSQL:

```
~/.openclaw/workspace/
├── AGENTS.md          ─┐
├── IDENTITY.md         │  Scanned, chunked,
├── SOUL.md             │  embedded with bge-m3,
├── memory/             │  stored in pgvector
│   ├── 2026-03-27.md   │
│   └── 2026-03-28.md  ─┘
└── skills/
    └── web-search/
        └── SKILL.md   ─── Also indexed
```

**Chunking strategy**: Markdown files are split into chunks of ~512 tokens with 50-token overlap. Each chunk gets a bge-m3 embedding and is stored with its file path + line range for precise citation.

**Incremental sync**: Only changed files are re-indexed (hash comparison). Call `POST /workspace/sync` after editing workspace files, or `POST /workspace/sync {"force": true}` for a full re-index.

### Auto-Extraction (Hooks)

The `auto-extract` hook runs at session boundaries (new session, reset, pre-compaction) to automatically extract durable memories from the conversation:

```
Session about to compact
    │
    ▼
auto-extract hook fires
    │
    ├─ Reads last 30 messages from session transcript
    ├─ Calls POST /extract with {messages, auto_store: true}
    │       │
    │       ▼
    │   Claude Haiku analyzes messages
    │   Extracts: [
    │     {tier: "fact", category: "preference", content: "User prefers tabs over spaces"},
    │     {tier: "vector", category: "decision", content: "Decided to use PostgreSQL over MongoDB"},
    │   ]
    │       │
    │       ▼
    │   Each extracted memory is stored via /store or /facts
    │
    └─ Session compacts normally
```

### Auto-Compaction

When conversations get long, the `/compact` endpoint summarizes them:

```bash
curl -X POST http://localhost:18800/compact \
  -H "Content-Type: application/json" \
  -d '{"messages": [...], "force": true}'
```

Returns a structured summary with sections:
- **Context** — what was being discussed
- **Key Decisions** — what was decided
- **Action Items** — what needs to be done
- **Important Details** — technical specifics
- **Current State** — where things stand

## API Reference

### Core Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Service health + component status |
| POST | `/store` | Store a memory (auto-classifies tier) |
| POST | `/search` | Semantic + FTS hybrid search |
| POST | `/recall` | Context recall (returns relevant memories for a topic) |
| POST | `/facts` | Store/query structured facts |
| GET | `/facts` | List all active facts |
| POST | `/compact` | Summarize conversation messages |
| POST | `/extract` | Extract durable memories from messages |

### Workspace Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/workspace/sync` | Sync workspace files to pgvector |
| POST | `/workspace/search` | Search indexed workspace chunks |
| GET | `/workspace/status` | Indexing status |
| GET | `/workspace/read` | Read a workspace file by path |

### OpenAI-Compatible Endpoint

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/embeddings` | bge-m3 embeddings in OpenAI format |

This endpoint allows any tool expecting OpenAI's embedding API to use local bge-m3 instead. No API key required.

## OpenClaw Plugin

The `memory-api` plugin registers a custom `MemoryRuntime` that fully replaces OpenClaw's built-in SQLite-based memory:

**What it replaces:**
- SQLite + sqlite-vec → PostgreSQL + pgvector (HNSW)
- SQLite FTS5 → PostgreSQL tsvector + GIN
- Local embedding cache → Redis
- File-based storage → MinIO

**What it registers:**
- `registerMemoryRuntime` — takes over `getMemorySearchManager`
- `registerTool("memory_search")` — hybrid search across all sources
- `registerTool("memory_get")` — read specific workspace file lines
- `registerMemoryPromptSection` — injects memory system instructions into agent prompt
- `registerMemoryFlushPlan` — pre-compaction memory persistence

**Why `execFile('curl')` instead of `fetch()`:**
The plugin runs inside OpenClaw's gateway Node.js process, which may sandbox `fetch()` calls to localhost. By spawning `curl` as a child process (via `child_process.execFile`), we bypass this sandbox — the same way OpenClaw's `exec` tool successfully calls localhost APIs.

## Requirements

- **PostgreSQL 15+** with pgvector extension
- **Redis 7+**
- **MinIO** (or any S3-compatible storage)
- **Python 3.11+** with ~600MB disk for bge-m3 model
- **Node.js 22+** (for OpenClaw plugin)
- **~2GB RAM** for bge-m3 embedding model

## Configuration

All settings are in `memory-service/config.py` and can be overridden via environment variables:

```bash
export MEMORY_PG_HOST=db.example.com
export MEMORY_PG_PASSWORD=your-secure-password
export MEMORY_REDIS_URL=redis://redis:6379/0
export MEMORY_TIMEZONE=America/Vancouver
```

See `config/openclaw.json.example` for OpenClaw integration config.

## Project Structure

```
agentic-memory/
├── memory-service/          # FastAPI backend
│   ├── main.py              # API endpoints
│   ├── config.py            # Configuration (env vars)
│   ├── embeddings.py        # bge-m3 embedding pipeline
│   ├── storage.py           # PostgreSQL operations
│   ├── workspace.py         # Workspace file indexing
│   ├── compact.py           # Conversation compaction
│   ├── extract.py           # Memory extraction from transcripts
│   ├── retrieval.py         # Search & retrieval logic
│   ├── cache.py             # Redis caching & dedup
│   ├── files.py             # MinIO file operations
│   ├── models.py            # Pydantic request/response models
│   ├── task_queue.py        # Async task execution
│   └── requirements.txt
├── openclaw-plugin/
│   ├── memory-api/          # OpenClaw memory plugin
│   │   ├── index.js         # Plugin entry (registerMemoryRuntime)
│   │   ├── package.json
│   │   └── openclaw.plugin.json
│   └── hooks/
│       └── auto-extract/    # Session memory extraction hook
│           ├── handler.js
│           └── HOOK.md
├── config/                  # Example configurations
│   ├── openclaw.json.example
│   └── AGENTS.md.example
├── sql/
│   └── schema.sql           # Full PostgreSQL schema
├── docker-compose.yml       # Infrastructure (pg + redis + minio)
└── docs/
    └── ARCHITECTURE.md      # Detailed architecture documentation
```

## Upgrading from OpenClaw 2026.3.13 → 2026.3.28+

If your agent lost `exec`, `read`, `write`, or `edit` tools after upgrading OpenClaw, see **[docs/UPGRADE-2026.3.28.md](docs/UPGRADE-2026.3.28.md)** for the fix.

**TL;DR**: The `tools.allow` field in `openclaw.json` acts as a whitelist. You must include `group:fs` and `group:runtime` for file and exec tools to work. Also, do NOT use `profile: "full"` — use `profile: "coding"` instead.

## Changelog

### 2026-04-02

- **Fix**: Document OpenClaw 2026.3.28 `tools.allow` whitelist issue that disables exec/read/write
- **Fix**: Document `profile: "full"` vs `profile: "coding"` gotcha (full = empty allowlist)
- **Fix**: Update `openclaw.json.example` with correct tool group allowlist
- **Docs**: Add `docs/UPGRADE-2026.3.28.md` — complete upgrade guide with exec fix, session cache clearing, and SQLite → PostgreSQL migration notes
- **Fix**: Use `process.env.HOME` instead of hardcoded paths in plugin

### 2026-03-21

- Initial release: Agentic Memory — PostgreSQL + pgvector + bge-m3 memory system for AI agents

## License

MIT

## Credits

Built by [NexAgent AI Solutions](https://nexagent.ca) as the memory backbone for the OpenClaw AI agent platform.

Inspired by the memory architecture patterns in [Claude Code](https://docs.anthropic.com/en/docs/claude-code).
