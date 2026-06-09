"""Core unit tests for discord-grok-plugin.

State is isolated per-test via DISCORD_STATE_DIR (paths are resolved lazily, so
setting the env var in a fixture is enough — no real ~/.claude is touched).
"""

import asyncio

import pytest

from discord_grok_plugin import context as C
from discord_grok_plugin import main as M


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_STATE_DIR", str(tmp_path / "state"))
    for k in (
        "XAI_API_KEY", "OPENAI_API_KEY", "DISCORD_CHANNEL_ID", "DISCORD_CHANNEL_IDS",
        "DISCORD_ALLOW_ALL_CHANNELS", "DISCORD_OWNER_IDS", "DISCORD_OWNER_ID", "DISCORD_TOKEN",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


def test_stores_both_turns():
    C.update_context("c", new_user_message="u-msg", ai_reply="a-msg", last_id="100")
    snip = C.build_context_prompt_snippet("c")
    assert "u-msg" in snip and "a-msg" in snip
    assert C.get_last_processed_id("c") == "100"


def test_reserve_and_reset():
    C.set_last_processed_id("c", "250")
    assert C.get_last_processed_id("c") == "250"
    C.reset_context("c")
    assert C.get_last_processed_id("c") is None
    assert C.load_context("c")["recent_exchanges"] == []


def test_recent_exchanges_trimmed():
    for i in range(20):
        C.update_context("c", new_user_message=f"u{i}", ai_reply=f"a{i}", last_id=str(i))
    assert len(C.load_context("c")["recent_exchanges"]) <= C.DEFAULT_LIMIT_RECENT


def test_chunking():
    long = "x" * 4500
    chunks = M._split_message(long)
    assert all(len(c) <= 2000 for c in chunks)
    assert sum(len(c) for c in chunks) == len(long)
    assert M._split_message("short") == ["short"]


def test_channel_default_deny(monkeypatch):
    assert M._is_allowed("123", M._get_allowed_channels()) is False
    monkeypatch.setenv("DISCORD_ALLOW_ALL_CHANNELS", "true")
    assert M._is_allowed("123", M._get_allowed_channels()) is True


def test_channel_explicit_list(monkeypatch):
    monkeypatch.setenv("DISCORD_CHANNEL_IDS", "111,222")
    allowed = M._get_allowed_channels()
    assert M._is_allowed("222", allowed) is True
    assert M._is_allowed("999", allowed) is False


def test_owner_gate(monkeypatch):
    assert M._is_owner("42") is False
    monkeypatch.setenv("DISCORD_OWNER_IDS", "42, 7")
    assert M._is_owner("7") is True
    assert M._is_owner("8") is False


def test_llm_fallback_no_key():
    out = M.get_ai_response("snippet", "hello", "c")
    assert "No XAI_API_KEY" in out


def test_to_thread_nonblocking():
    res = asyncio.run(asyncio.to_thread(M.get_ai_response, "", "ping", "c", "<@42>"))
    assert isinstance(res, str) and res
