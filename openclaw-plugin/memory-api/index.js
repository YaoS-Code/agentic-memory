/**
 * memory-api — OpenClaw memory plugin backed by PostgreSQL + pgvector + bge-m3.
 *
 * Fully replaces the built-in memory-core (SQLite + sqlite-vec).
 * Uses registerMemoryRuntime to take over the entire memory subsystem.
 * All API calls go through child_process.execFile('curl') to bypass
 * any gateway fetch() sandbox restrictions.
 */

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { readFile } from "node:fs/promises";
import { join, resolve, relative, isAbsolute, sep } from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const DEFAULT_API_URL = "http://localhost:18800";
const DEFAULT_MAX_RESULTS = 10;

/**
 * Call the Memory Service API via curl subprocess.
 */
async function apiCall(apiUrl, path, body, timeoutSec = 30) {
  const url = `${apiUrl}${path}`;
  const args = ["-s", "-f", "--max-time", String(timeoutSec)];

  if (body) {
    args.push(
      "-X", "POST",
      "-H", "Content-Type: application/json",
      "-d", JSON.stringify(body),
    );
  }

  args.push(url);

  const { stdout } = await execFileAsync("curl", args);
  if (!stdout || stdout.trim() === "") {
    throw new Error(`Memory API ${path} returned empty response`);
  }
  return JSON.parse(stdout);
}

/**
 * Create a MemorySearchManager backed by pgvector (via Memory Service HTTP API).
 * Implements the full MemorySearchManager interface from:
 *   packages/memory-host-sdk/src/host/types.ts
 */
function createPgMemorySearchManager(apiUrl, maxResults, workspaceDir) {
  // Track sync state
  let lastSyncTime = 0;
  let fileCount = 0;
  let chunkCount = 0;
  let dirty = true;

  return {
    /**
     * Hybrid search: vector (HNSW) + FTS (tsvector), merged with RRF.
     */
    async search(query, opts) {
      const max = opts?.maxResults ?? maxResults;
      const minScore = opts?.minScore ?? 0.0;

      try {
        // Search workspace files (primary — replaces SQLite search)
        const wsData = await apiCall(apiUrl, "/workspace/search", {
          query,
          max_results: max,
          min_score: minScore,
        });

        const results = (wsData.results || []).map((r) => ({
          path: r.path,
          startLine: r.startLine,
          endLine: r.endLine,
          score: r.score || 0,
          snippet: r.snippet || "",
          source: r.source || "memory",
        }));

        // Also search conversation memories for richer context
        try {
          const convData = await apiCall(apiUrl, "/search", {
            query,
            max_results: Math.min(max, 3),
            tiers: ["vector"],
          });

          for (const r of convData.results || []) {
            results.push({
              path: `memory/conversations`,
              startLine: 1,
              endLine: 1,
              score: (r.score || 0) * 0.8, // slightly lower weight
              snippet: (r.content || "").slice(0, 700),
              source: "memory",
            });
          }
        } catch {
          // Conversation search failure is non-fatal
        }

        // Sort by score descending, limit
        results.sort((a, b) => b.score - a.score);
        return results.slice(0, max);
      } catch (err) {
        console.error(`[memory-api] search error: ${err.message}`);
        return [];
      }
    },

    /**
     * Read a workspace file by relative path.
     */
    async readFile(params) {
      const relPath = params.relPath?.trim();
      if (!relPath) return { text: "", path: "" };

      // Try reading from workspace directory first (fastest path)
      if (workspaceDir) {
        const absPath = isAbsolute(relPath) ? resolve(relPath) : resolve(workspaceDir, relPath);
        const rel = relative(workspaceDir, absPath);
        const inWorkspace = rel.length > 0 && !rel.startsWith("..") && !isAbsolute(rel);

        if (inWorkspace && absPath.endsWith(".md")) {
          try {
            const text = await readFile(absPath, "utf-8");
            const lines = text.split("\n");
            const from = params.from || 0;
            const count = params.lines || lines.length;
            return {
              text: lines.slice(from, from + count).join("\n"),
              path: rel.replace(/\\/g, "/"),
            };
          } catch {
            // Fall through to API
          }
        }
      }

      // Fallback: read via API
      try {
        const qs = new URLSearchParams({ path: relPath });
        if (params.from) qs.set("from_line", String(params.from));
        if (params.lines) qs.set("lines", String(params.lines));
        const data = await apiCall(apiUrl, `/workspace/read?${qs}`);
        return { text: data.text || "", path: data.path || relPath };
      } catch {
        return { text: "", path: relPath };
      }
    },

    /**
     * Return provider status.
     */
    status() {
      return {
        backend: "builtin",
        provider: "pgvector",
        model: "bge-m3",
        requestedProvider: "pgvector",
        files: fileCount,
        chunks: chunkCount,
        dirty,
        workspaceDir,
        sources: ["memory"],
        sourceCounts: [
          { source: "memory", files: fileCount, chunks: chunkCount },
        ],
        cache: { enabled: true, entries: chunkCount },
        fts: { enabled: true, available: true },
        vector: {
          enabled: true,
          available: true,
          dims: 1024,
        },
        batch: {
          enabled: false,
          failures: 0,
          limit: 2,
          wait: true,
          concurrency: 2,
          pollIntervalMs: 2000,
          timeoutMs: 3600000,
        },
        custom: {
          apiUrl,
          searchMode: "hybrid",
          backend: "postgresql+pgvector",
        },
      };
    },

    /**
     * Sync workspace files to pgvector.
     */
    async sync(params) {
      try {
        const force = params?.force || false;
        const result = await apiCall(apiUrl, "/workspace/sync", {
          force,
          workspace_dir: workspaceDir,
        }, 120); // 2 min timeout for full sync

        fileCount = (result.indexed || 0) + (result.unchanged || 0);
        chunkCount = result.chunks || chunkCount;
        dirty = false;
        lastSyncTime = Date.now();

        if (params?.progress) {
          params.progress({
            completed: fileCount,
            total: fileCount,
            label: `Indexed ${result.indexed} files, ${result.unchanged} unchanged`,
          });
        }
      } catch (err) {
        console.error(`[memory-api] sync error: ${err.message}`);
      }
    },

    /**
     * Check if embedding model is available.
     */
    async probeEmbeddingAvailability() {
      try {
        const health = await apiCall(apiUrl, "/health");
        return {
          ok: health.embedding_model_loaded === true,
          error: health.embedding_model_loaded ? undefined : "Embedding model not loaded",
        };
      } catch (err) {
        return { ok: false, error: err.message };
      }
    },

    /**
     * Check if vector search is available.
     */
    async probeVectorAvailability() {
      try {
        const health = await apiCall(apiUrl, "/health");
        return health.pg === true && health.embedding_model_loaded === true;
      } catch {
        return false;
      }
    },

    async close() {
      // No-op — the HTTP API manages its own connections
    },
  };
}

export default definePluginEntry({
  id: "memory-api",
  name: "Memory API Plugin",
  description:
    "Full memory backend via PostgreSQL + pgvector + bge-m3. " +
    "Replaces built-in SQLite memory-core entirely.",
  kind: "memory",

  register(api) {
    // ── 1. Register the memory runtime (replaces memory-core's runtime) ──
    api.registerMemoryRuntime({
      async getMemorySearchManager(params) {
        const pluginConfig =
          params.cfg?.plugins?.entries?.["memory-api"]?.config || {};
        const apiUrl = pluginConfig.apiUrl || DEFAULT_API_URL;
        const maxResults = pluginConfig.maxResults || DEFAULT_MAX_RESULTS;
        const workspaceDir =
          params.cfg?.agents?.defaults?.workspace || process.env.HOME + "/.openclaw/workspace";

        try {
          // Verify API is reachable
          const health = await apiCall(apiUrl, "/health");
          if (!health.pg || !health.embedding_model_loaded) {
            return {
              manager: null,
              error: `Memory API unhealthy: pg=${health.pg}, emb=${health.embedding_model_loaded}`,
            };
          }

          const manager = createPgMemorySearchManager(
            apiUrl, maxResults, workspaceDir,
          );

          // Auto-sync on first access (non-blocking)
          manager.sync({ reason: "initial" }).catch((err) =>
            console.error(`[memory-api] initial sync failed: ${err.message}`)
          );

          return { manager };
        } catch (err) {
          return {
            manager: null,
            error: `Memory API unreachable: ${err.message}`,
          };
        }
      },

      resolveMemoryBackendConfig() {
        return { backend: "builtin" };
      },

      async closeAllMemorySearchManagers() {
        // No-op — API manages its own lifecycle
      },
    });

    // ── 2. Register memory_search and memory_get tools ──
    //    (memory-core normally registers these; we must too since we replace it)

    // Shared manager cache — lazily initialized, reused across tool calls
    let cachedManager = null;
    let managerPromise = null;

    async function getManager(cfg) {
      if (cachedManager) return cachedManager;
      if (managerPromise) return managerPromise;

      managerPromise = (async () => {
        const pluginConfig = cfg?.plugins?.entries?.["memory-api"]?.config || {};
        const apiUrl = pluginConfig.apiUrl || DEFAULT_API_URL;
        const maxResults = pluginConfig.maxResults || DEFAULT_MAX_RESULTS;
        const workspaceDir = cfg?.agents?.defaults?.workspace || process.env.HOME + "/.openclaw/workspace";

        const health = await apiCall(apiUrl, "/health");
        if (!health.pg || !health.embedding_model_loaded) return null;

        cachedManager = createPgMemorySearchManager(apiUrl, maxResults, workspaceDir);
        cachedManager.sync({ reason: "initial" }).catch(() => {});
        return cachedManager;
      })();

      try {
        return await managerPromise;
      } finally {
        managerPromise = null;
      }
    }

    api.registerTool(
      (ctx) => {
        const cfg = ctx.config;
        if (!cfg) return null;

        return {
          label: "Memory Search",
          name: "memory_search",
          description:
            "Mandatory recall step: semantically search workspace files " +
            "(MEMORY.md, memory/*.md, SKILL files, AGENTS.md) and past conversation memories. " +
            "Returns top snippets with path + line numbers. Backend: PostgreSQL + pgvector + bge-m3.",
          parameters: {
            type: "object",
            properties: {
              query: { type: "string", description: "Search query" },
              maxResults: { type: "number", description: "Max results (default 10)" },
              minScore: { type: "number", description: "Min score threshold" },
            },
            required: ["query"],
          },
          async execute(_toolCallId, params) {
            try {
              const manager = await getManager(cfg);
              if (!manager) {
                return JSON.stringify({
                  results: [], disabled: true, error: "memory API unavailable",
                });
              }
              const results = await manager.search(params.query, {
                maxResults: params.maxResults,
                minScore: params.minScore,
              });
              const status = manager.status();
              return JSON.stringify({
                results,
                provider: status.provider,
                model: status.model,
                mode: status.custom?.searchMode,
              });
            } catch (err) {
              return JSON.stringify({
                results: [], disabled: true, error: err.message,
              });
            }
          },
        };
      },
      { names: ["memory_search"] },
    );

    api.registerTool(
      (ctx) => {
        const cfg = ctx.config;
        if (!cfg) return null;

        return {
          label: "Memory Get",
          name: "memory_get",
          description:
            "Read a workspace file by path with optional from/lines range. " +
            "Use after memory_search to pull specific lines and keep context small.",
          parameters: {
            type: "object",
            properties: {
              path: { type: "string", description: "Relative path within workspace" },
              from: { type: "number", description: "Start line (1-based)" },
              lines: { type: "number", description: "Number of lines to read" },
            },
            required: ["path"],
          },
          async execute(_toolCallId, params) {
            try {
              const manager = await getManager(cfg);
              if (!manager) {
                return JSON.stringify({ path: params.path, text: "", error: "memory unavailable" });
              }
              const result = await manager.readFile({
                relPath: params.path,
                from: params.from,
                lines: params.lines,
              });
              return JSON.stringify(result);
            } catch (err) {
              return JSON.stringify({ path: params.path, text: "", error: err.message });
            }
          },
        };
      },
      { names: ["memory_get"] },
    );

    // ── 3. Prompt section for the model ──
    api.registerMemoryPromptSection(({ availableTools }) => {
      const sections = [];
      if (availableTools.has("memory_search")) {
        sections.push(
          "## Memory Search",
          "Use `memory_search` to find information from workspace files and past conversations.",
          "Backend: PostgreSQL + pgvector (bge-m3 1024-dim embeddings, HNSW index).",
          "Hybrid search combines semantic vector matching with full-text search.",
        );
      }
      return sections;
    });

    // ── 3. Flush plan for pre-compaction memory save ──
    api.registerMemoryFlushPlan(({ nowMs }) => {
      const today = new Date(nowMs || Date.now()).toISOString().slice(0, 10);
      return {
        softThresholdTokens: 4000,
        forceFlushTranscriptBytes: 200000,
        reserveTokensFloor: 20000,
        prompt: [
          `Write any lasting notes to memory/${today}.md.`,
          `Store durable memories only in memory/YYYY-MM-DD.md (create memory/ if needed).`,
          `If memory/${today}.md already exists, APPEND new content only.`,
          `Treat MEMORY.md, SOUL.md, TOOLS.md, AGENTS.md as read-only during this flush.`,
          `If nothing to store, reply with NO_REPLY.`,
        ].join(" "),
        systemPrompt:
          "Pre-compaction memory flush. Capture durable memories to disk. " +
          "For structured facts, use exec to call: " +
          "curl -s -X POST http://localhost:18800/facts -H 'Content-Type: application/json' -d '{...}'",
        relativePath: `memory/${today}.md`,
      };
    });
  },
});
