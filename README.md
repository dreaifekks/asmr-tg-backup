# asmr-tg-backup

[English documentation](https://dreaifekks.github.io/asmr-tg-backup/) ·
[简体中文文档](https://dreaifekks.github.io/asmr-tg-backup/zh/) ·
<a href="https://t.me/+9-Cy-yue1PJiMWY9" target="_blank" rel="noopener noreferrer">Telegram showcase</a>

`asmr-tg-backup` is an extensible background service for discovering ASMR media,
archiving it with `yt-dlp`, and optionally delivering the archived files to
Telegram. The core includes YouTube channel uploads plus Twitch VOD and live
recording; optional packages can add source providers and scoped network
routing without taking over durable job state.

## Highlights

- One long-running process with SQLite-backed discovery, download, delivery,
  and Telegram control-panel state.
- Direct MTProto media upload is the default for official PyPI and GHCR
  releases; no separate Telegram Bot API server is required.
- Twitch channels can download published VODs or begin recording while a stream
  is live.
- A typed extension host can add source providers or task-scoped connection
  routing; extensions are installed and enabled explicitly.
- Downloads and Telegram delivery are independent jobs, so an upload failure
  does not discard or repeat a completed download.
- The optional Telegram panel manages sources, filters, status, and tracked
  local resources, with source changes persisted in an editable TOML catalog.

## Quick start with PyPI

Native installations require Python 3.11 or newer. Install `ffmpeg` and
`ffprobe` for the default audio workflow and Twitch live recording. `curl`
is required only when the media transport is Bot API.

The recommended install includes `cryptg` for faster large-file encryption:

```bash
pipx install "asmr-tg-backup[performance]"
asmr-tg-backup --version
asmr-tg-backup setup
asmr-tg-backup service install
```

The guided setup defaults to MTProto and asks for:

1. a BotFather token;
2. the destination chat ID or `@channel`;
3. the Telegram user ID allowed to open the control panel.

Quick links:
<a href="https://t.me/BotFather" target="_blank" rel="noopener noreferrer">create the bot with BotFather</a>
·
<a href="https://t.me/userinfobot" target="_blank" rel="noopener noreferrer">find your numeric user ID</a>

This is a bot login, not a personal Telegram user login. The first actual
MTProto delivery signs the bot in non-interactively with its token and creates
a reusable local session.

The service command generates a unit for the current pipx/virtualenv path,
starts it, and enables boot-time user services. Remove only that unit later
with:

```bash
asmr-tg-backup service uninstall
```

Send `/panel` to the bot after the service starts. The panel is the recommended
way to add the first YouTube or Twitch source and change the source filter.

For a persistent native service, continue with the
[systemd user-service guide](https://dreaifekks.github.io/asmr-tg-backup/getting-started/pypi/#run-as-a-user-service).

## Quick start with Docker Compose

Clone or download the repository, then use its Compose files with the official
GHCR image and persistent `asmr-data` volume:

```bash
git clone https://github.com/dreaifekks/asmr-tg-backup.git
cd asmr-tg-backup
cp .env.example .env
cp config.example.toml config.toml
mkdir -p settings
cp sources.example.toml settings/sources.toml
chmod 700 settings
chmod 600 .env config.toml settings/sources.toml
id -u
id -g
```

Set `PUID` and `PGID` in `.env` to the two printed values, then set the bot token
and destination. Enable Telegram delivery in `config.toml` and start the
application:

```bash
docker compose pull asmr-tg-backup
docker compose up -d asmr-tg-backup
docker compose logs --tail=100 asmr-tg-backup
```

The example sources are disabled. Send `/panel` to add or enable a source after
the service starts.

See the
[Docker Compose guide](https://dreaifekks.github.io/asmr-tg-backup/getting-started/docker-compose/)
for UID/GID handling, source builds, upgrades, and the optional local Bot API
profile.

## Sources: panel first, file when needed

| Provider | Supported origin | Notes |
| --- | --- | --- |
| YouTube | Channel uploads | Uses a real `UC...` channel ID |
| Twitch | VODs, highlights, uploads, or live recording | Uses Twitch application settings |
| Extension | Registered kinds | Edit `sources.toml`; the core still schedules and stores every item |

Send `/panel` to add, enable, disable, or remove a source, switch Twitch mode,
and change the global source filter. The same bot accepts `/origin rename` and
`/origin history` for renaming and backfill requests. These changes are written
atomically to `sources.toml`; SQLite holds only the synchronized runtime mirror
plus cursors, jobs, and history. `config.toml` is reserved for global runtime
settings.

For batch changes or complete field control, edit the same catalog:

```toml
version = 1
source_filter = "ASMR"

[[origins]]
id = "youtube-example"
provider = "youtube"
kind = "uploads"
name = "Example channel"
external_id = "UC_CHANNEL_ID"
bootstrap = "latest"
enabled = true
```

`bootstrap = "latest"` starts with the newest matching item. Use `"all"` for a
history backfill. Validate and apply manual edits with:

```bash
asmr-tg-backup sources validate
asmr-tg-backup sources apply
```

On an upgrade, legacy `[[origins]]`, `[[channels]]`, or `[[feeds]]` declarations
are used only to create a missing `sources.toml`. Once the catalog exists they
are ignored with a startup warning, so review the migrated catalog and remove
the old declarations instead of maintaining two copies.

Twitch sources require a Client ID plus a Client Secret or app access token.
See [Sources and downloads](https://dreaifekks.github.io/asmr-tg-backup/configuration/sources/)
for the Twitch developer-console link, `vod`/`live` behavior, credentials, and
download profiles.

## Optional extensions

Extensions are ordinary Python packages discovered from the same environment as
the core. For the built-in trusted catalog, one command handles same-environment
installation, minimal private setup, enablement, validation, and a safe restart
of the matching managed user service:

```bash
asmr-tg-backup extensions enable proxy-router
asmr-tg-backup extensions enable niconico-origin
```

The command never rewrites `config.toml`; it maintains a private managed
sidecar beside it. Containers still install selected extensions at image build
time. Advanced and third-party extensions can be installed and configured
manually, then checked with `extensions list` and `extensions doctor`.

The first optional repositories using the 0.6 one-command setup layer are:

- [`asmr-tg-backup-ext-proxy-router`](https://github.com/dreaifekks/asmr-tg-backup-ext-proxy-router): independent routing for notification,
  discovery, probe, download, Telegram control, Bot API delivery, and MTProto
  scopes using HTTP/SOCKS endpoints or a Clash-compatible subscription through
  Mihomo;
- [`asmr-tg-backup-ext-niconico-origin`](https://github.com/dreaifekks/asmr-tg-backup-ext-niconico-origin): public Niconico live-search discovery,
  with the resulting live probe/download handled by the core.

See the [extension guide](https://dreaifekks.github.io/asmr-tg-backup/configuration/extensions/)
for installation, configuration, route boundaries, and the API contract.

## Telegram delivery

Official packages and images can use the default MTProto path after the local
bot token and destination are configured. Source builds need their own complete
Telegram application ID/hash pair. In every installation, the bot token and
MTProto session stay in the local runtime directories.

Source-build application settings are created from
<a href="https://my.telegram.org/apps" target="_blank" rel="noopener noreferrer">Telegram API development tools</a>.

Bot API can be used with:

- an existing Bot API URL;
- a preinstalled native `telegram-bot-api` service;
- the optional Compose `local-api` profile; or
- `api.telegram.org` with playable audio splitting for its smaller file limit.

Cloud Bot API audio above the configured 49 MB limit can be split into 2–10
independently playable parts. Each part receives a distinct `Part i/n` title
and its own cover. MTProto sends the file directly without splitting.

See [Telegram delivery](https://dreaifekks.github.io/asmr-tg-backup/configuration/telegram/)
for complete transport configuration.

## Telegram control panel

Send `/panel` or `/start` to manage sources, inspect status, change the source
filter, and browse tracked local resources. Access is configured with allowed
user, chat, and topic IDs.

Disk deletion is optional. When enabled, the panel manages downloaded files
tracked in SQLite while retaining database history and Telegram messages.

See [Control panel](https://dreaifekks.github.io/asmr-tg-backup/configuration/control-panel/)
for configuration and file-management behavior.

## State and files

Native setup stores runtime configuration below
`~/.config/asmr-tg-backup/` and application state below
`~/.local/share/asmr-tg-backup/`. Docker stores application state in
`/data` and its editable source catalog in `./settings/sources.toml`.

Back up `config.toml`, `sources.toml`, the environment file, SQLite database,
downloads, and MTProto session before upgrades. Run only one application
process against a given database/session pair.

Keep runtime configuration, bot tokens, Twitch settings, SQLite/WAL files,
downloads, and MTProto sessions in the local runtime directories rather than
source control.

## Architecture and development

The runtime flow is:

```text
Panel / CLI -> sources.toml -> SQLite source runtime mirror
Built-in / extension providers -> provider discovery -> SQLite media and jobs
  -> yt-dlp / ffmpeg artifacts
  -> MTProto or Bot API delivery
  -> Telegram message records
```

External contributors should start with the bilingual
[contribution guide](https://dreaifekks.github.io/asmr-tg-backup/contributing/)
and [architecture and development guide](https://dreaifekks.github.io/asmr-tg-backup/development/).

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[docs]"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  .venv/bin/python -m unittest discover -s tests
.venv/bin/mkdocs build --strict
```

## License

Apache License 2.0. See
[LICENSE](https://github.com/dreaifekks/asmr-tg-backup/blob/master/LICENSE).
