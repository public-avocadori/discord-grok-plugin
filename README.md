# discord-grok-plugin

High-performance push-based Discord auto-responder with **short-term rolling memory** (Grok build session style continuity). No long-term DB or vector store.

Event-driven (discord.py `on_message`), instant responses, strict duplicate prevention, pip-installable, and ready to distribute.

## Why this exists
Traditional polling loops have timing/race issues and latency.  
This plugin is the "complete form" from day one:

- **Push** (real-time on Discord events)
- **Non-blocking** — the LLM call runs in a worker thread (`asyncio.to_thread`), so it never freezes the gateway/heartbeat
- **Grok-build continuity** via per-channel short-term JSON context (`last_processed_id`, `current_focus`, `recent_exchanges`, `key_facts`; `key_facts` is optionally auto-populated via `LLM_AUTO_FACTS`, otherwise an extension hook)
- **Robust anti-dup**: a per-channel `asyncio.Lock` plus an in-memory in-flight guard, and the message id is **reserved before** the (slow) LLM call — reconnects/re-deliveries/races cannot double-process
- Works great as a standalone bot **or** alongside Grok build schedulers (shared state dir)
- General purpose — skills (like いらすとら) are supported via the LLM prompt / your own extensions

## Features
- Event-driven, low latency (no 1-min polling) and **non-blocking** LLM calls
- Short-term memory that survives restarts (per-channel `.json`), storing **both** user and assistant turns
- Atomic writes (`.tmp` + `os.replace`) + **cross-process file lock** (safe to share the state dir)
- Snowflake + lock + reserve-before-LLM dedup (no double replies on reconnect/races)
- Real LLM replies (xAI Grok or any OpenAI-compatible) with context injected, **retried with backoff** on transient errors
- **Typing indicator** while thinking; replies **threaded** to the triggering message
- Real `@mention` pings (the model is given the speaker's `<@id>` token)
- Long replies auto-split into 2000-char Discord chunks
- Optional **per-channel cooldown** and optional **auto key-fact extraction** (periodic summary)
- Graceful fallback when no API key (still shows memory is working)
- **Owner-gated** admin commands: `!memory` / `!ctx` and `!forget`
- **Default-deny** channel scoping (explicit allow-list or opt-in all-channels)
- `pip install` + `discord-grok-plugin` entrypoint **or** `python -m discord_grok_plugin`
- Tests (pytest) + CI (ruff, mypy); Docker + systemd deploy recipes
- Cross platform (Windows + Unix), same default state path as Grok build

## Installation

```bash
pip install discord-grok-plugin
```

Or from source (for development):

```bash
git clone https://github.com/public-avocadori/discord-grok-plugin.git
cd discord-grok-plugin
pip install -e .
```

## Quick Start

1. Create a Discord bot at https://discord.com/developers/applications
2. Enable **Message Content Intent**
3. Invite the bot to your server with `Send Messages`, `Read Message History` (and `Read Messages` if using slash-style)
4. Copy the token

5. Create a `.env` (copy `.env.example`):

```env
DISCORD_TOKEN=your_token_here
XAI_API_KEY=your_xai_key     # or OPENAI_API_KEY
DISCORD_CHANNEL_ID=123456789012345678        # required (default-deny) unless ALLOW_ALL
# DISCORD_CHANNEL_IDS=123,456                 # or multiple
# DISCORD_ALLOW_ALL_CHANNELS=true             # answer everywhere instead
DISCORD_OWNER_IDS=123456789012345678         # who may run !memory / !forget
```

6. Run:

```bash
discord-grok-plugin
# or
python -m discord_grok_plugin
```

The bot now answers with full short-term continuity. Say something, restart the process, say more — it remembers the thread without you re-explaining.

## Environment Variables

| Variable                | Required | Description |
|-------------------------|----------|-------------|
| `DISCORD_TOKEN`         | yes      | Your bot token |
| `XAI_API_KEY`           | rec.     | For Grok models (takes precedence) |
| `OPENAI_API_KEY`        | rec.     | Fallback / generic OpenAI-compatible |
| `DISCORD_CHANNEL_ID`    | no\*     | Single channel to listen in |
| `DISCORD_CHANNEL_IDS`   | no\*     | Comma-separated list |
| `DISCORD_ALLOW_ALL_CHANNELS` | no  | `true` to answer in every readable channel |
| `DISCORD_OWNER_IDS`     | no       | Comma-separated user ids allowed to run `!memory` / `!forget` |
| `LLM_MODEL`             | no       | e.g. `grok-3-mini`, `gpt-4o-mini` |
| `LLM_MAX_TOKENS`        | no       | Default 900 |
| `LLM_MAX_RETRIES`       | no       | Retries on transient (429/5xx/timeout) errors. Default 2 |
| `LLM_COOLDOWN_SECONDS`  | no       | Min seconds between replies per channel. Default 0 (off) |
| `LLM_AUTO_FACTS`        | no       | `true` to periodically summarise context into `key_facts` |
| `LLM_FACTS_EVERY`       | no       | Run the summary every N replies (with AUTO_FACTS). Default 6 |
| `DISCORD_STATE_DIR`     | no       | Override memory dir (defaults to `~/.claude/channels/discord`) |

\* **Default-deny:** with no channel allow-list **and** `DISCORD_ALLOW_ALL_CHANNELS` off, the bot stays silent everywhere. Set at least one channel, or explicitly opt into all-channels (careful on big servers).

## How the Memory Works (Important)

- Every channel gets its own `context/<channel_id>.json`
- On every incoming message (under a per-channel lock):
  1. Check `last_processed_id` (snowflake) + in-flight set → skip if already handled
  2. Load rolling context **once** + build compact prompt snippet
  3. **Reserve** the id: `set_last_processed_id(channel_id, message.id)` — *before* the LLM call
  4. Call the LLM in a worker thread (`asyncio.to_thread`, non-blocking)
  5. `update_context(...)` to persist the user + assistant turns
  6. Send the reply (auto-chunked to 2000 chars)

Reserving the id in step 3 (not after the reply) closes the duplicate window during the slow LLM call; the lock + in-flight set handle true concurrency.

`recent_exchanges` is trimmed (default 8) and each message capped (`MAX_CONTENT_CHARS`). No permanent memory — exactly as requested.

Owners can inspect with `!memory` / `!ctx` and wipe with `!forget` (set `DISCORD_OWNER_IDS`).

## Using with Grok Build / Scheduler (continuity)

Point `DISCORD_STATE_DIR` (or leave default) so that both this plugin and any scheduler-based Grok sessions share the same JSON files.  
The context helper is deliberately the same shape as the original `discord_context.py` used in prompts.

## Extensibility & Skills

- The injected `[Short-term context ...]` block + system prompt makes any LLM "remember" ongoing work.
- For actual tool/skills execution (e.g. calling いらすとら or other Grok skills):
  - Option A: Let the LLM return a structured command in its text; parse it in a subclass or wrapper.
  - Option B: Extend `on_message` or provide your own responder that inspects `ai_response` and dispatches.
  - Option C: Use the context helpers directly from your own code:

```python
from discord_grok_plugin.context import (
    load_context, update_context, build_context_prompt_snippet, reset_context
)

ctx = load_context(channel_id)
snippet = build_context_prompt_snippet(channel_id)
# ... call your AI with the snippet ...
update_context(channel_id, new_user_message=..., ai_reply=..., last_id=...)
```

The plugin is intentionally **not** hard-coded to any single skill.

## Development

```bash
pip install -e .
discord-grok-plugin
```

Quick smoke test (paths are resolved lazily, so setting `DISCORD_STATE_DIR`
**before importing** truly isolates the test from your real `~/.claude`):

```bash
pip install -e .
python -c "
import tempfile, os, shutil
from pathlib import Path

tmp = tempfile.mkdtemp(prefix='dgp-test-')
os.environ['DISCORD_STATE_DIR'] = str(Path(tmp)/'state')   # set BEFORE import

from discord_grok_plugin.context import update_context, build_context_prompt_snippet, get_last_processed_id, reset_context

ch='testchan'
update_context(ch, new_user_message='plan the plugin', ai_reply='ok', last_id='111')
assert get_last_processed_id(ch) == '111'
snip = build_context_prompt_snippet(ch)
assert 'plan the plugin' in snip and 'ok' in snip   # both turns stored
reset_context(ch)
assert get_last_processed_id(ch) is None
shutil.rmtree(tmp, ignore_errors=True)
print('context roundtrip + both-turns + reset + isolation: OK')
"
# Then: discord-grok-plugin  (with DISCORD_TOKEN in env/.env) starts the push bot
```

Admin commands (`!memory` / `!ctx` / `!forget`) require the caller's id to be in
`DISCORD_OWNER_IDS`; otherwise they are politely refused.

Full checks (same as CI):

```bash
pip install -e ".[dev]"
pytest -q
ruff check .
mypy src/discord_grok_plugin
```

## Deployment

**Docker:**

```bash
docker build -t discord-grok-plugin .
docker run --env-file .env -v dgp-data:/data discord-grok-plugin
```

The image stores memory under the `/data` volume (`DISCORD_STATE_DIR=/data`).

**systemd** (Linux host): see [`deploy/discord-grok-plugin.service`](deploy/discord-grok-plugin.service).
Put your config in `/etc/discord-grok-plugin.env`, then:

```bash
sudo cp deploy/discord-grok-plugin.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now discord-grok-plugin
```

## License

MIT — use it, improve it, star it on GitHub.
