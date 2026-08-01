# MusicBot v5.0.0

A Telegram music bot built with **Pyrogram** + **PyTgCalls** that streams audio
directly into Telegram group voice chats.

## What's New in v5.0.0

- **No more yt-dlp** — replaced with JioSaavn + Spotify catalog
- **Modular architecture** — split into focused packages
- **Richer metadata** — Spotify enrichment when credentials are configured

## Architecture

```
search/
  saavn_metadata.py   — JioSaavn catalog search (metadata + stream URL)
  spotify_metadata.py — Spotify metadata enrichment (optional, no stream URL)
stream/
  queue.py            — In-memory queue & playlist state
  voice.py            — PyTgCalls GroupCallFile voice engine
bot/
  player.py           — search_catalog() → get_authorized_stream() orchestration
  commands.py         — All Pyrogram command & callback handlers
  bot.py              — Entry point: Pyrogram clients, startup
  gen_session.py      — Helper to generate a Pyrogram userbot session string
```

## Play Flow

```
/play <query>
    ↓
search_catalog(query)
    ├─ search_spotify(query)   → metadata (title, artist, album art)    [optional]
    └─ search_saavn(query)     → metadata + direct CDN stream URL
    ↓
get_authorized_stream(track)   → confirmed CDN stream URL
    ↓
voice.stream_song(chat_id, track)
    ├─ ffmpeg: stream_url → s16le 48kHz mono PCM → /tmp/musicbot/
    └─ GroupCallFile.start(chat_id) → PyTgCalls streams raw PCM into voice chat
    ↓
Now Playing card with pause / skip / stop controls
```

## Commands

| Category | Command | Description |
|----------|---------|-------------|
| Navigation | `/start` `/menu` `/help` `/ping` `/stats` `/id` `/whoami` `/settings` | Bot info and menus |
| Music | `/play <song>` `/nowplaying` `/queue` `/lyrics` `/pause` `/resume` `/skip` `/stop` | Playback control |
| Playlist | `/playlist` `/addsong <name>` `/removesong <name>` | Personal playlists |
| Admin | `/admin` | Admin panel (group admins only) |

## Environment Variables (Replit Secrets)

| Secret | Required | Description |
|--------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | Bot token from @BotFather |
| `API_ID` | ✅ | Telegram API ID from my.telegram.org |
| `API_HASH` | ✅ | Telegram API Hash from my.telegram.org |
| `SESSION_SECRET` | ✅ for voice | Pyrogram session string for the dedicated @Flaxcopy assistant |
| `ASSISTANT_API_ID` | Optional | Separate Telegram API ID for @Flaxcopy; defaults to `API_ID` |
| `ASSISTANT_API_HASH` | Optional | Separate Telegram API hash for @Flaxcopy; defaults to `API_HASH` |
| `SPOTIFY_CLIENT_ID` | Optional | Spotify app client ID (metadata enrichment) |
| `SPOTIFY_CLIENT_SECRET` | Optional | Spotify app secret (metadata enrichment) |

## Generating a Userbot Session

To use a dedicated assistant account (@Flaxcopy) instead of the bot account for
voice streaming, generate a Pyrogram session string:

```bash
.pythonlibs/bin/python bot/gen_session.py
```

When prompted, log in with the dedicated Telegram account whose username is
`@Flaxcopy`. Copy the printed session string and save the entire value as
`SESSION_SECRET` in Replit Secrets, then restart the `Telegram Bot` workflow.

## Support

- Support: https://t.me/copytry
- Updates: https://t.me/copymusicofficial
