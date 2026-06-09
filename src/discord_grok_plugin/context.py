#!/usr/bin/env python3
"""Lightweight short-term conversation context for the Discord auto-responder.

Design:
- No long-term / DB / vector store. Per-channel rolling JSON only
  ("Grok build session"-style continuity within an active task).
- Path resolution is LAZY (resolved on each call from ``DISCORD_STATE_DIR``)
  so the state directory can be overridden at runtime — e.g. in tests — even
  after this module has been imported. (Previously the path was bound at import
  time, which silently broke test isolation.)

Public API (kept stable for extensions):
    load_context, update_context, build_context_prompt_snippet,
    reset_context, get_last_processed_id, set_last_processed_id,
    snippet_from_ctx, save_context
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

try:  # optional: enables cross-process safety when DISCORD_STATE_DIR is shared
    from filelock import FileLock
except Exception:  # pragma: no cover - filelock is an install-time optional
    FileLock = None  # type: ignore

DEFAULT_LIMIT_RECENT = 8
DEFAULT_MAX_FACTS = 12
# Cap each stored message so a single huge paste cannot blow up the JSON file.
MAX_CONTENT_CHARS = 2000
# How long to wait for the per-channel lock before proceeding best-effort.
LOCK_TIMEOUT_SECONDS = 10


def _state_dir() -> Path:
    return Path(
        os.environ.get(
            "DISCORD_STATE_DIR", Path.home() / ".claude" / "channels" / "discord"
        )
    )


def _context_dir() -> Path:
    d = _state_dir() / "context"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _context_path(channel_id: str) -> Path:
    return _context_dir() / f"{channel_id}.json"


@contextmanager
def _locked(channel_id: str) -> Iterator[None]:
    """Hold a per-channel inter-process lock around a read-modify-write.

    Needed because the README encourages sharing ``DISCORD_STATE_DIR`` between
    this plugin and other processes (e.g. a scheduler). Degrades to a no-op if
    ``filelock`` is not installed, and proceeds best-effort on lock timeout
    rather than hanging the event loop's worker thread.
    """
    if FileLock is None:
        yield
        return
    lock = FileLock(str(_context_path(channel_id)) + ".lock", timeout=LOCK_TIMEOUT_SECONDS)
    try:
        lock.acquire()
    except Exception:
        yield
        return
    try:
        yield
    finally:
        try:
            lock.release()
        except Exception:
            pass


def _default_ctx(channel_id: str) -> Dict[str, Any]:
    return {
        "channel_id": channel_id,
        "last_processed_id": None,
        "updated_at": None,
        "current_focus": "",
        "key_facts": [],
        "recent_exchanges": [],
    }


def load_context(channel_id: str) -> Dict[str, Any]:
    """Load short-term context for a channel. Returns a safe default if missing/corrupt."""
    path = _context_path(channel_id)
    if not path.exists():
        return _default_ctx(channel_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return _default_ctx(channel_id)
    # Backfill any missing keys (forward/backward compatibility).
    data.setdefault("channel_id", channel_id)
    data.setdefault("last_processed_id", None)
    data.setdefault("updated_at", None)
    data.setdefault("current_focus", "")
    data.setdefault("key_facts", [])
    data.setdefault("recent_exchanges", [])
    return data


def save_context(channel_id: str, ctx: Dict[str, Any]) -> None:
    """Atomically write the context file (tmp + os.replace)."""
    path = _context_path(channel_id)
    tmp = path.with_name(path.name + ".tmp")
    ctx = dict(ctx)
    ctx["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ctx, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def set_last_processed_id(channel_id: str, last_id: str) -> Dict[str, Any]:
    """Persist ``last_processed_id`` on its own.

    Used to RESERVE a message id as handled *before* the (slow) LLM call, which
    closes the duplicate-processing window on reconnects / re-deliveries.
    """
    with _locked(channel_id):
        ctx = load_context(channel_id)
        ctx["last_processed_id"] = last_id
        save_context(channel_id, ctx)
    return ctx


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
    """Record one exchange into the rolling context.

    The user message and the AI reply are stored as **separate** entries so the
    model actually sees what the user said (the previous implementation collapsed
    both into one dict, which dropped the user turn entirely).
    """
    with _locked(channel_id):
        return _update_context_locked(
            channel_id,
            new_user_message=new_user_message,
            ai_reply=ai_reply,
            focus_update=focus_update,
            facts_add=facts_add,
            last_id=last_id,
            trim_recent=trim_recent,
        )


def _update_context_locked(
    channel_id: str,
    *,
    new_user_message: Optional[str],
    ai_reply: Optional[str],
    focus_update: Optional[str],
    facts_add: Optional[List[str]],
    last_id: Optional[str],
    trim_recent: int,
) -> Dict[str, Any]:
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

    for role, content in (("user", new_user_message), ("assistant", ai_reply)):
        if content:
            entry: Dict[str, Any] = {"role": role, "content": content[:MAX_CONTENT_CHARS]}
            if last_id:
                entry["id"] = last_id
            ctx["recent_exchanges"].append(entry)

    if trim_recent and len(ctx["recent_exchanges"]) > trim_recent:
        ctx["recent_exchanges"] = ctx["recent_exchanges"][-trim_recent:]

    save_context(channel_id, ctx)
    return ctx


def get_last_processed_id(channel_id: str) -> Optional[str]:
    """Accessor used by the handler's snowflake anti-dup guard."""
    return load_context(channel_id).get("last_processed_id")


def reset_context(channel_id: str) -> None:
    """Clear short-term memory for the channel (resets the file to defaults)."""
    with _locked(channel_id):
        save_context(channel_id, _default_ctx(channel_id))


def snippet_from_ctx(ctx: Dict[str, Any], max_recent: int = 6) -> str:
    """Build a compact prompt snippet from an already-loaded context dict.

    Prefer this when you have already called :func:`load_context` to avoid
    re-reading the file.
    """
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

    channel_id = ctx.get("channel_id", "?")
    header = f"[Short-term context for channel {channel_id} — Grok-build-session style continuity]"
    return header + "\n" + "\n\n".join(parts)


def build_context_prompt_snippet(channel_id: str, max_recent: int = 6) -> str:
    """Convenience wrapper that loads the context then builds the snippet."""
    return snippet_from_ctx(load_context(channel_id), max_recent=max_recent)
