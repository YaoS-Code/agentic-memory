"""Auto memory extraction — extract key information from conversations.

Inspired by Claude Code's SessionMemory system:
- Dual-threshold trigger (token count + message count since last extraction)
- Post-reply async execution (non-blocking)
- Uses Claude Haiku for cost-efficient extraction
- Auto-classifies into appropriate storage tiers (fact/vector)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx

from config import settings

logger = logging.getLogger(__name__)

ANTHROPIC_API_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
EXTRACT_MODEL = os.environ.get("EXTRACT_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

# ── Thresholds ────────────────────────────────────────────

DEFAULT_TOKEN_THRESHOLD = 3000  # tokens since last extraction
DEFAULT_MSG_THRESHOLD = 10  # messages since last extraction
CHARS_PER_TOKEN = 3


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def estimate_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
        total += estimate_tokens(str(content))
    return total


def should_extract(
    messages: list[dict],
    last_extract_index: int = 0,
    token_threshold: int = DEFAULT_TOKEN_THRESHOLD,
    msg_threshold: int = DEFAULT_MSG_THRESHOLD,
) -> bool:
    """Check if new messages since last extraction meet extraction thresholds."""
    new_messages = messages[last_extract_index:]
    if not new_messages:
        return False
    if len(new_messages) >= msg_threshold:
        return True
    if estimate_messages_tokens(new_messages) >= token_threshold:
        return True
    return False


# ── Extraction Prompt ─────────────────────────────────────

EXTRACT_SYSTEM_PROMPT = """\
You are a memory extraction assistant. Your job is to scan a conversation \
and extract information worth remembering for future conversations.

Extract ONLY items that fit these categories:
1. **User preferences/habits** → output as tier "fact" with domain and key
2. **Project decisions/conclusions** → output as tier "vector", category "decision"
3. **Skills/knowledge learned** → output as tier "vector", category "skill"
4. **Contact/person info** → output as tier "fact" with domain "contact"
5. **Important dates/deadlines** → output as tier "fact" with domain "schedule"

IGNORE:
- Casual chat, greetings, filler
- Information already stated in earlier extractions
- Temporary instructions that won't apply to future conversations
- Tool outputs or raw data (only extract conclusions from them)

Output a JSON array of extraction objects. Each object has:
{
  "content": "concise description of what to remember",
  "tier": "fact" or "vector",
  "category": "decision" | "skill" | "insight" | "general" | "project",
  "importance": 1-10,
  "domain": "only for facts, e.g. preference, contact, schedule",
  "key": "only for facts, the specific key",
  "value": "only for facts, the structured value",
  "tags": ["relevant", "tags"]
}

If nothing worth extracting, output an empty array: []

IMPORTANT: Output ONLY the JSON array, no markdown fences, no explanation.
"""


def _format_messages(messages: list[dict]) -> str:
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
        if len(content) > 1500:
            content = content[:1300] + "\n...[truncated]..."
        lines.append(f"[{role}]: {content}")
    return "\n\n".join(lines)


async def extract_memories(
    messages: list[dict],
    last_extract_index: int = 0,
) -> list[dict]:
    """Extract memorable information from new messages.

    Returns list of StoreRequest-compatible dicts ready for /store.
    """
    new_messages = messages[last_extract_index:]
    if not new_messages:
        return []

    transcript = _format_messages(new_messages)
    user_prompt = (
        f"Extract memorable information from these {len(new_messages)} new messages "
        f"(messages {last_extract_index + 1} to {len(messages)} of the conversation).\n\n"
        f"--- MESSAGES ---\n{transcript}\n--- END ---"
    )

    if ANTHROPIC_API_KEY:
        raw = await _extract_via_api(user_prompt)
    else:
        raw = await _extract_via_cli(user_prompt)

    # Parse JSON response
    try:
        # Strip markdown fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        items = json.loads(raw)
        if not isinstance(items, list):
            items = [items]
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Failed to parse extraction response: %s\nRaw: %s", e, raw[:200])
        return []

    # Validate and normalize
    results = []
    for item in items:
        if not isinstance(item, dict) or not item.get("content"):
            continue
        results.append({
            "content": item["content"],
            "tier": item.get("tier", "vector"),
            "category": item.get("category", "general"),
            "importance": min(10, max(1, item.get("importance", 5))),
            "tags": item.get("tags", []),
            "source": "auto_extract",
            # Fact-specific fields
            "domain": item.get("domain"),
            "key": item.get("key"),
            "value": item.get("value"),
        })

    logger.info("Extracted %d memories from %d new messages", len(results), len(new_messages))
    return results


# ── Backend: Direct API ──────────────────────────────────

async def _extract_via_api(user_prompt: str) -> str:
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{ANTHROPIC_API_URL}/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": EXTRACT_MODEL,
                "max_tokens": 2000,
                "system": EXTRACT_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_prompt}],
            },
        )
        resp.raise_for_status()
        data = resp.json()

    parts = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            parts.append(block["text"])
    return "".join(parts)


# ── Backend: Claude CLI fallback ─────────────────────────

async def _extract_via_cli(user_prompt: str) -> str:
    full_prompt = f"{EXTRACT_SYSTEM_PROMPT}\n\n{user_prompt}"

    proc = await asyncio.create_subprocess_exec(
        CLAUDE_BIN,
        "--permission-mode", "bypassPermissions",
        "--print",
        "--model", "claude-haiku-4-5-20251001",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={
            **os.environ,
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
                    "/usr/local/bin:/usr/bin:/bin",
            "HOME": os.environ.get("HOME", os.path.expanduser("~")),
        },
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(full_prompt.encode()), timeout=120,
    )

    if proc.returncode != 0:
        err = stderr.decode()[-500:]
        raise RuntimeError(f"Claude CLI failed (exit {proc.returncode}): {err}")

    return stdout.decode().strip()
