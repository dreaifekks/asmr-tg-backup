# asmr-tg-backup

`asmr-tg-backup` discovers public YouTube, Twitch, and RSS media, archives it
with `yt-dlp`, and can deliver the resulting media to Telegram. It runs as one
long-lived process with SQLite-backed discovery, download, delivery, and
control-panel state.

The application uses the Telegram API. Telegram delivery always uses your bot
token and destination; the token is never part of a package or image.

## Choose an installation

| Goal | Start here |
| --- | --- |
| Small native Linux service with guided setup | [PyPI and native Linux](getting-started/pypi.md) |
| Reproducible container with persistent `/data` | [Docker Compose](getting-started/docker-compose.md) |
| Understand all configuration fields | [Reference](reference.md) |

Official PyPI and GHCR releases are ready to use the default MTProto media
transport after you supply the bot token and destination. A source checkout can
use MTProto with its own Telegram application ID/hash, supplied as one complete
pair.

## Telegram delivery choices

Installation and upload transport are separate decisions:

- **MTProto direct upload** is the default. It sends the media without running
  a separate Bot API server and keeps a reusable session in the private data
  directory.
- **Existing Bot API URL** is available for users who already operate a trusted
  endpoint.
- **Local Bot API** is an advanced native-systemd or Compose option.
- **Official Bot API splitting** is the final fallback. Audio above the 49 MB
  safety threshold is converted into independently playable parts with a title
  and cover for every part.

See [Telegram delivery](configuration/telegram.md) before changing transport or
upload-size settings.

## Runtime requirements

- Python 3.11 or newer for a native installation;
- `ffmpeg` and `ffprobe` for audio extraction, thumbnails, splitting, and live
  recording;
- `curl` for the Bot API transport and Telegram control panel;
- a Telegram bot token and destination when delivery is enabled;
- Twitch credentials only when Twitch discovery is enabled.

The Compose image includes the operating-system media tools and `cryptg`
acceleration. Native installs must install system tools separately and can add
`cryptg` through the optional `performance` extra for faster MTProto encryption.

## Start safely

Keep sources and Telegram delivery disabled until configuration and database
initialization succeed. Then enable one source, verify one archive, and only
then enable delivery. Keep configuration, environment files, the SQLite
database, and the MTProto session private.
