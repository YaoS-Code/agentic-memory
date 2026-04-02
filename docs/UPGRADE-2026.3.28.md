# Upgrading to OpenClaw 2026.3.28+ — Exec Tool Fix & SQLite Migration

## Background

OpenClaw 2026.3.28 (released March 31, 2026) changed how tool profiles work. If your agent lost `exec`, `read`, `write`, or `edit` tools after upgrading, this guide explains the root cause and fix.

## Problem: Agent Loses exec After Upgrade

### Symptoms

- Agent says "没有 exec 权限" or "tool permissions are none"
- Agent can only use `web_search`, `web_fetch`, `cron`, `gateway`
- Agent cannot run shell commands or read/write files
- `openclaw agent -m "list your tools" --json` shows only 3-4 tools instead of 17+

### Root Cause

OpenClaw's tool system uses **profiles** (`minimal`, `coding`, `messaging`, `full`) and an **allowlist** (`tools.allow`) together. The tools available to the agent are the **intersection** of these two:

```
Available tools = (profile tools) ∩ (allowlist)
```

**The bug**: If `agents.list[].tools.allow` only contains `["group:web", "group:automation"]`, it acts as a **whitelist** — only tools in those groups are available. Tools in `group:fs` (read/write/edit) and `group:runtime` (exec/process) are excluded.

### Tool Groups Reference

| Group | Tools |
|-------|-------|
| `group:fs` | read, write, edit, apply_patch |
| `group:runtime` | exec, process, code_execution |
| `group:web` | web_search, web_fetch, x_search |
| `group:memory` | memory_search, memory_get |
| `group:sessions` | sessions_list, sessions_history, sessions_send, sessions_spawn, sessions_yield, subagents, session_status |
| `group:automation` | cron, gateway |

### Fix

In `~/.openclaw/openclaw.json`, update the agent's tool allowlist to include all needed groups:

```json
{
  "agents": {
    "list": [{
      "id": "main",
      "tools": {
        "profile": "coding",
        "allow": [
          "group:fs",
          "group:runtime",
          "group:web",
          "group:memory",
          "group:sessions",
          "group:automation"
        ],
        "exec": {
          "security": "full",
          "ask": "off",
          "host": "gateway",
          "timeoutSec": 300
        }
      }
    }]
  }
}
```

**Important**: Do NOT use `"profile": "full"`. Despite the name, the `full` profile has an empty allow/deny list (`{}`), which means it provides **no profile-level allowlist**. The `coding` profile is what includes exec, read, write, and edit.

### Verification

```bash
# Restart gateway
openclaw gateway restart

# Test exec is available
openclaw agent --agent main -m "use exec to run: echo hello" --json \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print([t['name'] for t in d['result']['meta']['systemPromptReport']['tools']['entries']])"

# Should show: ['read', 'edit', 'write', 'exec', 'process', 'cron', ...]
```

## Problem: Discord Agent Refuses to Exec

Even after fixing the config, the agent may still refuse to use exec in Discord. This is caused by cached session context.

### Additional Fixes

1. **AGENTS.md**: Remove any line that says "Discord sessions 没有 exec 权限" or similar. Replace with:

   ```markdown
   ### Exec in Discord
   Discord sessions have full exec permissions. You can run any shell command directly.
   ```

2. **exec-approvals.json** (`~/.openclaw/exec-approvals.json`): Set security to full:

   ```json
   {
     "defaults": {
       "security": "full",
       "ask": "off",
       "autoAllowSkills": true
     },
     "agents": {
       "main": {
         "security": "full",
         "ask": "off",
         "autoAllowSkills": true
       }
     }
   }
   ```

3. **Clear cached Discord sessions**:

   ```bash
   # Remove Discord DM sessions that cache old instructions
   python3 -c "
   import json
   path = '$HOME/.openclaw/agents/main/sessions/sessions.json'
   with open(path) as f: data = json.load(f)
   for k in [k for k in data if 'discord:direct' in k]: del data[k]
   with open(path, 'w') as f: json.dump(data, f, indent=2)
   "

   # Restart
   openclaw gateway restart
   ```

## SQLite → PostgreSQL Migration

Agentic Memory fully replaces OpenClaw's built-in SQLite memory with PostgreSQL + pgvector. After installing:

- `plugins.slots.memory = "memory-api"` routes all memory operations to PostgreSQL
- The built-in `memory-core` (SQLite + sqlite-vec + FTS5) is completely bypassed
- No SQLite packages need to be installed on the system
- Old SQLite backups at `~/.openclaw/memory/*.sqlite.bak` can be safely ignored

### What Was SQLite, What Is PostgreSQL Now

| Component | Before (SQLite) | After (PostgreSQL) |
|-----------|----------------|-------------------|
| Vector storage | sqlite-vec extension | pgvector HNSW index |
| Full-text search | SQLite FTS5 | PostgreSQL tsvector + GIN |
| Embedding cache | In-process SQLite | Redis |
| File attachments | Local filesystem | MinIO (S3-compatible) |
| Concurrency | Single-writer lock | Full MVCC |

### Non-Memory Storage (Not Affected)

These OpenClaw storage layers do NOT use SQLite and require no migration:

- **Session store**: JSON files (`~/.openclaw/agents/*/sessions/`)
- **Cron store**: JSON file (`~/.openclaw/cron/jobs.json`)
- **Auth profiles**: JSON file (`~/.openclaw/agents/*/agent/auth-profiles.json`)
- **Exec approvals**: JSON file (`~/.openclaw/exec-approvals.json`)
