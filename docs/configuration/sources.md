# Sources and downloads

Sources tell the service where to discover media and how each origin should be
polled. Use the Telegram panel for day-to-day changes, and use `sources.toml`
when you need every field, an RSS feed, or a batch update.

## What you can manage

From `/panel`, you can:

- add YouTube channels and Twitch broadcasters;
- add a provider registered by an enabled extension, such as Niconico;
- enable, disable, inspect, or remove an existing source;
- choose live recording or archive download for Twitch VOD sources; and
- inspect, set, disable, or reset the global source filter.

An extension that registers a source provider adds its own `➕ Provider`
button after the extension is enabled and the service has restarted. The
button makes the provider available; select it and submit an identifier when
you want to create the source and begin polling.

RSS feeds are built in as manual catalog sources. Add them to `sources.toml`
and apply the catalog with the CLI.

## What to prepare

Keep these roles separate when editing or backing up the service:

- `sources.toml` is the user-editable source catalog and global source filter;
- `config.toml` holds service, download, Telegram, credential-reference,
  manually managed extension, and panel-access settings;
- one-command extension setup keeps managed state in
  `<config-stem>.extensions.toml` and private settings below
  `extensions/<config-stem>/`; and
- SQLite holds the synchronized runtime mirror plus polling cursors, jobs,
  errors, and media records.

Here `<config-stem>` means the main config filename without its final `.toml`.
The panel and CLI atomically update `sources.toml` and then synchronize SQLite.
Edit the catalog instead of editing source rows in SQLite.

Before adding a source, prepare the remote identifier it expects: a YouTube
handle or channel ID, a Twitch login or user ID, an extension-specific
identifier, or an RSS feed URL. Twitch also needs the credentials described in
[Twitch credentials](#twitch-credentials). Review the source filter after
adding an origin; a healthy source still skips items whose title, source name,
and source ID do not match it.

## Add a source from Telegram

1. Enable and configure the required extension first when the provider is not
   built in. For example:

   ```bash
   asmr-tg-backup extensions enable niconico-origin
   ```

2. Send `/panel`, then select `➕ YouTube`, `➕ Twitch`, or the provider button
   added by the extension.
3. Follow the prompt. Extension providers accept
   `<external_id> [display name]`; quote an identifier that contains spaces.
4. Open `📚 Sources` and confirm the new source, its enabled state, and its
   `provider/kind` value.

The same bot accepts `/origin rename <origin_id> <name>` and
`/origin history <origin_id>` for renaming and history backfill. See
[Extensions](extensions.md) for installation and provider-specific examples.

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

### Add an RSS feed manually {#rss-feed}

For an RSS source, `external_id` is the complete feed URL and `kind` is
`feed`:

```toml
[[origins]]
id = "rss-example"
provider = "rss"
kind = "feed"
name = "Example media feed"
external_id = "https://feeds.example.com/media.xml"
enabled = true
bootstrap = "latest"
allowed_media_hosts = ["media.example.com", "*.cdn.example.com"]
allow_private_media = false
```

Each RSS item or Atom entry uses its link as the media URL. That link must use
HTTP or HTTPS without embedded credentials. By default, media hosts must
resolve to public addresses.
`allowed_media_hosts` narrows downloads to the exact hosts and wildcard
subdomains you list; it is useful when a feed should only publish media from a
known site or CDN. Leave the list empty to accept any public media host.

Set `allow_private_media = true` only when the feed is expected to publish
media from a private or local network that this service is allowed to reach.
This setting permits non-public media addresses, while `allowed_media_hosts`
can still limit which host names are accepted. Run `sources validate` after
changing either field.

## Manual tuning and CLI

The panel covers common changes. Edit the catalog directly for every field or
for batch changes:

```bash
asmr-tg-backup sources path \
  --config ~/.config/asmr-tg-backup/config.toml
asmr-tg-backup sources export \
  --config ~/.config/asmr-tg-backup/config.toml \
  --output sources.backup.toml
# Edit sources.toml.
asmr-tg-backup sources validate \
  --config ~/.config/asmr-tg-backup/config.toml
asmr-tg-backup sources apply \
  --config ~/.config/asmr-tg-backup/config.toml
asmr-tg-backup sources list \
  --config ~/.config/asmr-tg-backup/config.toml
```

The CLI otherwise looks for `config.toml` in the current directory. Replace
the path above when using another main config. You can also prepare another
file and atomically replace the active catalog:

```bash
asmr-tg-backup sources validate \
  --config ~/.config/asmr-tg-backup/config.toml \
  --file ./candidate.toml
asmr-tg-backup sources apply \
  --config ~/.config/asmr-tg-backup/config.toml \
  --file ./candidate.toml
```

`apply` updates the SQLite mirror in one transaction. A source removed from the
catalog is removed from the pollable source set while existing media and job
history are retained. For an upgraded installation, run
`asmr-tg-backup sources migrate --config ~/.config/asmr-tg-backup/config.toml`
once to create the catalog from existing SQLite sources or legacy source
declarations.

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
| `provider` | Built-ins are `youtube`, `twitch`, and manual `rss`; enabled extensions may register additional values such as `niconico` |
| `kind` | YouTube uses `uploads` or `vod_after_live`; Twitch supports `vods`, `highlights`, and `uploads`; RSS uses `feed`; extensions define their own kinds |
| `name` | Display name in the panel and status output |
| `external_id` | Remote identifier: YouTube channel ID, Twitch broadcaster ID/login, RSS feed URL, or the value defined by an extension provider |
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
delivery derivative without replacing the complete video backup.

Complete backup files, thumbnails, delivery files, and live segments remain
under the application data directory. See [Operate](../operations.md) before
changing that directory.

## Automatic local retention {#automatic-local-retention}

New setup configs remove temporary recording and Telegram upload files one day
after a successful delivery. Complete backup files are kept:

```toml
[storage]
process_retention_hours = 24
backup_retention_hours = 0
archive_dir = ""
archive_after_delivery_hours = 24
archive_require_mount = true
```

`process_retention_hours` controls temporary files. `backup_retention_hours`
controls complete backup files. A value of `0` keeps that category
indefinitely.

To move complete backup files instead of keeping them below `downloads`, set
`archive_dir` to an existing directory on the mounted disk. The move happens
after `archive_after_delivery_hours`; keep `archive_require_mount = true` for
mounted storage. The directory may be a subdirectory of the mount.

SQLite records completed deliveries and the current location of each backup;
the media content remains as normal files in the configured storage. For
Docker, expose the mounted directory as a bind mount and use its container
path. Restart the service after changing these settings.
