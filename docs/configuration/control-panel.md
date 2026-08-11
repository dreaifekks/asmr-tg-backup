# Telegram control panel

The Telegram control panel keeps routine source and local-resource operations
in one inline message. Send `/panel` or `/start` after it is enabled.

## What you can do

The panel lets an authorized user:

- add YouTube and Twitch sources;
- add providers registered by enabled extensions, such as Niconico;
- enable, disable, inspect, and remove sources;
- choose Twitch live recording or archive download;
- manage the global source filter and inspect polling or job status; and
- browse tracked local files, with optional confirmed disk deletion.

Enabling a source-provider extension adds its provider button after the service
restarts. Select that button and submit the requested identifier when you want
to create the source and begin polling.

## What to prepare

Choose the user, chat, and message-thread IDs that may operate the panel, then
add them to the matching allowlists:

```toml
[control]
enabled = true
api_base = ""
poll_interval_seconds = 10
panel_idle_timeout_seconds = 3600
delete_webhook_on_startup = true
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

The panel receives updates with Bot API long polling. Keep
`delete_webhook_on_startup = true` when the bot may still have a webhook; the
service removes that webhook at startup without dropping pending updates.

## Control-only Bot API endpoint

`control.api_base` selects the Bot API endpoint used for `getUpdates`, callback
acknowledgements, command registration, and panel message sends/edits. An empty
value, or omitting the field, uses the same endpoint as
`telegram.bot_api.api_base`.

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
`telegram.bot_api.api_base` at the same local server as the panel. Assign one
Bot API location to each bot token at a time.

To move a token between Telegram's cloud Bot API and a local server, prepare
the local server, stop the application, complete the server's documented bot
migration, update the field, and restart. Keep cloud and local `getUpdates`
consumers mutually exclusive throughout the move.

## Open the panel

1. Save the `[control]` settings and restart the service with the command for
   your [deployment method](../operations.md).
2. Send `/panel` or `/start` from an allowed user, chat, and message thread.
3. Use `🔄 Refresh` when you want a new runtime snapshot. The panel also
   refreshes after each completed action.
4. Send `/panel` again whenever you want a fresh panel message.

## Source management

The panel buttons can:

- add, enable, disable, and remove YouTube, Twitch, and enabled extension
  sources;
- choose live recording or archive download for a Twitch VOD source;
- inspect, set, disable, or reset the global source filter; and
- inspect source polling errors and media/job statistics.

Each enabled extension provider gets a generated `➕ Provider` button. Select
it and submit `<external_id> [display name]`; quote an identifier that contains
spaces. The new source appears in `📚 Sources` with its `provider/kind` after
the input is accepted. RSS remains a manual catalog source; see
[Sources and downloads](sources.md#rss-feed).

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

Here `<config-stem>` means the main config filename without its final `.toml`.

| Data | Storage | How it changes |
| --- | --- | --- |
| Sources, enabled state, names, bootstrap range, Twitch mode, global filter | `sources.toml`; SQLite only holds the synchronized runtime mirror | Panel buttons or `/origin` commands; or edit the file and run `sources apply` |
| Telegram, download, Twitch credential references, panel endpoint and authorization | `config.toml` and optional `env` | Edit and restart the service |
| Managed extension enablement, required flags, and private-config references | `<config-stem>.extensions.toml`; the default path is `config.extensions.toml` | `extensions enable` manages the sidecar; settings in the main config take precedence |
| Managed private extension settings | `extensions/<config-stem>/` beside the main config | Extension setup or `extensions enable --reconfigure`; validate with `extensions doctor` |
| Poll cursors, errors, media, jobs, deliveries, file records | `state.db` | Maintained by the running service |
| Current panel message, navigation session, Telegram update offset | `state.db` | Maintained automatically by the panel |
| Downloads and MTProto session | Application data directory | Maintained by the service; explicit file deletion may use the panel |

Edit `sources.toml` rather than SQLite when changing sources. A complete backup
includes `config.toml`, `sources.toml`, optional `env`, the matching
`<config-stem>.extensions.toml` sidecar (normally
`config.extensions.toml`) and `extensions/<config-stem>/` directory when
present, every manually configured `config_file` stored elsewhere, `state.db`,
downloads, and the MTProto session.

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
