"""Conversation compaction — summarize long conversations to save tokens.

Inspired by Claude Code's auto-compact strategy:
- Dual-threshold trigger (message count + token estimate)
- Structured summary preserving key decisions, names, timeline
- Uses Claude Haiku for cost efficiency
- Two backends: direct API (if ANTHROPIC_API_KEY set) or Claude CLI fallback
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
COMPACT_MODEL = os.environ.get("COMPACT_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

# ── Thresholds ────────────────────────────────────────────

DEFAULT_MSG_THRESHOLD = 20  # messages
DEFAULT_TOKEN_THRESHOLD = 6000  # estimated tokens
CHARS_PER_TOKEN = 3  # rough estimate for mixed EN/CN


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~3 chars per token for mixed EN/CN)."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate total tokens across all messages."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            # Handle structured content blocks
            content = " ".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
        total += estimate_tokens(str(content))
    return total


def should_compact(
    messages: list[dict],
    msg_threshold: int = DEFAULT_MSG_THRESHOLD,
    token_threshold: int = DEFAULT_TOKEN_THRESHOLD,
) -> bool:
    """Check if conversation needs compaction (dual-threshold)."""
    if len(messages) >= msg_threshold:
        return True
    if estimate_messages_tokens(messages) >= token_threshold:
        return True
    return False


# ── Compact Prompt ────────────────────────────────────────

COMPACT_SYSTEM_PROMPT = """\
You are a conversation summarizer. Your job is to compress a conversation \
into a structured summary that preserves all important information while \
drastically reducing token count.

Rules:
1. Preserve: key decisions, conclusions, action items, code references, \
   file paths, person names, dates/deadlines, technical details
2. Drop: greetings, filler, repeated explanations, verbose tool outputs
3. Use bullet points, not prose
4. Keep the original language (Chinese or English) as used in the conversation
5. If code was discussed, note the file paths and what changed — not the full code
6. Output in this format:

## Context
- What this conversation is about (1-2 lines)

## Key Decisions
- Decision 1
- Decision 2

## Action Items
- [ ] Item 1
- [ ] Item 2

## Important Details
- Technical detail 1
- Technical detail 2

## Current State
- Where the conversation left off
"""


def _format_messages_for_prompt(messages: list[dict]) -> str:
    """Format messages into a readable conversation transcript."""
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
        # Truncate very long individual messages
        if len(content) > 2000:
            content = content[:1800] + "\n...[truncated]..."
        lines.append(f"[{role}]: {content}")
    return "\n\n".join(lines)


async def compact_messages(
    messages: list[dict],
    max_summary_tokens: int = 2000,
) -> dict:
    """Compact a conversation into a structured summary using Claude Haiku.

    Returns:
        {
            "summary": str,           # The compressed summary
            "token_estimate": int,     # Estimated tokens of summary
            "original_tokens": int,    # Estimated tokens of original
            "original_count": int,     # Number of messages compacted
            "compression_ratio": float # original / summary tokens
        }
    """
    transcript = _format_messages_for_prompt(messages)
    original_tokens = estimate_messages_tokens(messages)

    user_prompt = (
        f"Summarize the following conversation ({len(messages)} messages, "
        f"~{original_tokens} tokens) into a compact structured summary.\n\n"
        f"--- CONVERSATION START ---\n{transcript}\n--- CONVERSATION END ---"
    )

    if ANTHROPIC_API_KEY:
        summary = await _compact_via_api(user_prompt, max_summary_tokens)
    else:
        summary = await _compact_via_cli(user_prompt)

    summary_tokens = estimate_tokens(summary)
    compression_ratio = round(original_tokens / max(summary_tokens, 1), 1)

    logger.info(
        "Compacted %d messages: %d → %d tokens (%.1fx compression)",
        len(messages), original_tokens, summary_tokens, compression_ratio,
    )

    return {
        "summary": summary,
        "token_estimate": summary_tokens,
        "original_tokens": original_tokens,
        "original_count": len(messages),
        "compression_ratio": compression_ratio,
    }


# ── Backend: Direct API ──────────────────────────────────

async def _compact_via_api(user_prompt: str, max_tokens: int) -> str:
    """Call Anthropic Messages API directly."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{ANTHROPIC_API_URL}/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": COMPACT_MODEL,
                "max_tokens": max_tokens,
                "system": COMPACT_SYSTEM_PROMPT,
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

async def _compact_via_cli(user_prompt: str) -> str:
    """Call Claude CLI (--print mode) as fallback when no API key."""
    full_prompt = f"{COMPACT_SYSTEM_PROMPT}\n\n{user_prompt}"

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
