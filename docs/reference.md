# Reference

## Commands

| Command | Purpose |
| --- | --- |
| `asmr-tg-backup setup` | Choose MTProto or an advanced Bot API path, create private configuration and the source catalog, and initialize SQLite |
| `asmr-tg-backup service install [--config PATH]` | Generate, enable, and start the systemd user service; enable user linger for boot startup |
| `asmr-tg-backup service uninstall` | Stop and remove the generated unit while keeping all application data |
| `asmr-tg-backup init-config --output PATH` | Copy the packaged safe example without overwriting an existing file |
| `asmr-tg-backup init --config PATH` | Initialize directories and SQLite |
| `asmr-tg-backup run --config PATH` | Run continuous polling, workers, delivery, and control |
| `asmr-tg-backup poll --config PATH` | Run one discovery cycle and process work |
| `asmr-tg-backup poll --no-process --config PATH` | Discover and enqueue only |
| `asmr-tg-backup process --config PATH` | Process queued work without discovery |
| `asmr-tg-backup status --config PATH` | Print queue and recent-item status |
| `asmr-tg-backup enqueue URL --config PATH` | Queue one YouTube URL manually |
| `asmr-tg-backup sources path --config PATH` | Show the canonical catalog path |
| `asmr-tg-backup sources list --config PATH` | Show the filter and every configured source field |
| `asmr-tg-backup sources validate [--file PATH] --config PATH` | Validate the canonical catalog or another file without applying it |
| `asmr-tg-backup sources apply [--file PATH] --config PATH` | Reconcile the canonical catalog, or atomically replace it from another valid file |
| `asmr-tg-backup sources export --output PATH [--config PATH]` | Export a private catalog snapshot |
| `asmr-tg-backup sources migrate --config PATH` | Create the unified catalog from legacy TOML/SQLite sources |
| `asmr-tg-backup extensions list [--config PATH]` | List trusted, installed, and enabled extensions without importing them |
| `asmr-tg-backup extensions enable NAME [--config PATH]` | Install, minimally configure, validate, enable, and safely restart one trusted extension |
| `asmr-tg-backup extensions doctor [--config PATH]` | Validate the composed runtime for all enabled extensions |

### Guided setup choices

| Choice shown by setup | Result |
| --- | --- |
| MTProto direct upload | Ready to use in an official installation; source setup asks for your own application ID/hash |
| Existing trusted API URL | Assumes a large-file endpoint and generates a 1.99 GB single-file limit with splitting disabled; edit the generated limit for other endpoints |
| Local `telegram-bot-api` user service | Registers a preinstalled executable at `127.0.0.1:18081` |
| `api.telegram.org` with audio parts | Uses a 49 MB safety limit and playable audio splitting |

## Native paths

XDG variables replace the corresponding default roots.

`<config-stem>` below means the main configuration filename without its final
`.toml`.

| Resource | XDG path | Default |
| --- | --- | --- |
| Setup config | `$XDG_CONFIG_HOME/asmr-tg-backup/config.toml` | `~/.config/asmr-tg-backup/config.toml` |
| Managed extension state | Beside `<config-stem>.toml` as `<config-stem>.extensions.toml` | `~/.config/asmr-tg-backup/config.extensions.toml` |
| Private extension configs | Beside the main config as `extensions/<config-stem>/<filename>` | `~/.config/asmr-tg-backup/extensions/config/<filename>` |
| Source catalog | beside the setup config by default | `~/.config/asmr-tg-backup/sources.toml` |
| Worker environment | next to the setup config as `env` | `~/.config/asmr-tg-backup/env` |
| Worker unit | `$XDG_CONFIG_HOME/systemd/user/asmr-tg-backup.service` | `~/.config/systemd/user/asmr-tg-backup.service` |
| Application data | `$XDG_DATA_HOME/asmr-tg-backup` | `~/.local/share/asmr-tg-backup` |
| Database | below application data | `~/.local/share/asmr-tg-backup/state.db` |
| Downloads | below application data | `~/.local/share/asmr-tg-backup/downloads` |
| MTProto session | configured below application data | `~/.local/share/asmr-tg-backup/telegram-mtproto.session` |
| Local API env | `$XDG_CONFIG_HOME/asmr-tg-backup/telegram-bot-api.env` | `~/.config/asmr-tg-backup/telegram-bot-api.env` |
| Local API unit | `$XDG_CONFIG_HOME/systemd/user/asmr-tg-backup-telegram-bot-api.service` | `~/.config/systemd/user/asmr-tg-backup-telegram-bot-api.service` |
| Local API data | `$XDG_DATA_HOME/asmr-tg-backup/telegram-bot-api` | `~/.local/share/asmr-tg-backup/telegram-bot-api` |

Docker sets `ASMR_TG_BACKUP_DATA_DIR=/data`, mounts the writable host directory
`./settings` at `/settings`, and uses `/settings/sources.toml` as the catalog.
Mount the directory rather than only the file so atomic catalog replacement can
succeed. The named `asmr-data` volume holds the database, downloads, and
MTProto session.

## Configuration sections

| Section | Purpose |
| --- | --- |
| `[app]` | Data path, polling, retry, leases, worker count, logging |
| `[sources]` | Points to the unified `sources.toml` catalog used by both panel and CLI |
| `[extensions]` | Enabled extension entry-point IDs |
| `[extensions."id"]` | Required flag, private config path, and inline extension options |
| `[download]` | yt-dlp, ffmpeg, formats, paths, timeout, sidecars |
| `[download.provider_profiles.*]` | Per-provider download overrides |
| `[storage]` | Temporary-file cleanup, complete-backup retention, and mounted storage |
| `[telegram]` | Enablement, token, destination, transport, media, caption |
| `[telegram.mtproto]` | Application pair, session path, MTProto size limit |
| `[telegram.bot_api]` | Endpoint, Bot API size limit, playable splitting |
| `[control]` | Telegram panel endpoint, permissions, and polling |
| `[twitch]` | Helix credentials and VOD/live behavior |
| `[live]` | Provider-neutral live polling, retry, worker count, and recording timeout |

New setup configs write `[storage].process_retention_hours = 24`,
`[storage].backup_retention_hours = 0`, and an empty `archive_dir`. See
[Automatic local retention](configuration/sources.md#automatic-local-retention)
for the available storage settings.

`config.toml` is process configuration. Source rows and the global source
filter live in `sources.toml`; Panel changes therefore do not rewrite
`config.toml`. Edit global settings and restart the process, or edit the source
catalog and run `sources validate` followed by `sources apply`.

## Environment variables

| Variable | Overrides or controls |
| --- | --- |
| `ASMR_TG_BACKUP_DATA_DIR` | `[app].data_dir` |
| `ASMR_TG_BACKUP_SOURCES_PATH` | `[sources].path` |
| `TELEGRAM_BOT_TOKEN` | `telegram.bot_token` |
| `TELEGRAM_CHAT_ID` | `telegram.chat_id` |
| `ASMR_TG_UPLOAD_TRANSPORT` | `telegram.upload_transport` |
| `ASMR_TG_MTPROTO_API_ID` | `telegram.mtproto.api_id` |
| `ASMR_TG_MTPROTO_API_HASH` | `telegram.mtproto.api_hash` |
| `TELEGRAM_API_BASE` | `telegram.bot_api.api_base`; inherited by control unless `control.api_base` is set |
| `TELEGRAM_MAX_UPLOAD_BYTES` | `telegram.bot_api.max_upload_bytes` |
| `TWITCH_CLIENT_ID` | Twitch client ID |
| `TWITCH_ACCESS_TOKEN` | Existing Twitch app access token |
| `TWITCH_CLIENT_SECRET` | Twitch app-token creation and refresh |

The two MTProto variables must be present together. Official-package users can
normally leave both unset. A source build needs its own complete pair whenever
MTProto is selected; a runtime pair overrides a pair in private TOML.

## Delivery flow

```text
discover -> queue -> download -> prepare media
  -> selected transport prepares/uploads
  -> Telegram accepts the message or media group
  -> store Telegram message IDs
```

Media uses MTProto or Bot API according to `upload_transport`. Bot API audio
splitting runs only when that transport is selected and its configured byte
limit is exceeded. Ambiguous send results become `uncertain`; they are not sent
again through a different transport.

The control panel continues to use Bot API independently of the media transport.

## Security boundaries

- Keep the main configuration, managed extension state, private extension
  settings, source catalog, environment files, SQLite, and `.session` files
  private.
- Never put the bot token or session into a package, image, issue, or log.
- Use a complete MTProto application pair from one source; never mix halves.
- Use HTTPS for remote Bot API endpoints reached over an untrusted network;
  loopback and controlled private Compose networks may use HTTP.
- Bind local Bot API and statistics endpoints only to trusted interfaces.
- Keep media-egress proxies separate from loopback Telegram API traffic.
- Back up a session only into storage with the same protection as credentials.
