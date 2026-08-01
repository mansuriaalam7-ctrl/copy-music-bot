"""
Generate a Pyrogram session string for the assistant (userbot) account.

Usage:
    python3 bot/gen_session.py

The script logs in to Telegram as a real user account, then prints a session
string.  Copy that string and save it as SESSION_SECRET in Replit Secrets.

Requirements: API_ID and API_HASH must already be set in Replit Secrets.
"""

import asyncio
import os
import sys

# Make sure pyrogram is importable when running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

try:
    from pyrogram import Client
except ImportError:
    print("ERROR: pyrogram is not installed. Run: pip install pyrogram tgcrypto")
    sys.exit(1)

# Use dedicated assistant credentials when available, fall back to main bot app.
try:
    API_ID = int(os.environ.get("ASSISTANT_API_ID", os.environ.get("API_ID", "0")))
except ValueError:
    API_ID = 0
API_HASH = os.environ.get("ASSISTANT_API_HASH", os.environ.get("API_HASH", ""))


async def main() -> None:
    if not API_ID or not API_HASH:
        print(
            "ERROR: Set API_ID and API_HASH in Replit Secrets first "
            "(or ASSISTANT_API_ID and ASSISTANT_API_HASH)."
        )
        sys.exit(1)

    print("=" * 60)
    print("  Pyrogram Session String Generator")
    print("  for MusicBot assistant account")
    print("=" * 60)
    print()
    print("You will be asked to log in with the REAL @Flaxcopy Telegram account.")
    print("This account will join voice chats and stream music.")
    print("(It must be @Flaxcopy, and does not need to own the bot.)")
    print()

    async with Client(
        "assistant_gen",
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True,
    ) as client:
        session_string = await client.export_session_string()
        me = await client.get_me()

    if (me.username or "").casefold() != "flaxcopy":
        print()
        print(
            "ERROR: The logged-in account is not @Flaxcopy. "
            "Log in with the dedicated assistant account and run this again."
        )
        sys.exit(1)

    print()
    print("=" * 60)
    print(f"  Logged in as: {me.first_name} (@{me.username})")
    print("=" * 60)
    print()
    print("Copy the session string below and save it as SESSION_SECRET")
    print("in Replit Secrets (Tools → Secrets → New secret):")
    print()
    print(session_string)
    print()
    print("After saving, restart the 'Telegram Bot' workflow.")


if __name__ == "__main__":
    asyncio.run(main())
