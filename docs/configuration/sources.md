# Sources and downloads

**Use the Telegram control panel for routine source management.** Send
`/panel` to add, enable, disable, or remove YouTube and Twitch sources, set the
filter, or switch Twitch live/VOD mode. The same bot accepts
`/origin rename <origin_id> <name>` and `/origin history <origin_id>` for
renaming and history backfill.

Those actions atomically update `sources.toml` and then synchronize its SQLite
runtime mirror. They do not rewrite `config.toml`: that file holds global
service, download, Telegram, Twitch credential-reference, and panel-access
settings. SQLite keeps polling cursors, jobs, errors, and media records; it is
not a second user-editable source configuration.

## The source catalog

`config.toml` only points to the catalog:

```toml
[sources]
path = "sources.toml"
```

A relative path is resolved beside `config.toml`; the
`ASMR_TG_BACKUP_SOURCES_PATH` environment variable overrides it.
`asmr-tg-backup setup` creates a private catalog automatically. Compose stores
it on the host as `./settings/sources.toml`.

The catalog contains the filter and every source:

```toml
version = 1
source_filter = "ASMR"

[[origins]]
id = "youtube-example"
provider = "youtube"
kind = "uploads"
name = "Example YouTube channel"
external_id = "UC_CHANNEL_ID"
enabled = true
bootstrap = "latest"

[[origins]]
id = "twitch-example"
provider = "twitch"
kind = "vods"
name = "Example Twitch broadcaster"
external_id = "broadcaster_login"
enabled = true
bootstrap = "latest"
recording_mode = "vod"
```

`source_filter` is a case-insensitive regular expression matched against the
source id, source name, and media title; an empty string accepts everything.
Every `id` must be unique. A remote identity normally uses `provider`, `kind`,
and `external_id`; Twitch `vods` also includes
`recording_mode`, so one broadcaster may deliberately have one `live` and one
`vod` source, but not two exact duplicates.

## Manual tuning and CLI

The panel covers common changes. Edit the catalog directly for every field or
for batch changes:

```bash
asmr-tg-backup sources path
asmr-tg-backup sources export --output sources.backup.toml
# Edit sources.toml.
asmr-tg-backup sources validate
asmr-tg-backup sources apply
asmr-tg-backup sources list
```

Append `--config /absolute/path/config.toml` when using a non-default config.
You can also prepare another file and atomically replace the active catalog:

```bash
asmr-tg-backup sources validate --file ./candidate.toml
asmr-tg-backup sources apply --file ./candidate.toml
```

`apply` updates the SQLite mirror in one transaction. A source removed from the
catalog is removed from the pollable source set while existing media and job
history are retained. For an upgraded installation, run
`asmr-tg-backup sources migrate` once to create the catalog from existing
SQLite sources or legacy source declarations.

Legacy `[[origins]]`, `[[channels]]`, and `[[feeds]]` declarations participate
only while `sources.toml` is missing. During that one-time migration, current
legacy declarations replace stale config-managed SQLite rows and are safely
merged with existing Panel/catalog rows; an id or remote-identity conflict
stops migration instead of choosing an implicit priority. Once `sources.toml`
exists, legacy declarations are ignored with a warning. Review the catalog and
remove them from `config.toml`; runtime source changes then have one authority.

Common fields:

| Field | Meaning |
| --- | --- |
| `id` | Stable local identifier; it need not change when the display name changes |
| `provider` | `youtube` or `twitch` |
| `kind` | YouTube uses `uploads`; Twitch supports `vods`, `highlights`, and `uploads` |
| `name` | Display name in the panel and status output |
| `external_id` | YouTube `UC...` channel ID, or Twitch login/numeric broadcaster ID |
| `enabled` | Whether the source is polled |
| `bootstrap` | `latest` starts at the newest matching item; `all` requests a history backfill |
| `recording_mode` | Twitch `vods` only: `vod` waits for an archive; `live` records during the stream |

Provider-specific flat TOML fields are preserved as source options. This is the
manual path for fields not exposed by the panel. Nested tables and nested
arrays are rejected so Panel/CLI rewrites remain deterministic.

Changing an existing source from `latest` to `all` is an explicit backfill
request. Changing it back does not cancel jobs already created.

## Twitch credentials {#twitch-credentials}

Every Twitch source needs `TWITCH_CLIENT_ID` plus either
`TWITCH_CLIENT_SECRET` or an existing `TWITCH_ACCESS_TOKEN`.

1. Sign in to the [Twitch Developer Console](https://dev.twitch.tv/console/apps){ target="_blank" rel="noopener noreferrer" }
   with an account that has verified email and two-factor authentication.
2. Select **Register Your Application** and follow the
   [official registration guide](https://dev.twitch.tv/docs/authentication/register-app/){ target="_blank" rel="noopener noreferrer" }.
   Use a unique name. `http://localhost:3000` is sufficient for the required
   OAuth Redirect URL because this project uses a server credential flow.
3. Open **Manage**, copy the Client ID, and select **New Secret**.

For a native/PyPI installation, put the values in
`~/.config/asmr-tg-backup/env`:

```dotenv
TWITCH_CLIENT_ID=replace-with-client-id
TWITCH_CLIENT_SECRET=replace-with-client-secret
```

Run `chmod 600 ~/.config/asmr-tg-backup/env` and restart the user service. For
Compose, put the same values in the deployment `.env` and recreate the
container.

The application obtains and refreshes an app access token through Twitch's
[client credentials flow](https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/#client-credentials-grant-flow){ target="_blank" rel="noopener noreferrer" };
there is no Twitch user-login step. A newly added Twitch source remains disabled
when credentials are missing. Configure them, restart, and then enable it from
the panel.

## Download profiles

The `[download]` table in `config.toml` controls yt-dlp and ffmpeg.
Provider-specific tables such as `[download.provider_profiles.twitch]` override
format and audio extraction for one provider. The default keeps M4A audio. If a
profile keeps video while Telegram sends audio, the service creates a separate
delivery derivative without replacing the video master.

Downloaded masters, thumbnails, delivery derivatives, and live segments remain
under the application data directory. See [Operate](../operations.md) before
changing that directory or deleting files.
