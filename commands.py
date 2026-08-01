"""
All Pyrogram command and callback-query handlers.

Permission model
----------------
• /play, /pause, /resume, /skip, /queue, /nowplaying, /lyrics — everyone
• /stop — everyone (gracefully ends the session)
• /admin panel, admin_* callbacks — group admins only
• /start, /help, /ping, /stats, /id — everyone
"""

from __future__ import annotations

import html
import logging
import time
from datetime import datetime
from typing import Optional
from urllib.parse import quote

import aiohttp

from pyrogram import filters
from pyrogram.enums import ChatMemberStatus, ChatType, ParseMode
from pyrogram.errors import UserNotParticipant
from pyrogram import Client
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from stream import queue as q_state
from stream import voice
from bot.player import advance_queue, search_catalog, get_authorized_stream

logger = logging.getLogger("MusicBot.Commands")

# ── Constants ─────────────────────────────────────────────────────────────────
BOT_NAME           = "MusicBot"
BOT_VERSION        = "5.2.0"
SUPPORT_URL        = "https://t.me/copytry"
UPDATES_URL        = "https://t.me/copymusicofficial"
ASSISTANT_USERNAME = "Flaxcopy"
ANONYMOUS_ADMIN_ID = 1087968824
PAGE_SIZE          = 5
WELCOME_BANNER     = (
    "https://images.unsplash.com/photo-1511379938547-c1f69419868d"
    "?w=800&q=80&fit=crop"
)

# Runtime references — set once by bot.py after startup
_bot_start_time:      datetime = datetime.now()
_command_count:       int      = 0
_active_chats:        set[int] = set()
_assistant_me                  = None
_assistant_connected: bool     = False
_assistant_error: str          = "SESSION_SECRET is missing."


def set_runtime_info(
    start_time: datetime,
    assistant_me=None,
    assistant_connected: bool = False,
    assistant_error: str = "",
) -> None:
    global _bot_start_time, _assistant_me, _assistant_connected, _assistant_error
    _bot_start_time      = start_time
    _assistant_me        = assistant_me
    _assistant_connected = assistant_connected
    _assistant_error     = assistant_error or "Assistant account is not connected."


def _track(chat_id: int) -> None:
    global _command_count
    _command_count += 1
    _active_chats.add(chat_id)


# ── Formatting helpers ────────────────────────────────────────────────────────

def fmt_dur(seconds: int) -> str:
    if not seconds:
        return "0:00"
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def _progress_bar(elapsed: int, duration: int, width: int = 12) -> str:
    """Return a Unicode progress bar for the now-playing card."""
    if not duration:
        return "━" * width
    pct    = min(1.0, elapsed / duration)
    filled = round(pct * width)
    return "▰" * filled + "▱" * (width - filled)


# ── Permission helpers ────────────────────────────────────────────────────────

def _is_anon_admin(message: Message) -> bool:
    if message.sender_chat:
        return message.sender_chat.id == message.chat.id
    return bool(message.from_user and message.from_user.id == ANONYMOUS_ADMIN_ID)


async def _is_admin(client: Client, message: Message) -> bool:
    if message.chat.type == ChatType.PRIVATE:
        return True
    if _is_anon_admin(message):
        return True
    if not message.from_user:
        return False
    try:
        m = await client.get_chat_member(message.chat.id, message.from_user.id)
        return m.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
    except Exception:
        return False


async def _is_admin_by_id(client: Client, chat_id: int, user_id: int) -> bool:
    try:
        chat = await client.get_chat(chat_id)
        if chat.type == ChatType.PRIVATE:
            return True
    except Exception:
        pass
    try:
        m = await client.get_chat_member(chat_id, user_id)
        return m.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
    except Exception:
        return False


async def _require_callback_admin(
    client: Client, query: CallbackQuery, chat_id: int, user_id: int
) -> bool:
    if await _is_admin_by_id(client, chat_id, user_id):
        return True
    await query.answer("🚫 Group admins only.", show_alert=True)
    return False


# ── Assistant helpers ─────────────────────────────────────────────────────────

async def _assistant_in_group(client: Client, chat_id: int) -> bool:
    """Check whether @Flaxcopy is already a member of this group."""
    try:
        member = await client.get_chat_member(chat_id, ASSISTANT_USERNAME)
        return member.status not in (ChatMemberStatus.BANNED, ChatMemberStatus.LEFT)
    except UserNotParticipant:
        return False
    except Exception as exc:
        logger.warning("assistant_in_group(%d): %s", chat_id, exc)
        return False


async def _ensure_assistant_in_group(client: Client, chat_id: int) -> bool:
    """Try to invite @Flaxcopy; return True when the assistant is a member."""
    if await _assistant_in_group(client, chat_id):
        return True
    try:
        await client.add_chat_members(chat_id, ASSISTANT_USERNAME, forward_limit=0)
    except Exception as exc:
        logger.info("Auto-invite of assistant failed for %d: %s", chat_id, exc)
        return False
    return await _assistant_in_group(client, chat_id)


# ── Lyrics helper ─────────────────────────────────────────────────────────────

async def _fetch_lyrics(artist: str, title: str) -> Optional[str]:
    if not title.strip():
        return None
    url = f"https://api.lyrics.ovh/v1/{quote(artist or 'Unknown')}/{quote(title)}"
    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return None
                payload = await response.json(content_type=None)
        lyrics = payload.get("lyrics") if isinstance(payload, dict) else None
        return lyrics.strip() if isinstance(lyrics, str) and lyrics.strip() else None
    except Exception:
        return None


# ── Invite keyboard ───────────────────────────────────────────────────────────

def _make_invite_keyboard() -> InlineKeyboardMarkup:
    """Keyboard shown when the assistant needs to be added to the group.

    Opens the @Flaxcopy profile so the user can add them as a group member.
    Does NOT use ?startgroup=true (that is for bots only).
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "➕  Add @Flaxcopy to Group",
            url=f"https://t.me/{ASSISTANT_USERNAME}",
        )],
        [InlineKeyboardButton(
            "🔄  I've added them — retry",
            callback_data="noop",
        )],
    ])


# ── Voice-chat detection ──────────────────────────────────────────────────────

async def _voice_chat_active(client: Client, chat_id: int) -> bool:
    """Return True if the group has an active voice chat or live stream.

    Returns True on any API error so that a temporary network glitch never
    blocks the user from playing music — PyTgCalls handles the real check.
    """
    try:
        from pyrogram.raw import functions
        peer = await client.resolve_peer(chat_id)
        if hasattr(peer, "channel_id"):
            result = await client.invoke(
                functions.channels.GetFullChannel(channel=peer)
            )
        else:
            result = await client.invoke(
                functions.messages.GetFullChat(chat_id=abs(chat_id))
            )
        return result.full_chat.call is not None
    except Exception as exc:
        # Fail permissively — assume active so the user can proceed.
        logger.debug("VC detection inconclusive for %d (%s); assuming active.", chat_id, exc)
        return True


# ── Pre-play checks ───────────────────────────────────────────────────────────

async def _preflight(client: Client, message: Message) -> bool:
    """Return True only when all conditions for streaming are met.

    Checks (in order):
      1. Group chat (not private DM)
      2. Assistant account connected
      3. @Flaxcopy is a member of this group

    Voice-chat existence is NOT checked here — PyTgCalls will either join an
    existing call or create a new one when call.start() is invoked.
    """
    chat_id   = message.chat.id
    chat_type = message.chat.type

    # 1. Groups only
    if chat_type == ChatType.PRIVATE:
        await message.reply(
            "🎙️ <b>Groups only</b>\n\n"
            "Add me to a group and use /play there.",
            parse_mode=ParseMode.HTML,
        )
        return False

    # 2. Assistant account must be connected
    if not _assistant_connected:
        await message.reply(
            "⚠️ <b>Assistant Account Not Connected</b>\n\n"
            "Voice streaming requires the @Flaxcopy assistant account.\n\n"
            f"<b>Error:</b> {html.escape(_assistant_error)}\n\n"
            "The bot owner must set a valid <code>SESSION_SECRET</code> "
            "(Pyrogram session string) in Replit Secrets.\n"
            "Run <code>python3 bot/gen_session.py</code> from the Shell to generate one.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💬  Support", url=SUPPORT_URL)
            ]]),
        )
        return False

    # 3. @Flaxcopy must be in this group
    if not await _ensure_assistant_in_group(client, chat_id):
        await message.reply(
            "🤖 <b>Add the Assistant First</b>\n\n"
            "<b>@Flaxcopy</b> must be a member of this group before music can play.\n\n"
            "<b>Steps:</b>\n"
            "1️⃣  Tap the button below to open @Flaxcopy's profile\n"
            "2️⃣  Tap <i>Add to Group</i> and select this group\n"
            "3️⃣  Send /play again",
            parse_mode=ParseMode.HTML,
            reply_markup=_make_invite_keyboard(),
        )
        return False

    return True


# ── Now-playing card helpers ──────────────────────────────────────────────────

def _now_playing_caption(chat_id: int) -> str:
    """Build the now-playing caption with inline progress bar."""
    song   = q_state.current_song(chat_id)
    paused = chat_id in q_state.paused_chats

    if not song:
        return (
            "🔇 <b>Nothing is playing.</b>\n\n"
            "Use /play &lt;song name&gt; to start!"
        )

    duration  = int(song.get("duration") or 0)
    started   = float(song.get("started_at") or 0)
    elapsed   = min(int(time.time() - started) if started else 0, duration)
    bar       = _progress_bar(elapsed, duration)
    status    = "⏸  PAUSED" if paused else "▶️  PLAYING"
    q_len     = q_state.queue_length(chat_id)

    lines = [
        f"<b>{status}</b>\n",
        f"🎵  <b>{html.escape(song.get('title', '—'))}</b>",
        f"👤  {html.escape(str(song.get('uploader', 'Unknown')))}",
        f"⏱  {fmt_dur(elapsed)}  {bar}  {fmt_dur(duration)}",
        f"📥  Requested by <b>{html.escape(str(song.get('requested_by', 'Unknown')))}</b>",
    ]
    if q_len > 1:
        lines.append(f"📋  {q_len - 1} more in queue")

    return "\n".join(lines)


def _thumbnail_url(song: dict) -> str:
    """Return the best available thumbnail URL for a track."""
    vid_id = song.get("youtube_id", "")
    if vid_id:
        return f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg"
    return song.get("thumbnail", "")


async def _send_now_playing(message: Message, chat_id: int, song: dict) -> None:
    """Send a now-playing card — photo with caption when a thumbnail is available."""
    caption  = _now_playing_caption(chat_id)
    kb       = kb_now_playing(chat_id)
    thumb    = _thumbnail_url(song)

    if thumb:
        try:
            await message.reply_photo(
                thumb, caption=caption,
                parse_mode=ParseMode.HTML, reply_markup=kb,
            )
            return
        except Exception:
            pass  # fall through to text reply

    await message.reply(caption, parse_mode=ParseMode.HTML, reply_markup=kb)


async def _edit_now_playing(query: CallbackQuery, chat_id: int) -> None:
    """Edit an existing message to show the current now-playing state."""
    caption = _now_playing_caption(chat_id)
    kb      = kb_now_playing(chat_id)
    try:
        if query.message.photo:
            await query.message.edit_caption(
                caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb,
            )
        else:
            await query.message.edit_text(
                caption, parse_mode=ParseMode.HTML, reply_markup=kb,
            )
    except Exception as exc:
        logger.debug("_edit_now_playing: %s", exc)


# ── Generic edit helper ───────────────────────────────────────────────────────

async def _edit(
    query: CallbackQuery,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    try:
        if query.message.photo:
            await query.message.edit_caption(
                caption=text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
            )
        else:
            await query.message.edit_text(
                text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
            )
    except Exception as exc:
        logger.debug("_edit: %s", exc)


# ── Keyboards ─────────────────────────────────────────────────────────────────

def kb_main(bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎵  Music Menu",  callback_data="menu_music"),
            InlineKeyboardButton("📋  Playlist",    callback_data="menu_playlist_0"),
        ],
        [
            InlineKeyboardButton("⚙️  Settings",   callback_data="menu_settings"),
            InlineKeyboardButton("🛡️  Admin",       callback_data="menu_admin"),
        ],
        [
            InlineKeyboardButton("📊  Stats",       callback_data="menu_stats"),
            InlineKeyboardButton("ℹ️  About",       callback_data="menu_about"),
        ],
        [InlineKeyboardButton(
            "➕  Add to Group",
            url=f"https://t.me/{bot_username}?startgroup=true",
        )],
        [
            InlineKeyboardButton("💬  Support",  url=SUPPORT_URL),
            InlineKeyboardButton("📢  Updates",  url=UPDATES_URL),
        ],
    ])


def kb_music(chat_id: int) -> InlineKeyboardMarkup:
    playing = voice.is_connected(chat_id) and chat_id not in q_state.paused_chats
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎵  Now Playing", callback_data="act_nowplaying"),
            InlineKeyboardButton("📜  Queue",        callback_data="act_queue"),
        ],
        [
            InlineKeyboardButton(
                "⏸  Pause" if playing else "▶️  Resume",
                callback_data="act_pause" if playing else "act_resume",
            ),
            InlineKeyboardButton("⏭️  Skip",  callback_data="act_skip"),
        ],
        [InlineKeyboardButton("⏹️  Stop",    callback_data="act_stop")],
        [InlineKeyboardButton("🏠  Main Menu", callback_data="menu_main")],
    ])


def kb_now_playing(chat_id: int) -> InlineKeyboardMarkup:
    playing = voice.is_connected(chat_id) and chat_id not in q_state.paused_chats
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⏸  Pause" if playing else "▶️  Resume",
                callback_data="act_pause" if playing else "act_resume",
            ),
            InlineKeyboardButton("⏭️  Skip",  callback_data="act_skip"),
            InlineKeyboardButton("⏹️  Stop",  callback_data="act_stop"),
        ],
        [
            InlineKeyboardButton("📜  Queue",  callback_data="act_queue"),
            InlineKeyboardButton("🏠  Menu",   callback_data="menu_main"),
        ],
    ])


def kb_playlist(uid: int, page: int) -> InlineKeyboardMarkup:
    songs = q_state.get_playlist(uid)
    total = max(1, (len(songs) + PAGE_SIZE - 1) // PAGE_SIZE)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"menu_playlist_{page - 1}"))
    nav.append(InlineKeyboardButton(f"· {page + 1}/{total} ·", callback_data="noop"))
    if page < total - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"menu_playlist_{page + 1}"))
    return InlineKeyboardMarkup([
        nav,
        [
            InlineKeyboardButton("➕  Add Song",  callback_data="act_addsong_hint"),
            InlineKeyboardButton("🗑️  Remove",   callback_data="act_remove_hint"),
        ],
        [InlineKeyboardButton("🏠  Main Menu", callback_data="menu_main")],
    ])


def kb_settings() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐  Language  ›  English",    callback_data="noop")],
        [InlineKeyboardButton("🔔  Notifications  ›  On",    callback_data="noop")],
        [InlineKeyboardButton("🎚️  Queue Mode  ›  Public",  callback_data="noop")],
        [InlineKeyboardButton("🎨  Theme  ›  Dark",          callback_data="noop")],
        [InlineKeyboardButton("🏠  Main Menu",               callback_data="menu_main")],
    ])


def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊  Statistics",     callback_data="admin_stats"),
            InlineKeyboardButton("🗑️  Clear Queue",   callback_data="admin_clearqueue"),
        ],
        [
            InlineKeyboardButton("📋  Playlists",      callback_data="admin_pl_overview"),
            InlineKeyboardButton("👥  Active Chats",   callback_data="admin_chats"),
        ],
        [InlineKeyboardButton("🔊  Leave VC",           callback_data="admin_leavevc")],
        [InlineKeyboardButton("🏠  Main Menu",           callback_data="menu_main")],
    ])


def kb_back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🏠  Main Menu", callback_data="menu_main"),
    ]])


def kb_back_music(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("«  Music",  callback_data="menu_music"),
        InlineKeyboardButton("🏠  Main",  callback_data="menu_main"),
    ]])


def kb_back_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("«  Admin",  callback_data="menu_admin"),
        InlineKeyboardButton("🏠  Main",  callback_data="menu_main"),
    ]])


# ── UI text builders ──────────────────────────────────────────────────────────

def text_welcome(first_name: str) -> str:
    return (
        f"🎵  <b>Welcome to {BOT_NAME}, {first_name}!</b>\n\n"
        "Music streaming for Telegram voice chats.\n\n"
        "<b>✨ Features</b>\n"
        "  🎶  YouTube search via yt-dlp\n"
        "  📋  Personal playlists\n"
        "  🎤  Real-time voice streaming\n"
        "  📊  Live statistics\n"
        "  ⌨️  Full inline navigation\n\n"
        "<i>Add me to a group, then /play &lt;song name&gt;!</i>"
    )


def text_queue(chat_id: int) -> str:
    q = q_state.get_queue(chat_id)
    if not q:
        return (
            "📭 <b>Queue is empty.</b>\n\n"
            "Use /play &lt;song&gt; to add songs."
        )
    lines = [f"<b>📋 QUEUE</b>  ({len(q)} songs)\n"]
    for i, s in enumerate(q):
        icon = "▶️" if i == 0 else f"{i + 1}."
        lines.append(f"{icon}  <b>{html.escape(s.get('title', '?'))}</b>  ⏱ {fmt_dur(s.get('duration', 0))}")
    return "\n".join(lines)


def text_playlist_page(uid: int, page: int) -> str:
    songs = q_state.get_playlist(uid)
    if not songs:
        return (
            "📭 <b>Your playlist is empty.</b>\n\n"
            "Use /addsong &lt;name&gt; to save songs."
        )
    total = max(1, (len(songs) + PAGE_SIZE - 1) // PAGE_SIZE)
    start = page * PAGE_SIZE
    lines = [f"<b>📋 MY PLAYLIST</b>  ({len(songs)} songs · Page {page + 1}/{total})\n"]
    for i, s in enumerate(songs[start: start + PAGE_SIZE], start + 1):
        lines.append(f"  {i}.  <b>{html.escape(s.get('title', '?'))}</b>")
    return "\n".join(lines)


def text_music_menu() -> str:
    return (
        "<b>🎵 MUSIC MENU</b>\n\n"
        "  /play &lt;song&gt;  —  Search &amp; stream\n"
        "  /nowplaying    —  Current song\n"
        "  /queue         —  View queue\n"
        "  /pause         —  Pause playback\n"
        "  /resume        —  Resume playback\n"
        "  /skip          —  Skip current song\n"
        "  /stop          —  Stop &amp; clear queue\n"
        "  /lyrics        —  Current song lyrics\n"
    )


def text_settings() -> str:
    return (
        "<b>⚙️ SETTINGS</b>\n\n"
        "  🌐  Language\n"
        "  🔔  Notifications\n"
        "  🎚️  Queue Mode\n"
        "  🎨  Theme\n\n"
        "<i>Tap a setting to toggle.</i>"
    )


def text_admin_menu() -> str:
    return (
        "<b>🛡️ ADMIN PANEL</b>\n\n"
        "  📊  Statistics\n"
        "  🗑️  Clear Queue\n"
        "  📋  Playlists overview\n"
        "  👥  Active chats\n"
        "  🔊  Leave VC\n"
    )


def text_stats() -> str:
    up = datetime.now() - _bot_start_time
    h, r = divmod(int(up.total_seconds()), 3600)
    m, s = divmod(r, 60)
    total_q  = sum(len(v) for v in q_state.queues.values())
    total_pl = q_state.playlist_song_count()
    if _assistant_connected and _assistant_me is not None:
        ast_status = f"🟢 @{_assistant_me.username}"
    else:
        ast_status = f"🔴 {html.escape(_assistant_error)}"
    return (
        "<b>📊 BOT STATISTICS</b>\n\n"
        f"  ⏱️  Uptime           ›  {h}h {m}m {s}s\n"
        f"  💬  Active chats    ›  {len(_active_chats)}\n"
        f"  ⚡  Commands        ›  {_command_count}\n"
        f"  🔊  Live calls      ›  {voice.active_call_count()}\n"
        f"  🎵  Songs in queues ›  {total_q}\n"
        f"  📋  Playlist songs  ›  {total_pl}\n"
        f"  👥  Playlist users  ›  {len(q_state.playlists)}\n"
        f"  🤖  Version         ›  {BOT_VERSION}\n"
        f"  🎤  Voice engine    ›  {ast_status}\n"
    )


def text_about() -> str:
    return (
        f"<b>ℹ️ ABOUT {BOT_NAME.upper()}</b>\n\n"
        f"  🤖  Version   ›  {BOT_VERSION}\n"
        "  📦  Framework ›  Pyrogram\n"
        "  🎤  Voice     ›  PyTgCalls\n"
        "  🎵  Catalog   ›  YouTube via yt-dlp\n"
        "  🐍  Runtime   ›  Python 3.10\n\n"
        "  Premium music streaming for\n"
        "  Telegram group voice chats.\n\n"
        f"  💬  <a href='{SUPPORT_URL}'>Support</a>  ·  "
        f"  📢  <a href='{UPDATES_URL}'>Updates</a>\n"
    )


# ── Command handlers ──────────────────────────────────────────────────────────

def register(app):
    """Register all handlers on the given Pyrogram Client instance."""

    # /start ──────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("start"))
    async def cmd_start(client: Client, message: Message) -> None:
        _track(message.chat.id)
        me    = await client.get_me()
        fname = message.from_user.first_name if message.from_user else "there"
        kb    = kb_main(me.username)
        cap   = text_welcome(fname)
        try:
            await message.reply_photo(
                WELCOME_BANNER, caption=cap,
                parse_mode=ParseMode.HTML, reply_markup=kb,
            )
        except Exception:
            await message.reply(cap, parse_mode=ParseMode.HTML, reply_markup=kb)

    # /menu ───────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("menu"))
    async def cmd_menu(client: Client, message: Message) -> None:
        _track(message.chat.id)
        me    = await client.get_me()
        fname = message.from_user.first_name if message.from_user else "there"
        await message.reply(
            text_welcome(fname), parse_mode=ParseMode.HTML,
            reply_markup=kb_main(me.username),
        )

    # /help ───────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("help"))
    async def cmd_help(client: Client, message: Message) -> None:
        _track(message.chat.id)
        await message.reply(
            text_music_menu(), parse_mode=ParseMode.HTML,
            reply_markup=kb_music(message.chat.id),
        )

    # /ping ───────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("ping"))
    async def cmd_ping(client: Client, message: Message) -> None:
        _track(message.chat.id)
        t0    = message.date.timestamp()
        reply = await message.reply("🏓 <b>Pong!</b>", parse_mode=ParseMode.HTML)
        ms    = round((reply.date.timestamp() - t0) * 1000)
        await reply.edit_text(
            f"🏓 <b>Pong!</b>  <code>{ms} ms</code>",
            parse_mode=ParseMode.HTML, reply_markup=kb_back_main(),
        )

    # /stats ──────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("stats"))
    async def cmd_stats(client: Client, message: Message) -> None:
        _track(message.chat.id)
        await message.reply(
            text_stats(), parse_mode=ParseMode.HTML, reply_markup=kb_back_main(),
        )

    # /id ─────────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("id"))
    async def cmd_id(client: Client, message: Message) -> None:
        _track(message.chat.id)
        uid = message.from_user.id if message.from_user else "unknown"
        await message.reply(
            f"🪪 <b>Your ID</b>\n\n"
            f"  User: <code>{uid}</code>\n"
            f"  Chat: <code>{message.chat.id}</code>",
            parse_mode=ParseMode.HTML, reply_markup=kb_back_main(),
        )

    # /whoami ─────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("whoami"))
    async def cmd_whoami(client: Client, message: Message) -> None:
        _track(message.chat.id)
        anon  = _is_anon_admin(message)
        admin = await _is_admin(client, message)
        uid   = "anonymous" if anon else str(message.from_user.id if message.from_user else "?")
        await message.reply(
            "👤 <b>Who Am I?</b>\n\n"
            f"  🪪  User ID: <code>{uid}</code>\n"
            f"  💬  Chat ID: <code>{message.chat.id}</code>\n"
            f"  🎭  Role: {'👑 Admin' if admin else '👤 Member'}\n"
            f"  🕵️  Anonymous Admin: {'Yes' if anon else 'No'}",
            parse_mode=ParseMode.HTML, reply_markup=kb_back_main(),
        )

    # /play ───────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("play"))
    async def cmd_play(client: Client, message: Message) -> None:
        _track(message.chat.id)
        chat_id = message.chat.id

        # Usage hint
        if not message.command[1:]:
            await message.reply(
                "💡 <b>Usage:</b>  /play &lt;song name&gt;\n\n"
                "Example:  <code>/play Alan Walker Faded</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        query    = " ".join(message.command[1:])
        req_name = (message.from_user.first_name if message.from_user else None) or "Someone"

        # Pre-flight (group checks + assistant checks)
        if message.chat.type != ChatType.PRIVATE:
            if not await _preflight(client, message):
                return

        # Searching…
        search_msg = await message.reply(
            f"🔍  <b>Searching YouTube…</b>\n\n<i>{html.escape(query)}</i>",
            parse_mode=ParseMode.HTML,
        )

        track = await search_catalog(query)
        if not track:
            await search_msg.edit_text(
                "❌ <b>Not Found</b>\n\n"
                "No results on YouTube or SoundCloud.\n"
                "Try a different search term.",
                parse_mode=ParseMode.HTML,
            )
            return

        stream_url = get_authorized_stream(track)
        if not stream_url:
            await search_msg.edit_text(
                "❌ <b>No Stream Available</b>\n\n"
                "Found the track but could not obtain an audio stream.\n"
                "Try another search.",
                parse_mode=ParseMode.HTML,
            )
            return

        track["requested_by"] = req_name
        pos = q_state.enqueue(chat_id, track)

        if pos == 1:
            # First song — start streaming now
            await search_msg.edit_text(
                f"⏳  <b>Loading…</b>\n\n"
                f"🎵  <b>{html.escape(track['title'])}</b>\n"
                f"👤  {html.escape(str(track.get('uploader', 'Unknown')))}\n"
                f"⏱️  {fmt_dur(track.get('duration', 0))}\n\n"
                "<i>Joining voice chat and buffering audio…</i>",
                parse_mode=ParseMode.HTML,
            )

            ok = await voice.stream_song(chat_id, track)
            if ok:
                try:
                    await search_msg.delete()
                except Exception:
                    pass
                await _send_now_playing(message, chat_id, track)
            else:
                q_state.clear_queue(chat_id)
                await search_msg.edit_text(
                    "❌ <b>Playback Failed</b>\n\n"
                    "<b>Common causes:</b>\n"
                    "• No active voice chat in this group\n"
                    "• Bot or @Flaxcopy lacks voice-chat permissions\n"
                    "• @Flaxcopy is not in this group\n\n"
                    "Start a voice chat, confirm @Flaxcopy is a member, then try again.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("➕  Add @Flaxcopy", url=f"https://t.me/{ASSISTANT_USERNAME}"),
                        InlineKeyboardButton("💬  Support", url=SUPPORT_URL),
                    ]]),
                )
        else:
            # Added to queue
            await search_msg.edit_text(
                f"✅  <b>Added to Queue</b>\n\n"
                f"🎵  <b>{html.escape(track['title'])}</b>\n"
                f"👤  {html.escape(str(track.get('uploader', 'Unknown')))}\n"
                f"⏱️  {fmt_dur(track.get('duration', 0))}\n"
                f"📍  Position: <b>#{pos}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_now_playing(chat_id),
            )

    # /nowplaying ─────────────────────────────────────────────────────────────

    @app.on_message(filters.command("nowplaying"))
    async def cmd_nowplaying(client: Client, message: Message) -> None:
        _track(message.chat.id)
        song = q_state.current_song(message.chat.id)
        if song:
            await _send_now_playing(message, message.chat.id, song)
        else:
            await message.reply(
                "🔇 <b>Nothing is playing.</b>\n\nUse /play &lt;song&gt; to start!",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_back_main(),
            )

    # /lyrics ─────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("lyrics"))
    async def cmd_lyrics(client: Client, message: Message) -> None:
        _track(message.chat.id)
        song = q_state.current_song(message.chat.id)
        if not song:
            await message.reply(
                "🎤 <b>No song is playing.</b>\n\nStart one with /play, then use /lyrics.",
                parse_mode=ParseMode.HTML,
            )
            return

        reply = await message.reply("🎤 <b>Looking up lyrics…</b>", parse_mode=ParseMode.HTML)
        lyrics = await _fetch_lyrics(song.get("uploader", ""), song.get("title", ""))
        if not lyrics:
            await reply.edit_text(
                "❌ <b>Lyrics not found</b>\n\nNot available for this track.",
                parse_mode=ParseMode.HTML,
            )
            return

        body = (
            f"🎤 <b>{html.escape(song.get('title', '—'))}</b>\n"
            f"<i>{html.escape(song.get('uploader', 'Unknown'))}</i>\n\n"
            f"{html.escape(lyrics)}"
        )
        await reply.edit_text(body[:4096], parse_mode=ParseMode.HTML)

    # /queue ──────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("queue"))
    async def cmd_queue(client: Client, message: Message) -> None:
        _track(message.chat.id)
        await message.reply(
            text_queue(message.chat.id), parse_mode=ParseMode.HTML,
            reply_markup=kb_back_music(message.chat.id),
        )

    # /pause ──────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("pause"))
    async def cmd_pause(client: Client, message: Message) -> None:
        _track(message.chat.id)
        chat_id = message.chat.id
        if not voice.is_connected(chat_id):
            await message.reply("❌ Nothing is playing.")
            return
        if chat_id in q_state.paused_chats:
            await message.reply("⚠️ Already paused. Use /resume to continue.")
            return
        voice.pause(chat_id)
        song = q_state.current_song(chat_id)
        await message.reply(
            f"⏸  <b>Paused</b>\n\n🎵  {html.escape(song['title']) if song else '—'}",
            parse_mode=ParseMode.HTML, reply_markup=kb_now_playing(chat_id),
        )

    # /resume ─────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("resume"))
    async def cmd_resume(client: Client, message: Message) -> None:
        _track(message.chat.id)
        chat_id = message.chat.id
        if not voice.is_connected(chat_id):
            await message.reply("❌ Nothing is playing.")
            return
        if chat_id not in q_state.paused_chats:
            await message.reply("⚠️ Not paused right now.")
            return
        voice.resume(chat_id)
        song = q_state.current_song(chat_id)
        await message.reply(
            f"▶️  <b>Resumed</b>\n\n🎵  {html.escape(song['title']) if song else '—'}",
            parse_mode=ParseMode.HTML, reply_markup=kb_now_playing(chat_id),
        )

    # /skip ───────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("skip"))
    async def cmd_skip(client: Client, message: Message) -> None:
        _track(message.chat.id)
        chat_id = message.chat.id
        q = q_state.get_queue(chat_id)
        if not q:
            await message.reply("📭 Queue is empty — nothing to skip.")
            return
        skipped = q[0].get("title", "—")
        await advance_queue(chat_id)
        nxt  = q_state.current_song(chat_id)
        text = (
            f"⏭️  <b>Skipped</b>  ›  {html.escape(skipped)}\n\n"
            + (
                f"▶️  Now playing:  <b>{html.escape(nxt['title'])}</b>"
                if nxt else "📭  Queue is now empty."
            )
        )
        await message.reply(text, parse_mode=ParseMode.HTML, reply_markup=kb_now_playing(chat_id))

    # /stop — open to everyone ─────────────────────────────────────────────────

    @app.on_message(filters.command("stop"))
    async def cmd_stop(client: Client, message: Message) -> None:
        _track(message.chat.id)
        chat_id = message.chat.id
        q_state.clear_queue(chat_id)
        await voice.leave_call(chat_id)
        await message.reply(
            "⏹️  <b>Stopped</b>\n\nQueue cleared and left the voice chat.",
            parse_mode=ParseMode.HTML, reply_markup=kb_back_main(),
        )

    # /playlist ───────────────────────────────────────────────────────────────

    @app.on_message(filters.command("playlist"))
    async def cmd_playlist(client: Client, message: Message) -> None:
        _track(message.chat.id)
        uid = message.from_user.id if message.from_user else message.chat.id
        await message.reply(
            text_playlist_page(uid, 0), parse_mode=ParseMode.HTML,
            reply_markup=kb_playlist(uid, 0),
        )

    # /addsong ────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("addsong"))
    async def cmd_addsong(client: Client, message: Message) -> None:
        _track(message.chat.id)
        uid = message.from_user.id if message.from_user else message.chat.id
        if not message.command[1:]:
            await message.reply(
                "💡 <b>Usage:</b>  /addsong &lt;song name&gt;",
                parse_mode=ParseMode.HTML,
            )
            return
        name  = " ".join(message.command[1:])
        added = q_state.add_to_playlist(uid, {"title": name, "url": ""})
        if not added:
            await message.reply(f"⚠️ Already saved:  {html.escape(name)}", parse_mode=ParseMode.HTML)
        else:
            pl = q_state.get_playlist(uid)
            await message.reply(
                f"✅ <b>Saved to playlist</b>\n\n🎵  {html.escape(name)}\n📋  Total: {len(pl)} songs",
                parse_mode=ParseMode.HTML,
            )

    # /removesong ─────────────────────────────────────────────────────────────

    @app.on_message(filters.command("removesong"))
    async def cmd_removesong(client: Client, message: Message) -> None:
        _track(message.chat.id)
        uid = message.from_user.id if message.from_user else message.chat.id
        if not message.command[1:]:
            await message.reply(
                "💡 <b>Usage:</b>  /removesong &lt;song name&gt;",
                parse_mode=ParseMode.HTML,
            )
            return
        name    = " ".join(message.command[1:])
        removed = q_state.remove_from_playlist(uid, name)
        if removed:
            await message.reply(f"🗑️ <b>Removed:</b>  {html.escape(name)}", parse_mode=ParseMode.HTML)
        else:
            await message.reply(f"❌ <b>Not found:</b>  {html.escape(name)}", parse_mode=ParseMode.HTML)

    # /admin ──────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("admin"))
    async def cmd_admin(client: Client, message: Message) -> None:
        _track(message.chat.id)
        if not await _is_admin(client, message):
            await message.reply(
                "🚫 <b>Admin Panel</b> is for group administrators only.",
                parse_mode=ParseMode.HTML,
            )
            return
        await message.reply(
            text_admin_menu(), parse_mode=ParseMode.HTML, reply_markup=kb_admin(),
        )

    # /settings ───────────────────────────────────────────────────────────────

    @app.on_message(filters.command("settings"))
    async def cmd_settings(client: Client, message: Message) -> None:
        _track(message.chat.id)
        await message.reply(
            text_settings(), parse_mode=ParseMode.HTML, reply_markup=kb_settings(),
        )

    # ── Callback query router ─────────────────────────────────────────────────

    @app.on_callback_query()
    async def cb_handler(client: Client, query: CallbackQuery) -> None:
        data    = query.data or ""
        chat_id = query.message.chat.id
        uid     = query.from_user.id if query.from_user else 0

        await query.answer()

        # Navigation ──────────────────────────────────────────────────────────

        if data == "menu_main":
            me    = await client.get_me()
            fname = query.from_user.first_name if query.from_user else "there"
            await _edit(query, text_welcome(fname), kb_main(me.username))

        elif data == "menu_music":
            await _edit(query, text_music_menu(), kb_music(chat_id))

        elif data.startswith("menu_playlist_"):
            try:
                page = int(data.rsplit("_", 1)[-1])
            except ValueError:
                page = 0
            await _edit(query, text_playlist_page(uid, page), kb_playlist(uid, page))

        elif data == "menu_settings":
            await _edit(query, text_settings(), kb_settings())

        elif data == "menu_admin":
            try:
                chat = await client.get_chat(chat_id)
                is_private = chat.type == ChatType.PRIVATE
            except Exception:
                is_private = False
            if not is_private and not await _is_admin_by_id(client, chat_id, uid):
                await query.answer("🚫 Group admins only.", show_alert=True)
                return
            await _edit(query, text_admin_menu(), kb_admin())

        elif data == "menu_about":
            await _edit(query, text_about(), kb_back_main())

        elif data == "menu_stats":
            await _edit(query, text_stats(), kb_back_main())

        # Playback ────────────────────────────────────────────────────────────

        elif data == "act_nowplaying":
            await _edit_now_playing(query, chat_id)

        elif data == "act_queue":
            await _edit(query, text_queue(chat_id), kb_back_music(chat_id))

        elif data == "act_pause":
            if not voice.is_connected(chat_id):
                await query.answer("Nothing is playing.", show_alert=True)
                return
            if chat_id in q_state.paused_chats:
                await query.answer("Already paused.", show_alert=True)
                return
            voice.pause(chat_id)
            await _edit_now_playing(query, chat_id)

        elif data == "act_resume":
            if not voice.is_connected(chat_id):
                await query.answer("Nothing is playing.", show_alert=True)
                return
            if chat_id not in q_state.paused_chats:
                await query.answer("Not paused.", show_alert=True)
                return
            voice.resume(chat_id)
            await _edit_now_playing(query, chat_id)

        elif data == "act_skip":
            q = q_state.get_queue(chat_id)
            if not q:
                await query.answer("Queue is empty.", show_alert=True)
                return
            skipped = q[0].get("title", "—")
            await advance_queue(chat_id)
            await _edit(
                query,
                f"⏭️  <b>Skipped</b>  ›  {html.escape(skipped)}\n\n"
                + _now_playing_caption(chat_id),
                kb_now_playing(chat_id),
            )

        elif data == "act_stop":
            # Open to everyone — no admin check
            q_state.clear_queue(chat_id)
            await voice.leave_call(chat_id)
            await _edit(
                query,
                "⏹️  <b>Stopped</b>\n\nQueue cleared and left the voice chat.",
                kb_back_main(),
            )

        # Admin panel ─────────────────────────────────────────────────────────

        elif data == "admin_stats":
            if not await _require_callback_admin(client, query, chat_id, uid):
                return
            await _edit(query, text_stats(), kb_back_admin())

        elif data == "admin_clearqueue":
            if not await _require_callback_admin(client, query, chat_id, uid):
                return
            count = q_state.clear_queue(chat_id)
            await voice.leave_call(chat_id)
            await _edit(
                query,
                f"🗑️  <b>Queue Cleared</b>\n\n{count} song(s) removed.",
                kb_back_admin(),
            )

        elif data == "admin_pl_overview":
            if not await _require_callback_admin(client, query, chat_id, uid):
                return
            total = q_state.playlist_song_count()
            await _edit(
                query,
                f"📋  <b>Playlist Overview</b>\n\n"
                f"  👥  Users with playlists:  <b>{len(q_state.playlists)}</b>\n"
                f"  🎵  Total saved songs:     <b>{total}</b>",
                kb_back_admin(),
            )

        elif data == "admin_chats":
            if not await _require_callback_admin(client, query, chat_id, uid):
                return
            await _edit(
                query,
                f"👥  <b>Active Chats</b>\n\n"
                f"  Total sessions:    <b>{len(_active_chats)}</b>\n"
                f"  Live voice calls:  <b>{voice.active_call_count()}</b>\n"
                f"  Downloading:       <b>{len(q_state.downloading)}</b>",
                kb_back_admin(),
            )

        elif data == "admin_leavevc":
            if not await _require_callback_admin(client, query, chat_id, uid):
                return
            await voice.leave_call(chat_id)
            await _edit(
                query,
                "🔊  <b>Left Voice Chat</b>\n\nThe assistant has left the voice chat.",
                kb_back_admin(),
            )

        # Hints & misc ────────────────────────────────────────────────────────

        elif data == "act_addsong_hint":
            await query.answer("Send /addsong <song name> in the chat.", show_alert=True)

        elif data == "act_remove_hint":
            await query.answer("Send /removesong <song name> in the chat.", show_alert=True)

        elif data == "noop":
            pass

        else:
            await query.answer(f"Unknown action: {data}", show_alert=True)
