# discord-grok-plugin

High-performance push-based Discord auto-responder with **short-term rolling memory** (Grok build session style continuity). No long-term DB or vector store.

Event-driven (discord.py `on_message`), instant responses, strict duplicate prevention, pip-installable, and ready to distribute.

## Why this exists
Traditional polling loops have timing/race issues and latency.  
This plugin is the "complete form" from day one:

- **Push** (real-time on Discord events)
- **Grok-build continuity** via per-channel short-term JSON context (`last_processed_id`, `current_focus`, `key_facts`, `recent_exchanges`)
- **Sacred anti-dup rule**: `last_processed_id` is advanced **immediately before** any reply is sent
- Works great as a standalone bot **or** alongside Grok build schedulers (shared state dir)
- General purpose — skills (like いらすとら) are supported via the LLM prompt / your own extensions

## Features
- Event-driven, low latency (no 1-min polling)
- Short-term memory that survives restarts (per-channel `.json`)
- Atomic writes (`.tmp` + `os.replace`)
- Snowflake-based exact dedup guard
- Real LLM replies (xAI Grok or any OpenAI-compatible) with context injected
- Graceful fallback when no API key (still shows memory is working)
- Debug commands: `!memory` / `!ctx` and `!forget`
- Multi-channel support or "all channels"
- `python -m pip install` + `discord-grok-plugin` entrypoint
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
# DISCORD_CHANNEL_ID=1513374433367036005   # optional single channel
# DISCORD_CHANNEL_IDS=123,456              # or multiple
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
| `DISCORD_CHANNEL_ID`    | no       | Single channel to listen in |
| `DISCORD_CHANNEL_IDS`   | no       | Comma-separated list |
| `LLM_MODEL`             | no       | e.g. `grok-3-mini`, `gpt-4o-mini` |
| `LLM_MAX_TOKENS`        | no       | Default 900 |
| `DISCORD_STATE_DIR`     | no       | Override memory dir (defaults to `~/.claude/channels/discord`) |

If no channel filter is set, the bot will reply in **every channel** it has access to (useful for personal servers, be careful in big ones).

## How the Memory Works (Important)

- Every channel gets its own `context/<channel_id>.json`
- On every incoming message:
  1. Check `last_processed_id` (snowflake) → skip if already handled
  2. Load rolling context + build compact prompt snippet
  3. Call LLM (snippet + current user msg injected)
  4. **IMMEDIATELY** `update_context(..., last_id=message.id)` — this is sacred
  5. Send the reply

This ordering eliminates the classic "fetch loop saw it but reply not sent yet" duplicate race.

`recent_exchanges` is trimmed (default 8), `key_facts` capped, etc. No permanent memory — exactly as requested.

You can inspect with `!memory` or `!ctx` and wipe with `!forget` (in the channel).

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

Run tests / checks (add your own later):

```bash
pip install -e .
python -c "
from discord_grok_plugin.context import load_context, update_context, build_context_prompt_snippet, reset_context, get_last_processed_id
from discord_grok_plugin.main import run_bot, get_ai_response
import tempfile, os, shutil
from pathlib import Path

# Isolated test (does not touch real ~/.claude)
tmp = tempfile.mkdtemp(prefix='dgp-test-')
os.environ['DISCORD_STATE_DIR'] = str(Path(tmp)/'state')

ch='testchan'
update_context(ch, new_user_message='plan the plugin', ai_reply='done sacred order', last_id='111')
print('last now:', get_last_processed_id(ch))
print('snippet has focus?', 'focus' in build_context_prompt_snippet(ch).lower())
reset_context(ch)
print('reset ok')

shutil.rmtree(tmp, ignore_errors=True)
print('imports + context + sacred update-before-reply + ai stub: OK')
"
# Also: discord-grok-plugin  (with DISCORD_TOKEN in env/.env) starts the push bot
```

Debug commands (`!memory` / `!ctx` / `!forget`) are fully supported (the `on_message` handler calls `bot.process_commands` for `!` messages and after normal flow).

## License

MIT — use it, improve it, star it on GitHub.
