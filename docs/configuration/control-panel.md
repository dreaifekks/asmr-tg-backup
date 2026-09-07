# Telegram control panel

The Telegram control panel keeps routine source and local-resource operations
in one inline message. Send `/panel` or `/start` after it is enabled.

## What you can do

The panel lets an authorized user:

- add YouTube and Twitch sources;
- add providers registered by enabled extensions, such as Niconico;
- enable, disable, inspect, and remove sources;
- choose Twitch live recording or archive download;
- manage the global source filter and inspect polling or job status;
- browse channel favorites ranked by Telegram reaction totals and maintain a
  personal Panel favorite list; and
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
reaction_favorites_enabled = false
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

## Favorites and channel pins

When the delivery target is a Telegram channel, reaction favorites are an
explicit opt-in:

```toml
[control]
enabled = true
reaction_favorites_enabled = true
```

The control worker then explicitly subscribes to Bot API
`message_reaction_count` updates. It accepts only messages that belong to the
current destination in SQLite `deliveries`. Per-reaction counts, the aggregate
total, message ID, update time, and pin-sync state are persisted in `state.db`.
The bot calls `pinChatMessage` when a total changes from zero to a positive
value. When the total returns to zero, it removes only a pin synchronized by
this feature; it never clears unrelated channel pins. Multiple messages from a
split delivery are grouped under one media item and their counts are summed in
the ASMR ranking.

The bot must be a target-channel administrator with the channel
`can_edit_messages` right. Reaction counts are still recorded if that right is
missing; pin errors are persisted and retried with backoff. Bot API does not
backfill reaction totals from before the feature was enabled, so rankings begin
with updates received afterward.

Telegram channel reactions are anonymous. Bot API provides the total but
cannot reliably attribute a native reaction to the current Panel user. The
Panel therefore keeps the concepts explicit:

- `❤️ Total ranking` uses native Telegram reaction totals and sorts descending;
- `⭐ My favorites` is the authorized user's explicit Panel favorite list,
  also sorted by the current total.

Every entry includes a URL button that opens the original channel message.
Public channels use `t.me/<username>/<message_id>`; private-channel links use
the member-only `t.me/c/...` form.

## Open the panel

1. Save the `[control]` settings and restart the service with the command for
   your [deployment method](../operations.md).
2. Send `/panel` or `/start` from an allowed user, chat, and message thread.
3. Use `🔄 Refresh` when you want a new runtime snapshot. The panel also
   refreshes after each completed action.
4. Send `/panel` again whenever you want a fresh panel message.

## Source management

### Back up one video

After selecting `➕ YouTube`, send `@handle [display name]` to subscribe as before,
or send a video URL directly, optionally prefixed with `url`:

```text
https://www.youtube.com/watch?v=abcdefghijk
url "https://youtu.be/abcdefghijk"
```

YouTube watch, youtu.be, Shorts, live, and embed video URLs are supported.
Playlist and sharing parameters are removed from video links; a playlist-only
URL is rejected. Ordinary channel URLs still subscribe, while `url "channel URL"`
is rejected.

For Twitch, select the source type and recording mode, then send
`https://www.twitch.tv/videos/123456` or `url "https://www.twitch.tv/videos/123456"`.
The configured Twitch API credentials resolve that video ID and its actual type
(VOD, Highlight, or Upload), regardless of the selected subscription mode.
No channel scan or channel recording starts. Clips and live channel URLs are
not supported in single-video mode.

Submit one URL at a time. A single-video request does not change `sources.toml`
or subscribe to future uploads. It bypasses the global source keyword filter
and new-video download delay, then uses the existing download, conversion,
local storage, and configured Telegram delivery pipeline. Access restrictions,
not-ready deferrals, and retries still apply. Repeated requests reuse existing
jobs; successful or uncertain deliveries are not resent, and blocked jobs
still require the existing recovery workflow.

Extension providers can opt in through `resolve_media_url`; the Panel displays
URL instructions for providers that implement it. Older extensions retain their
source identifier input and reject explicit `url` requests as unsupported.

The command form also works: `/origin add youtube url "https://youtu.be/abcdefghijk"`.

### Manage subscriptions

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
| Telegram reaction totals, pin-sync state, personal Panel favorites | `state.db` | Maintained by reaction updates and authorized Panel buttons |
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
regular files below a configured managed storage root are eligible; this
includes an enabled mounted `[storage].archive_dir`. Database history and
existing Telegram messages remain. An unavailable archive mount is shown as an
unsafe/missing resource and is never treated as permission to delete another
path.

When a Telegram delivery reaches `uncertain` because the response boundary is
ambiguous, its resource detail shows **Resolve uncertain delivery**. An operator
must verify the destination first, then explicitly confirm delivery or accept
the duplicate-message risk and force a resend. Both actions have a state-version
check and a separate audit record; the Panel never retries automatically.

`allow_disk_delete` controls only deletion started from the Panel. Automatic
cleanup and mounted storage use `[storage].process_retention_hours`,
`[storage].backup_retention_hours`, and `archive_dir`. See
[Automatic local retention](sources.md#automatic-local-retention).
