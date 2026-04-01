---
name: auto-extract
description: "Auto-extract memorable information from conversations before session reset"
metadata:
  {
    "openclaw":
      {
        "emoji": "🧠",
        "events": ["command:new", "command:reset", "session:compact:before"],
        "requires": { "config": ["workspace.dir"] },
      },
  }
---

# Auto-Extract Hook

Automatically extracts key information (facts, decisions, preferences) from conversations
and stores them in the Memory Service before the session is reset or compacted.

Uses the `/extract` endpoint at `http://localhost:18800/extract` with `auto_store: true`.
