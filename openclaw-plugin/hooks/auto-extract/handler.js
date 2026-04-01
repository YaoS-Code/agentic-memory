/**
 * Auto-extract hook — extracts memorable information from conversations
 * before session reset/compaction and stores in Memory Service.
 *
 * Events: command:new, command:reset, session:compact:before
 */

import fs from "node:fs/promises";
import path from "node:path";

const MEMORY_SERVICE_URL = "http://localhost:18800";

/**
 * Read recent messages from session transcript file.
 */
async function getSessionMessages(sessionFilePath, maxMessages = 30) {
  try {
    const content = await fs.readFile(sessionFilePath, "utf-8");
    const lines = content.trim().split("\n");
    const messages = [];

    for (const line of lines) {
      try {
        const entry = JSON.parse(line);
        if (entry.type === "message" && entry.message) {
          const msg = entry.message;
          const role = msg.role;
          if ((role === "user" || role === "assistant") && msg.content) {
            const text = Array.isArray(msg.content)
              ? msg.content.find((c) => c.type === "text")?.text
              : msg.content;
            if (text && !text.startsWith("/")) {
              messages.push({ role, content: text });
            }
          }
        }
      } catch {
        // Skip unparseable lines
      }
    }

    return messages.slice(-maxMessages);
  } catch {
    return [];
  }
}

/**
 * Find the session transcript file path.
 */
function resolveSessionPath(stateDir, sessionKey) {
  // OpenClaw stores transcripts in <stateDir>/sessions/<sessionKey>.jsonl
  const safeName = sessionKey.replace(/[:/]/g, "_");
  return path.join(stateDir, "sessions", `${safeName}.jsonl`);
}

/**
 * Call the Memory Service /extract endpoint.
 */
async function callExtract(messages) {
  try {
    const resp = await fetch(`${MEMORY_SERVICE_URL}/extract`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages,
        auto_store: true,
        force: true,
      }),
    });

    if (!resp.ok) {
      console.error(`[auto-extract] Extract API error: ${resp.status}`);
      return null;
    }

    return await resp.json();
  } catch (err) {
    console.error(`[auto-extract] Extract failed: ${err.message}`);
    return null;
  }
}

/**
 * Hook handler — called on command:new, command:reset, session:compact:before
 */
export default async function handler(event) {
  const sessionKey = event.sessionKey;
  if (!sessionKey) return;

  // Resolve state directory
  const homeDir = process.env.HOME || os.homedir();
  const stateDir = path.join(homeDir, ".openclaw", "state");

  const sessionPath = resolveSessionPath(stateDir, sessionKey);
  const messages = await getSessionMessages(sessionPath);

  if (messages.length < 5) {
    // Too few messages to extract anything useful
    return;
  }

  const result = await callExtract(messages);

  if (result && result.stored_count > 0) {
    console.log(
      `[auto-extract] Extracted ${result.stored_count} memories from ${messages.length} messages (session: ${sessionKey})`
    );
  }
}
