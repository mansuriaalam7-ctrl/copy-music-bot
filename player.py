"""
Player — orchestrates the full play flow:

    Query  →  search_catalog()  →  track dict with stream_url
           →  get_authorized_stream()  →  confirmed stream URL
           →  stream.voice.stream_song()  →  PyTgCalls voice chat

Search strategy
---------------
YouTube via yt-dlp (``ytsearch:<query>``).
Returns track metadata and a direct audio stream URL suitable for ffmpeg.
"""

from __future__ import annotations

import logging
from typing import Optional

from search.youtube import search_youtube
from stream import queue as q_state
from stream import voice

logger = logging.getLogger("MusicBot.Player")


# ── Catalog search ────────────────────────────────────────────────────────────

async def search_catalog(query: str) -> Optional[dict]:
    """Search YouTube for *query*.

    Returns a track dict that includes a ``stream_url``, or ``None`` when
    nothing is found.
    """
    track = await search_youtube(query)
    if track is None:
        logger.error("search_catalog: no YouTube results for %r", query)
    return track


def get_authorized_stream(track: dict) -> Optional[str]:
    """Return the direct audio stream URL from a resolved track dict."""
    url = track.get("stream_url", "")
    if not url:
        logger.error(
            "get_authorized_stream: track %r has no stream_url",
            track.get("title"),
        )
        return None
    return url


# ── Queue advance ─────────────────────────────────────────────────────────────

async def advance_queue(chat_id: int) -> None:
    """Called by voice.py when a track finishes playing.

    Pops the finished song and starts the next one, or leaves the call.
    """
    q_state.dequeue(chat_id)
    q_state.paused_chats.discard(chat_id)

    next_song = q_state.current_song(chat_id)
    if next_song:
        await voice.stream_song(chat_id, next_song)
    else:
        await voice.leave_call(chat_id)
        logger.info("Queue finished for chat %d", chat_id)
