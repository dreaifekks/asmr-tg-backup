# Development YouTube membership notifications

This feature observes anonymous YouTube metadata and can send Telegram text
notifications. It is an experiment, not a members-only downloader and not an
authentication mechanism.

## Safety boundary

The integrated runner is enabled only by `[dev.youtube_membership]`. It accepts
an explicit list of existing, disabled YouTube origin IDs and uses those
origins only as channel identifiers and labels. Requiring the origins to be
`enabled = false` keeps the production source worker from polling them.
Discoveries are not handed to the production source registry or
`Store.upsert_discovered()`. A configured enabled YouTube origin for the same
channel is rejected; a matching control-panel origin that appears later is
also skipped by the production poller while this dev feature is enabled.
Previously queued download or media-delivery jobs associated with the reserved
channel are cancelled before any downloader or Telegram media call; existing
files are retained.

Its SQLite database is `<app.data_dir>/dev/youtube-membership.db`. It contains
surface observations, bounded probe work, lifecycle state, and a text-message
outbox. It contains no media artifacts or download jobs. The directory is mode
`0700` and the database/lock files are mode `0600` where the filesystem permits.

The yt-dlp inspector uses its own `dev.youtube_membership.yt_dlp` executable
setting and never reads `[download].yt_dlp` or `[download].extra_args`. Each
invocation includes `--ignore-config`; cookies, `--cookies-from-browser`,
extractor authentication, and arbitrary yt-dlp arguments are not valid dev
configuration keys. Point `yt_dlp` at the real executable, not a wrapper that
adds credentials.

## Discovery and lifecycle

For each allowlisted channel, a cycle compares three anonymous surfaces:

1. the undocumented `UUMO...` members playlist;
2. the channel `streams` tab via flat yt-dlp extraction;
3. the official public Atom feed.

The `UUMO...` surface is useful evidence but is not a documented API contract.
Atom is auxiliary and is not assumed to enumerate member content. Selected IDs
receive `--skip-download` metadata probes. The runner never requests formats
for download or writes media.

The first successful result from each surface seeds a silent baseline. New
confirmed items can then produce one notification for each relevant event:

- `upcoming`: a member stream is scheduled;
- `live`: a member stream starts;
- `ended`: a previously live member stream reaches a post-live state;
- `published`: member content is first confirmed without an active live state.

Outbox uniqueness prevents the same event from being sent twice. If the process
dies after claiming a Telegram message, that row becomes `uncertain` on the
next open and is not resent automatically.

## Configuration and commands

Start with notifications disabled so the first cycle only establishes state:

```toml
[telegram]
enabled = false
bot_token = "YOUR_BOT_TOKEN"
chat_id = "YOUR_CHAT_ID"

[dev.youtube_membership]
enabled = true
notify = false
origin_ids = ["youtube-member-test"]
yt_dlp = "yt-dlp"
poll_interval_seconds = 1800
request_timeout_seconds = 180
request_spacing_seconds = 5.0
tab_limit = 30
chat_id = ""
```

```bash
asmr-tg-backup dev youtube-membership once --config config.toml
asmr-tg-backup dev youtube-membership status --config config.toml
```

After checking `counts`, `cooldown_until`, and `recent_notifications`, set
`notify = true`. A text-only notification may run while
`telegram.enabled = false`; this is intentional so enabling the experiment does
not activate normal media delivery. `asmr-tg-backup run` then starts the dev
runner in a separate thread, or it can be isolated in the foreground:

```bash
asmr-tg-backup dev youtube-membership run --config config.toml
```

`status` remains usable when the feature is disabled, allowing old experiment
state to be inspected safely.

## Failure handling

- `removed`, `private`, and `unavailable` are terminal.
- Network/tool/PO-token/login/no-format probe failures have a five-attempt cap.
- A rate limit or bot-check creates a persistent six-hour global cooldown.
- Notification failures retry at most five times; ambiguous Telegram outcomes
  become `uncertain` instead of being sent again.
- A non-blocking file lock rejects a second writer against the same dev state.

For multi-day raw surface comparison without Telegram notifications, the older
standalone observer remains available at
`python -m ytb_tg_backup.dev.member_observer`; see
[`member-observer.md`](member-observer.md). It uses its own config and database.
