# Telegram delivery

Delivery is enabled when a bot token, destination, and
`telegram.enabled = true` are present.

## Default MTProto configuration

```toml
[telegram]
enabled = true
bot_token = ""
chat_id = "@your_channel"
upload_transport = "mtproto"
media_type = "audio"
send_as_document = false
upload_timeout_seconds = 7200

[telegram.mtproto]
session_path = "telegram-mtproto.session"
max_upload_bytes = 1990000000

[telegram.bot_api]
api_base = "https://api.telegram.org"
max_upload_bytes = 49000000
split_large_audio = true
max_upload_parts = 10
```

A relative `session_path` is resolved below `[app].data_dir`; in Docker that is
`/data`. The session persists bot authorization and must remain private. Do not
share it, add it to an image, or commit it.

MTProto authenticates the bot with the same BotFather token used elsewhere in
the application. It does not sign in to a personal Telegram account and never
asks for a phone number, login code, or two-step verification password. Setup
does not contact Telegram; the first actual MTProto delivery creates and saves
the reusable bot session.

Official PyPI and GHCR releases can use the default MTProto path. A source build
must provide its own application pair either in the private configuration:

```toml
[telegram.mtproto]
api_id = 123456
api_hash = "0123456789abcdef0123456789abcdef"
```

or through both runtime variables:

```dotenv
ASMR_TG_MTPROTO_API_ID=123456
ASMR_TG_MTPROTO_API_HASH=0123456789abcdef0123456789abcdef
```

Only a complete pair is accepted. A complete runtime pair overrides a complete
pair in private TOML. Official-package users normally leave both locations
unset; source builds must configure one of them.

## Environment overrides

| Variable | Purpose | TOML fallback |
| --- | --- | --- |
| `ASMR_TG_BACKUP_DATA_DIR` | Database, downloads, and relative session root | `[app].data_dir` |
| `TELEGRAM_BOT_TOKEN` | BotFather token | `telegram.bot_token` |
| `TELEGRAM_CHAT_ID` | Destination chat or channel | `telegram.chat_id` |
| `ASMR_TG_UPLOAD_TRANSPORT` | `mtproto` or `bot_api` | `telegram.upload_transport` |
| `ASMR_TG_MTPROTO_API_ID` | MTProto application ID | `telegram.mtproto.api_id` |
| `ASMR_TG_MTPROTO_API_HASH` | MTProto application hash | `telegram.mtproto.api_hash` |
| `TELEGRAM_API_BASE` | Bot API endpoint | `telegram.bot_api.api_base` |
| `TELEGRAM_MAX_UPLOAD_BYTES` | Bot API per-file size limit | `telegram.bot_api.max_upload_bytes` |

Keep environment files at mode `0600`. The MTProto application pair is not the
bot token and does not replace it.

## Transport selection

`upload_transport` selects the media uploader:

- `mtproto` uploads through one persistent MTProto client and does not need a
  separate Bot API server;
- `bot_api` uses the configured HTTP Bot API endpoint and requires `curl` for
  media uploads.

The Telegram control panel continues to use a Bot API endpoint even when media
uses MTProto. It uses `[control].api_base` when set and otherwise inherits
`[telegram.bot_api].api_base`. The control panel has its own Python HTTP client
and does not require `curl`.

There is no automatic fallback sequence between MTProto, Bot API, and audio
splitting. A timeout after Telegram may have accepted a message is recorded as
uncertain to avoid duplicates. Switching transport is always an explicit
configuration decision.

## MTProto media uploads

The default 1,990,000,000-byte application limit leaves room below Telegram's
ordinary upload ceiling. MTProto uploads the media directly and preserves the
normal title, caption, and cover behavior without creating audio parts.

For a new `pipx` installation, include the optional extra from the start:

```bash
pipx install "asmr-tg-backup[performance]"
```

For an existing `pipx` installation, inject the accelerator into that managed
environment:

```bash
pipx inject asmr-tg-backup "cryptg>=0.5,<1"
```

Inside a virtual environment, install the extra with that environment's Python:

```bash
python -m pip install "asmr-tg-backup[performance]"
```

`cryptg` is only an optional encryption accelerator. It does not change
delivery semantics.

The uploader uses one process-wide client. Do not scale the application into
multiple processes sharing one SQLite state or one MTProto session.

## Bot API with an existing endpoint

```toml
[telegram]
upload_transport = "bot_api"

[telegram.bot_api]
api_base = "https://api.telegram.org"
max_upload_bytes = 49000000
split_large_audio = true
max_upload_parts = 10
```

Choosing **existing API URL** during setup configures a 1.99 GB limit and turns
off splitting for a large-file endpoint. For `api.telegram.org`, use a 49 MB
limit and enable audio splitting instead. Use HTTPS for a remote endpoint;
loopback and the Compose `local-api` network can use HTTP.

## Playable audio splitting

Splitting is a Bot API oversize policy, not a third transport or an automatic
fallback from MTProto. It runs only when `bot_api` is explicitly selected. When
an audio file exceeds `telegram.bot_api.max_upload_bytes` and splitting is
enabled, ffmpeg creates 2-10 independently playable M4A/MP3 parts and sends them
as one media group.

Every part gets:

- a distinct title ending in `Part i/n`;
- its own thumbnail attachment;
- a complete playable media container.

The caption is attached to the first item. If the file cannot fit within
`max_upload_parts`, delivery is blocked with an explicit oversize reason rather
than retried forever. Document and video delivery must fit the endpoint limit.

## Local Bot API

If `telegram-bot-api` is already installed on the host, native setup can
register it as a user service listening on `127.0.0.1:18081`. Compose can start
the same kind of service through the `local-api` profile at
`http://telegram-bot-api:8081`.

The server's `TELEGRAM_API_ID/HASH` variables are separate from the
application's `ASMR_TG_MTPROTO_API_ID/HASH`. Setup never downloads the C++
server and never performs Telegram's bot migration automatically. Follow the
[official migration procedure](https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server){ target="_blank" rel="noopener noreferrer" }
before moving a cloud-used token to a local server.

## Captions and media types

`caption_template` may use `{title}`, `{url}`, `{feed_name}` (source name),
`{video_id}`, and `{tag}`. `media_type` accepts `audio`, `video`, or `document`;
`send_as_document = true` forces document delivery.

An upload failure never removes the local archive master.
