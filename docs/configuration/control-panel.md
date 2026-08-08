# Telegram control panel

The optional control bot provides a single-message inline panel for sources,
statistics, filters, and tracked local resources. Send `/panel` or `/start` to
open it.

## Enable it safely

```toml
[control]
enabled = true
poll_interval_seconds = 10
panel_idle_timeout_seconds = 3600
allow_disk_delete = false
allowed_user_ids = ["123456789"]
allowed_chat_ids = []
allowed_message_thread_ids = []
```

Authorization is an AND across every non-empty allowlist. If a user and chat
are configured, both must match. If all allowlists are empty, every command is
denied. Prefer at least one allowed user ID; a chat-only allowlist permits every
member of that chat.

The panel closes after one idle hour by default. Set
`panel_idle_timeout_seconds = 0` only when an indefinitely active panel is
intentional.

## Source management

The panel can:

- add, enable, disable, and remove bot-managed YouTube and Twitch origins;
- choose live or VOD mode for a Twitch channel;
- inspect source polling errors and media/job statistics; and
- inspect, set, disable, or reset the global source filter.

Config-managed origins are visible but read-only. Change their TOML entries and
restart the service.

## Local resource library

The panel lists files recorded in SQLite artifacts. It does not recursively
scan the download directory or infer ownership from filenames.

Permanent disk deletion is opt-in:

```toml
[control]
allow_disk_delete = true
```

Restart after changing this setting. A deletion still requires an authorized,
current panel and a resource-specific confirmation. Only exact tracked regular
files below the configured download root are eligible. Database history and
existing Telegram messages are retained.
