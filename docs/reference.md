# Reference

## Commands

| Command | Purpose |
| --- | --- |
| `asmr-tg-backup setup` | Choose MTProto or an advanced Bot API path, create private configuration, and initialize SQLite |
| `asmr-tg-backup init-config --output PATH` | Copy the packaged safe example without overwriting an existing file |
| `asmr-tg-backup init --config PATH` | Initialize directories and SQLite |
| `asmr-tg-backup run --config PATH` | Run continuous polling, workers, delivery, and control |
| `asmr-tg-backup poll --config PATH` | Run one discovery cycle and process work |
| `asmr-tg-backup poll --no-process --config PATH` | Discover and enqueue only |
| `asmr-tg-backup process --config PATH` | Process queued work without discovery |
| `asmr-tg-backup status --config PATH` | Print queue and recent-item status |
| `asmr-tg-backup enqueue URL --config PATH` | Queue one YouTube URL manually |

### Setup profiles

| Profile | Result |
| --- | --- |
| `mtproto-official` | Default official-release MTProto configuration |
| `mtproto-own` | MTProto with a private application ID/hash entered during source-build setup |
| `custom-api-single` | Existing trusted Bot API URL with a large single-file profile |
| `local-api-single` | Preinstalled local Bot API registered at `127.0.0.1:18081` |
| `official-api-split` | `api.telegram.org`, 49 MB safety limit, playable audio splitting |

## Native paths

XDG variables replace the corresponding default roots.

| Resource | XDG path | Default |
| --- | --- | --- |
| Setup config | `$XDG_CONFIG_HOME/asmr-tg-backup/config.toml` | `~/.config/asmr-tg-backup/config.toml` |
| Application data | `$XDG_DATA_HOME/asmr-tg-backup` | `~/.local/share/asmr-tg-backup` |
| Database | below application data | `~/.local/share/asmr-tg-backup/state.db` |
| Downloads | below application data | `~/.local/share/asmr-tg-backup/downloads` |
| MTProto session | configured below application data | `~/.local/share/asmr-tg-backup/telegram-mtproto.session` |
| Local API env | `$XDG_CONFIG_HOME/asmr-tg-backup/telegram-bot-api.env` | `~/.config/asmr-tg-backup/telegram-bot-api.env` |
| Local API unit | `$XDG_CONFIG_HOME/systemd/user/asmr-tg-backup-telegram-bot-api.service` | `~/.config/systemd/user/asmr-tg-backup-telegram-bot-api.service` |
| Local API data | `$XDG_DATA_HOME/asmr-tg-backup/telegram-bot-api` | `~/.local/share/asmr-tg-backup/telegram-bot-api` |

Docker sets `ASMR_TG_BACKUP_DATA_DIR=/data`; the named `asmr-data` volume holds
the database, downloads, and MTProto session.

## Configuration sections

| Section | Purpose |
| --- | --- |
| `[app]` | Data path, polling, retry, leases, worker count, logging |
| `[[origins]]` | Provider-backed YouTube, Twitch, or RSS origins |
| `[download]` | yt-dlp, ffmpeg, formats, paths, timeout, sidecars |
| `[download.provider_profiles.*]` | Per-provider download overrides |
| `[telegram]` | Enablement, token, destination, transport, media, caption |
| `[telegram.mtproto]` | Application pair, session path, MTProto size limit |
| `[telegram.bot_api]` | Endpoint, Bot API size limit, playable splitting |
| `[control]` | Telegram panel permissions and polling |
| `[twitch]` | Helix credentials and VOD/live behavior |

## Environment variables

| Variable | Overrides or controls |
| --- | --- |
| `ASMR_TG_BACKUP_DATA_DIR` | `[app].data_dir` |
| `TELEGRAM_BOT_TOKEN` | `telegram.bot_token` |
| `TELEGRAM_CHAT_ID` | `telegram.chat_id` |
| `ASMR_TG_UPLOAD_TRANSPORT` | `telegram.upload_transport` |
| `ASMR_TG_MTPROTO_API_ID` | `telegram.mtproto.api_id` |
| `ASMR_TG_MTPROTO_API_HASH` | `telegram.mtproto.api_hash` |
| `TELEGRAM_API_BASE` | `telegram.bot_api.api_base` |
| `TELEGRAM_MAX_UPLOAD_BYTES` | `telegram.bot_api.max_upload_bytes` |
| `TWITCH_CLIENT_ID` | Twitch client ID |
| `TWITCH_ACCESS_TOKEN` | Existing Twitch app access token |
| `TWITCH_CLIENT_SECRET` | Twitch app-token creation and refresh |

The two MTProto variables must be present together. For an official release,
their complete runtime pair overrides the release defaults. A source build has
no such defaults and needs its own pair whenever MTProto is selected.

## Delivery flow

```text
discover -> queue -> download -> prepare media
  -> selected transport prepares/uploads
  -> send commit
  -> store Telegram message IDs
```

Media uses MTProto or Bot API according to `upload_transport`. Bot API audio
splitting runs only when that transport is selected and its configured byte
limit is exceeded. Ambiguous send results become `uncertain`; they are not sent
again through a different transport.

The control panel remains a Bot API consumer independently of the media
transport.

## Security boundaries

- Keep configuration, environment files, SQLite, and `.session` files private.
- Never put the bot token or session into a package, image, issue, or log.
- Use a complete MTProto application pair from one source; never mix halves.
- Use HTTPS for non-loopback Bot API endpoints.
- Bind local Bot API and statistics endpoints only to trusted interfaces.
- Keep media-egress proxies separate from loopback Telegram API traffic.
- Back up a session only into storage with the same protection as credentials.
