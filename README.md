# ytb-tg-backup

Small background service for polling YouTube official channel feeds, downloading new videos as audio files with `yt-dlp`, and optionally uploading the archived files to a Telegram channel.

The normal config shape is a YouTube channel list. Each item expands to the official YouTube Atom feed:

```text
https://www.youtube.com/feeds/videos.xml?channel_id=UC...
```

```toml
[[channels]]
id = "some-asmr-channel"
name = "Some ASMR Channel"
channel_id = "UC..."
routes = ["live"]
enabled = true
```

`channel_id` must be the real `UC...` channel id for static TOML config. The control bot can accept `@handle` and resolves it to a `UC...` id with `yt-dlp` before saving. `routes` are kept as subscription intent/display labels; the official feed URL is the same for `live` and `channel`.

## First setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
cp config.example.toml config.toml
ytb-tg-backup init --config config.toml
ytb-tg-backup poll --config config.toml --once
```

## Development

```bash
python3 -m unittest discover -s tests
```

If the package is not installed in the active environment, run tests with:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

Keep real `config.toml` files out of git; they can contain Telegram bot tokens,
chat IDs, and host-specific paths. Generated data, local state, virtualenvs, and
Python build artifacts are covered by `.gitignore`.

Required host tools:

- `python3 >= 3.11`
- `yt-dlp`
- `curl`

`ffmpeg` is recommended for high-quality YouTube downloads that need separate video/audio merging. If it is not available, set `download.ffmpeg = ""` and use a single-file format such as `best[height<=720][ext=mp4]/best[height<=720]/best`.

For live streams, the default behavior is archive-after-VOD: while `yt-dlp` reports `is_live`, `is_upcoming`, or `post_live`, the item stays in `waiting_ready` and is retried later. The service does not try to record the live stream in real time.

## Control bot

The same Telegram bot can manage dynamic subscriptions from a restricted user/chat/topic. Dynamic subscriptions are stored in SQLite and are merged with any static `[[channels]]` in the TOML config.

```toml
[control]
enabled = true
poll_interval_seconds = 10
delete_webhook_on_startup = true
default_routes = ["live"]
allowed_user_ids = ["123456789"]
allowed_chat_ids = ["-1001234567890"]
allowed_message_thread_ids = ["42"]
```

The allowlists are OR-based: a command is allowed when the user id, chat id, or topic id matches any configured allowlist. If all three allowlists are empty, all commands are denied.

The control bot uses Telegram `getUpdates`; when `delete_webhook_on_startup = true`, startup calls `deleteWebhook` with `drop_pending_updates=false` so polling can work without discarding queued updates.

Commands:

```text
/sub add [live|channel] <@handle|channel_id> [name]
/sub del <id>
/sub list
/source_filter [regex|off|reset]
/stats
/start
/help
```

Examples:

```text
/sub add @nightmare
/sub add channel @nightmare Nightmare
/source_filter "ASMR|sleep"
/source_filter reset
/sub del live@nightmare
```

For `/sub add @handle`, the bot runs:

```bash
yt-dlp --print "%(channel_id)s" "https://www.youtube.com/@handle"
```

and stores the resolved `UC...` id for official feed polling.

Source filtering applies before polling feeds. The default is `/ASMR/i`, so only
sources whose feed id or name matches `ASMR` case-insensitively are polled until
the bot changes it. Use `/source_filter <regex>` to set a case-insensitive
regular expression, `/source_filter off` to poll every source, or
`/source_filter reset` to restore `/ASMR/i`.

Telegram captions are generated as:

```text
Video title

YouTube URL

#tag
```

When `download.write_thumbnail = true` and `ffmpeg` is available, downloaded YouTube thumbnails are converted to Telegram-compatible JPEG cover art and uploaded with the `sendAudio` message.

## Telegram upload choices

The default Telegram Bot API endpoint has small upload limits. For real video archiving, either:

- run a local Telegram Bot API server from `deploy/telegram-bot-api` and set `telegram.api_base`, or
- lower `download.format` so files fit your configured `telegram.max_upload_bytes`.

Uploads are disabled until `telegram.enabled = true` and a bot token/chat id are configured.

## User systemd service

```bash
mkdir -p ~/.config/systemd/user
cp deploy/ytb-tg-backup.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ytb-tg-backup.service
journalctl --user -u ytb-tg-backup.service -f
```

The service expects the repo at `~/dev/ytb-tg-backup` and config at `~/.config/ytb-tg-backup/config.toml`.
