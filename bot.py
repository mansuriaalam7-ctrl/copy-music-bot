"""
MusicBot v5.1.0 — Entry Point
Pyrogram + PyTgCalls | JioSaavn + Spotify catalog

Rules enforced here:
  • Voice streaming ONLY through the dedicated assistant account (SESSION_SECRET).
  • If SESSION_SECRET is absent or invalid the bot starts, but all /play attempts
    are blocked with a clear "no assistant" message.
  • The bot account is NEVER used as a voice client.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Workspace root must be on sys.path so `search/` and `stream/` are importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from pyrogram import Client, idle

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("MusicBot")

# ── Config ────────────────────────────────────────────────────────────────────

# Bot credentials (from the @BotFather app)
def _read_api_id(name: str) -> int:
    value = os.environ.get(name, "").strip()
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.error("%s must be a numeric Telegram API ID.", name)
        return 0


API_ID    = _read_api_id("API_ID")
API_HASH  = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# Assistant credentials — dedicated Telegram app for @Flaxcopy.
# Falls back to the bot's API_ID/API_HASH if not set separately.
ASSISTANT_API_ID   = _read_api_id("ASSISTANT_API_ID") if os.environ.get("ASSISTANT_API_ID") else API_ID
ASSISTANT_API_HASH = os.environ.get("ASSISTANT_API_HASH", os.environ.get("API_HASH", ""))

SESSION_STR = os.environ.get("SESSION_SECRET", "").strip()
BOT_VERSION = "5.2.0"

# ── Bot client (commands only — never used for voice) ─────────────────────────

app = Client(
    "bot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)

# ── Assistant userbot (voice only) ────────────────────────────────────────────

_assistant: Optional[Client] = None
_assistant_config_error = ""
if SESSION_STR:
    try:
        _assistant = Client(
            "assistant_session",
            api_id=ASSISTANT_API_ID,
            api_hash=ASSISTANT_API_HASH,
            session_string=SESSION_STR,
            in_memory=True,
        )
        logger.info(
            "Assistant client created with api_id=%s.", ASSISTANT_API_ID
        )
    except Exception as _err:
        _assistant_config_error = "SESSION_SECRET is invalid and could not be parsed."
        logger.error(
            "%s (%s) Run bot/gen_session.py to generate a valid session string.",
            _assistant_config_error,
            _err,
        )
else:
    _assistant_config_error = "SESSION_SECRET is missing."
    logger.warning(
        "%s Voice streaming is disabled until a valid session string is added.",
        _assistant_config_error,
    )

# ── Import handlers AFTER app is created ─────────────────────────────────────
from bot.commands import register, set_runtime_info  # noqa: E402
from stream import voice                              # noqa: E402


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    global _assistant_config_error

    if not API_ID or not API_HASH or not BOT_TOKEN:
        logger.error(
            "Missing or invalid bot credentials. "
            "Set TELEGRAM_BOT_TOKEN, API_ID, and API_HASH in Secrets."
        )
        return

    # Register all command/callback handlers
    register(app)

    # Start the bot client
    await app.start()
    me = await app.get_me()
    logger.info("Bot started: @%s (v%s)", me.username, BOT_VERSION)

    # ── Attempt to connect the assistant userbot ──────────────────────────────
    assistant_me        = None
    assistant_connected = False

    if _assistant is not None:
        try:
            await _assistant.start()
            assistant_me        = await _assistant.get_me()
            if (assistant_me.username or "").casefold() != "flaxcopy":
                _assistant_config_error = (
                    "SESSION_SECRET belongs to a different Telegram account. "
                    "It must belong to @Flaxcopy."
                )
                logger.error("%s", _assistant_config_error)
                await _assistant.stop()
            else:
                assistant_connected = True
                logger.info(
                    "Assistant connected: @%s — ready for voice streaming.",
                    assistant_me.username,
                )
        except Exception as exc:
            _assistant_config_error = (
                "SESSION_SECRET is invalid or the assistant login failed."
            )
            logger.error(
                "%s (%s) Verify SESSION_SECRET is a valid Pyrogram session string.",
                _assistant_config_error,
                exc,
            )
    else:
        logger.warning(
            "%s Add a valid SESSION_SECRET to enable voice streaming.",
            _assistant_config_error or "No assistant account configured.",
        )

    # ── Initialise PyTgCalls only when the assistant is connected ─────────────
    if assistant_connected and _assistant is not None:
        from bot.player import advance_queue
        try:
            voice.init_factory(_assistant, advance_queue)
            logger.info("PyTgCalls GroupCallFactory ready (assistant account).")
        except Exception as exc:
            logger.error("Could not initialise voice engine: %s", exc)
            assistant_connected = False  # voice engine broken — block streaming
    else:
        logger.warning("Voice engine NOT initialised (no valid assistant session).")

    # Share runtime state with commands.py
    set_runtime_info(
        start_time=datetime.now(),
        assistant_me=assistant_me,
        assistant_connected=assistant_connected,
        assistant_error=_assistant_config_error,
    )

    await idle()

    # ── Cleanup ───────────────────────────────────────────────────────────────
    logger.info("Shutting down…")
    if _assistant is not None and _assistant.is_connected:
        await _assistant.stop()
    await app.stop()
    logger.info("Bot stopped cleanly.")


if __name__ == "__main__":
    app.run(main())
