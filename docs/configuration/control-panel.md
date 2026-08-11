# Telegram control panel

The Telegram control panel is the preferred source-management interface. Send
`/panel` or `/start` to manage sources and the filter, inspect runtime status,
and browse tracked local resources from one inline message.

## Enable the panel

```toml
[control]
enabled = true
api_base = ""
poll_interval_seconds = 10
panel_idle_timeout_seconds = 3600
allow_disk_delete = false
allowed_user_ids = ["123456789"]
allowed_chat_ids = []
allowed_message_thread_ids = []
```

Authorization is an AND across every non-empty allowlist. If both a user and a
chat are configured, both must match. Emptying every allowlist denies every
command. Prefer at least one allowed user ID; a chat-only allowlist permits all
members of that chat.

The panel closes after one idle hour by default. Set
`panel_idle_timeout_seconds = 0` to disable the timeout.

## Control-only Bot API endpoint

`control.api_base` selects the Bot API endpoint used for `getUpdates`, callback
acknowledgements, command registration, and panel message sends/edits. An empty
value, or omitting the field, inherits `telegram.bot_api.api_base` for backward
compatibility.

To keep MTProto media delivery unchanged while the panel uses a trusted local
Bot API server:

```toml
[telegram]
upload_transport = "mtproto"

[telegram.bot_api]
api_base = "https://api.telegram.org"

[control]
enabled = true
api_base = "http://127.0.0.1:18081"
```

The endpoint receives the bot token. Use HTTPS for remote servers; unencrypted
HTTP is appropriate only for a trusted loopback endpoint. Loopback requests are
always forced direct and cannot be captured by an extension or environment
proxy.

If `telegram.upload_transport = "bot_api"`, normally point
`telegram.bot_api.api_base` at the same local server as the panel. The override
does not make Telegram's cloud and local Bot API safe to use simultaneously for
one bot token.

Changing this field does not migrate the bot between Telegram's cloud Bot API
and a local Bot API server. Prepare the local server, stop the application,
complete the server's documented bot migration, update the field, and then
restart. Never run cloud and local `getUpdates` consumers for the same bot at
the same time.

## Source management

The panel buttons can:

- add, enable, disable, and remove YouTube or Twitch sources;
- choose live recording or archive download for a Twitch VOD source;
- inspect, set, disable, or reset the global source filter; and
- inspect source polling errors and media/job statistics.

Use `/origin rename <origin_id> <name>` to rename a source and
`/origin history <origin_id>` to change its bootstrap range from `latest` to
`all`. These commands use the same catalog path as the buttons.

Every source or filter change safely updates the `sources.toml` selected by
`[sources].path` and then synchronizes its SQLite runtime mirror. If you edit
the same file and run `asmr-tg-backup sources apply`, the panel shows the new
values. There is no precedence contest between “panel configuration” and “TOML
configuration.”

Use the [source catalog and CLI](sources.md) for batch edits, complete field
control, or configuration snapshots.

## What is stored where

| Data | Storage | How it changes |
| --- | --- | --- |
| Sources, enabled state, names, bootstrap range, Twitch mode, global filter | `sources.toml`; SQLite only holds the synchronized runtime mirror | Panel buttons or `/origin` commands; or edit the file and run `sources apply` |
| Telegram, download, Twitch credential references, panel endpoint and authorization | `config.toml` and optional `env` | Edit and restart the service |
| Poll cursors, errors, media, jobs, deliveries, file records | `state.db` | Maintained by the running service |
| Current panel message, navigation session, Telegram update offset | `state.db` | Maintained automatically by the panel |
| Downloads and MTProto session | Application data directory | Maintained by the service; explicit file deletion may use the panel |

Do not edit SQLite to change source configuration. Backups should include
`config.toml`, `sources.toml`, optional `env`, `state.db`, downloads, and the
MTProto session.

## Local resource library

The panel lists only files already tracked in SQLite. It does not recursively
scan the download directory or infer ownership from filenames.

Permanent disk deletion is opt-in in `config.toml`:

```toml
[control]
allow_disk_delete = true
```

Restart after changing this setting. Deletion still requires authorization, a
current panel session, and resource-specific confirmation. Only exact tracked
regular files below the download root are eligible; database history and
existing Telegram messages remain.
