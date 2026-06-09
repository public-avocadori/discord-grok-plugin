import discord
from discord.ext import commands
import os
from typing import List, Optional, Set

from dotenv import load_dotenv
from openai import OpenAI

from .context import (
    load_context,
    update_context,
    build_context_prompt_snippet,
    reset_context,
    get_last_processed_id,
)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


def _get_allowed_channels() -> Optional[Set[str]]:
    """Parse allowed channels from env.
    Supports DISCORD_CHANNEL_ID (single) or DISCORD_CHANNEL_IDS (comma separated).
    If none set, allow all (bot will answer in any channel it can read).
    """
    single = os.getenv("DISCORD_CHANNEL_ID")
    multi = os.getenv("DISCORD_CHANNEL_IDS")
    if not single and not multi:
        return None  # all channels
    ids: Set[str] = set()
    if single:
        ids.add(single.strip())
    if multi:
        for part in multi.split(","):
            p = part.strip()
            if p:
                ids.add(p)
    return ids if ids else None


def _is_allowed(channel_id: str, allowed: Optional[Set[str]]) -> bool:
    return allowed is None or channel_id in allowed


def get_ai_response(snippet: str, user_message: str, channel_id: str) -> str:
    """Call LLM (xAI Grok or OpenAI compatible) with injected short-term context.
    Falls back to a memory-aware stub if no API key.
    """
    xai_key = os.getenv("XAI_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    api_key = xai_key or openai_key

    if not api_key:
        # No key: still provide value via memory (user sees context is working)
        preview = (snippet or "No prior context.")[:400]
        return (
            "[discord-grok-plugin] No XAI_API_KEY or OPENAI_API_KEY set.\n"
            "Short-term memory is active. Context for this channel:\n"
            f"{preview}\n\n"
            f"Your message: {user_message[:200]}\n\n"
            "Set an API key to enable real Grok/OpenAI replies with continuity."
        )

    base_url = "https://api.x.ai/v1" if xai_key else None
    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)

    model = os.getenv("LLM_MODEL") or ("grok-3-mini" if xai_key else "gpt-4o-mini")
    max_tokens = int(os.getenv("LLM_MAX_TOKENS", "900"))

    system_prompt = (
        "You are a helpful, concise AI assistant participating in an ongoing Discord conversation. "
        "You have access to short-term rolling context from previous messages in this channel (Grok build session style). "
        "Use the context to maintain continuity: do not ask the user to repeat information, decisions, or facts already stated. "
        "Keep replies focused and under ~500 words unless the user asks for detail. "
        "If the provided context block is present, treat it as authoritative recent history."
    )

    messages = [{"role": "system", "content": system_prompt}]
    if snippet:
        messages.append({
            "role": "system",
            "content": f"[Short-term context for channel {channel_id}]\n{snippet}"
        })
    messages.append({"role": "user", "content": user_message})

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.7,
        )
        content = (resp.choices[0].message.content or "").strip()
        return content or "[LLM returned empty response. Memory updated anyway.]"
    except Exception as e:
        # Never leak keys or full traces to Discord
        err = str(e)[:120]
        return f"[LLM error while generating reply: {err}. Short-term context was loaded and will be preserved.]"


@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('discord-grok-plugin ready. Push-based (event-driven), short-term memory, sacred last_id-before-reply.')


@bot.event
async def on_message(message: discord.Message):
    # Never respond to self
    if message.author == bot.user:
        return

    # Ignore other bots by default (prevents loops)
    if getattr(message.author, "bot", False):
        return

    channel_id = str(message.channel.id)
    allowed = _get_allowed_channels()
    if not _is_allowed(channel_id, allowed):
        return

    # Strict anti-dup using snowflake numeric compare (sacred) — do this before heavy work
    last_processed = get_last_processed_id(channel_id)
    if last_processed:
        try:
            if int(message.id) <= int(last_processed):
                return
        except (ValueError, TypeError):
            pass  # if corrupt, proceed (will overwrite)

    # Load full context (for snippet + optional focus carry-over)
    ctx = load_context(channel_id)

    user_msg = (message.content or "").strip()
    if not user_msg:
        # Ignore pure embeds/attachments for now (can extend later)
        await bot.process_commands(message)
        return

    # Commands (e.g. !memory, !forget) are handled by the command system, not the auto-responder.
    # This prevents:
    #   - Double replies (AI text + command output)
    #   - Injecting "!cmd ..." into short-term recent_exchanges / memory as if normal chat
    if user_msg.startswith("!"):
        await bot.process_commands(message)
        return

    # Build Grok-build-style short-term context snippet for prompt injection
    snippet = build_context_prompt_snippet(channel_id)

    # === Generate reply (real LLM or stub) ===
    ai_response = get_ai_response(snippet, user_msg, channel_id)

    # === SACRED ORDER: advance last_processed_id + persist exchange BEFORE any Discord send/reply ===
    # This prevents duplicate processing from races, reconnects, or re-delivery.
    update_context(
        channel_id,
        new_user_message=user_msg,
        ai_reply=ai_response,
        last_id=str(message.id),
        focus_update=ctx.get("current_focus") or "Active conversation with short-term Grok-build memory via discord-grok-plugin",
    )

    # Now safe to reply (push, immediate, low latency)
    try:
        await message.channel.send(ai_response)
    except Exception as send_err:
        # Still keep the id advanced so we don't reprocess the same msg forever
        print(f"[discord-grok-plugin] Send failed for {channel_id}/{message.id}: {send_err}")

    # Let the commands system run for normal messages too (harmless if no prefix match).
    await bot.process_commands(message)


# Debug / admin commands (usable in any channel the bot sees)
@bot.command(name="memory", aliases=["ctx", "context"])
async def memory_cmd(ctx_cmd: commands.Context):
    """Show the current short-term context snippet for this channel (for debugging continuity)."""
    ch = str(ctx_cmd.channel.id)
    snip = build_context_prompt_snippet(ch)
    if not snip:
        await ctx_cmd.send("No short-term context stored for this channel yet.")
        return
    # Discord message limit ~2000 chars
    if len(snip) > 1850:
        snip = snip[:1840] + "\n... (truncated)"
    await ctx_cmd.send(f"**Short-term context (channel {ch}):**\n```\n{snip}\n```")


@bot.command(name="forget")
async def forget_cmd(ctx_cmd: commands.Context):
    """Reset short-term memory for the current channel (owner/debug use)."""
    ch = str(ctx_cmd.channel.id)
    reset_context(ch)
    await ctx_cmd.send("Short-term memory cleared for this channel.")


def run_bot():
    load_dotenv()  # support .env in cwd (nice for local runs on Windows too)
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("ERROR: DISCORD_TOKEN is required (set in env or .env file).")
        print("Optional: DISCORD_CHANNEL_ID or DISCORD_CHANNEL_IDS (comma sep), XAI_API_KEY / OPENAI_API_KEY,")
        print("          LLM_MODEL, LLM_MAX_TOKENS, DISCORD_STATE_DIR")
        return
    print("Starting discord-grok-plugin (push-based high-perf + Grok-build short-term memory)...")
    bot.run(token)


if __name__ == "__main__":
    run_bot()
