#!/usr/bin/env python3
"""
Lightweight short-term conversation context for Discord auto-responder.
 (Adapted from original for the plugin package)

Purpose:
- No permanent/long-term memory (user explicitly does not want DB/vector store).
- Provide "Grok build session"-like continuity within an active task.
- Prevent having to re-explain the entire context on every new Discord message.

Usage:
    from discord_grok_plugin.context import load_context, update_context, build_context_prompt_snippet

    ctx = load_context(channel_id)
    snippet = build_context_prompt_snippet(channel_id)

    # ... get AI response ...

    update_context(
        channel_id,
        new_user_message=message,
        ai_reply=reply,
        last_id=message_id,
        focus_update=...
    )
"""

from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

STATE_DIR = Path(os.environ.get("DISCORD_STATE_DIR", Path.home() / ".claude" / "channels" / "discord"))
CONTEXT_DIR = STATE_DIR / "context"
CONTEXT_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_LIMIT_RECENT = 8
DEFAULT_MAX_FACTS = 12


def _context_path(channel_id: str) -> Path:
    return CONTEXT_DIR / f"{channel_id}.json"


def load_context(channel_id: str) -> Dict[str, Any]:
    """Load existing short-term context for the channel. Returns a safe default if missing."""
    path = _context_path(channel_id)
    if not path.exists():
        return {
            "channel_id": channel_id,
            "last_processed_id": None,
            "updated_at": None,
            "current_focus": "",
            "key_facts": [],
            "recent_exchanges": [],
        }
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("key_facts", [])
        data.setdefault("recent_exchanges", [])
        data.setdefault("current_focus", "")
        return data
    except Exception:
        return {
            "channel_id": channel_id,
            "last_processed_id": None,
            "updated_at": None,
            "current_focus": "",
            "key_facts": [],
            "recent_exchanges": [],
        }


def save_context(channel_id: str, ctx: Dict[str, Any]) -> None:
    """Atomically write the context file."""
    path = _context_path(channel_id)
    tmp = path.with_suffix(".json.tmp")
    ctx = dict(ctx)
    ctx["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ctx, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def update_context(
    channel_id: str,
    *,
    new_user_message: Optional[str] = None,
    ai_reply: Optional[str] = None,
    focus_update: Optional[str] = None,
    facts_add: Optional[List[str]] = None,
    last_id: Optional[str] = None,
    trim_recent: int = DEFAULT_LIMIT_RECENT,
) -> Dict[str, Any]:
    """
    Update the rolling context after processing one exchange.
    SACRED: call this (with last_id) IMMEDIATELY after generating the reply text,
    BEFORE sending it on Discord. This guarantees the snowflake guard prevents
    duplicate handling on reconnects, re-deliveries, or races.
    """
    ctx = load_context(channel_id)

    if last_id:
        ctx["last_processed_id"] = last_id

    if focus_update:
        ctx["current_focus"] = focus_update

    if facts_add:
        for fact in facts_add:
            if fact and fact not in ctx["key_facts"]:
                ctx["key_facts"].append(fact)
        if len(ctx["key_facts"]) > DEFAULT_MAX_FACTS:
            ctx["key_facts"] = ctx["key_facts"][-DEFAULT_MAX_FACTS:]

    if new_user_message or ai_reply:
        exchange: Dict[str, Any] = {}
        if new_user_message:
            exchange["role"] = "user"
            exchange["content"] = new_user_message
        if ai_reply:
            exchange["role"] = "assistant"
            exchange["content"] = ai_reply
        if last_id:
            exchange["id"] = last_id
        ctx.setdefault("recent_exchanges", []).append(exchange)
        ctx["recent_exchanges"] = ctx["recent_exchanges"][-trim_recent:]

    save_context(channel_id, ctx)
    return ctx


def get_last_processed_id(channel_id: str) -> Optional[str]:
    """Convenience accessor used by main handler for snowflake anti-dup guard."""
    return load_context(channel_id).get("last_processed_id")


def reset_context(channel_id: str) -> None:
    """Clear short-term memory for the channel (keeps the file but resets to defaults)."""
    fresh: Dict[str, Any] = {
        "channel_id": channel_id,
        "last_processed_id": None,
        "updated_at": None,
        "current_focus": "",
        "key_facts": [],
        "recent_exchanges": [],
    }
    save_context(channel_id, fresh)


def build_context_prompt_snippet(channel_id: str, max_recent: int = 6) -> str:
    """
    Returns a compact text block you can inject into the AI prompt.
    """
    ctx = load_context(channel_id)
    parts: List[str] = []

    if ctx.get("current_focus"):
        parts.append(f"Current focus: {ctx['current_focus']}")

    if ctx.get("key_facts"):
        facts = "\n".join(f"- {f}" for f in ctx["key_facts"])
        parts.append(f"Key facts / decisions so far:\n{facts}")

    recent = ctx.get("recent_exchanges", [])[-max_recent:]
    if recent:
        lines = []
        for ex in recent:
            role = ex.get("role", "?")
            content = (ex.get("content") or "").replace("\n", " ")[:300]
            lines.append(f"  {role}: {content}")
        parts.append("Recent exchanges:\n" + "\n".join(lines))

    if not parts:
        return ""

    header = f"[Short-term context for channel {channel_id} — Grok-build-session style continuity]"
    return header + "\n" + "\n\n".join(parts)
