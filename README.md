# asmr-tg-backup

ASMR-focused background service for discovering public media from
provider-backed origins, archiving it with `yt-dlp`, and optionally delivering
the archived files to Telegram. The built-in providers are YouTube official
channel feeds, generic RSS feeds, and Twitch VOD/live discovery through the
Helix API.

YouTube members-only discovery and authentication are outside the main worker's
public-origin path.

## Providers and origins

A provider is the implementation that knows how to discover remote media. An
origin is one configured channel, broadcaster, or feed handled by that
provider. Media identity is namespaced by provider, content kind, and external
ID, while `origin_items` records every origin that discovered it. This prevents
same-looking YouTube and Twitch IDs from colliding and prevents one origin from
overwriting another origin's association.

Use `[[origins]]` for new configuration:

```toml
[[origins]]
id = "youtube-example"
provider = "youtube"
kind = "uploads"
name = "Example YouTube channel"
external_id = "UC_CHANNEL_ID"
bootstrap = "latest"
enabled = false

[[origins]]
id = "twitch-example"
provider = "twitch"
kind = "vods"
name = "Example Twitch ASMR"
external_id = "twitch_login_or_numeric_broadcaster_id"
bootstrap = "latest"
enabled = false
# recording_mode = "live"
```

Supported built-in shapes are:

- `provider = "youtube"`, `kind = "uploads"`: `external_id` is a real
  `UC...` channel ID.
- `provider = "twitch"`, `kind = "vods"`: Twitch broadcasts;
  `external_id` may be a numeric broadcaster ID, login, or `@login`.
  `recording_mode = "vod"` discovers the archived broadcast after it ends,
  while `recording_mode = "live"` detects and records the active channel.
- `provider = "twitch"` also understands `highlights` and `uploads` kinds.
- `provider = "rss"`, `kind = "feed"`: `external_id` is the feed URL.

RSS media URLs must use HTTP(S) and resolve only to public addresses. Set an
origin's `allowed_media_hosts` array when the expected media hosts are known.
`allow_private_media = true` is an explicit opt-in for trusted local feeds and
should not be used for untrusted input.

`bootstrap = "latest"` keeps only the newest matching item eligible when an
origin is first seen. `bootstrap = "all"` allows backfill; Twitch bounds each
poll with `[twitch].max_pages_per_poll` and refuses to advance its checkpoint if
that bound is reached before a safe stopping point. Changing an existing origin
from `latest` to `all` is treated as an explicit backfill request: persisted seed
items are reactivated and that origin's discovery checkpoint is reset once.

The legacy `[[channels]]` and `[[feeds]]` config forms remain accepted and are
translated to origins at load time. The Telegram control panel manages dynamic
YouTube and Twitch origins; the old `/sub` commands remain as YouTube-compatible
aliases.

## Twitch API credentials

Keep Twitch credentials out of TOML. The config contains environment variable
names only:

```toml
[twitch]
client_id_env = "TWITCH_CLIENT_ID"
access_token_env = "TWITCH_ACCESS_TOKEN"
client_secret_env = "TWITCH_CLIENT_SECRET"
request_timeout_seconds = 30
max_pages_per_poll = 3
recording_mode = "vod"
live_poll_interval_seconds = 30
live_retry_seconds = 15
live_worker_count = 1
live_download_timeout_seconds = 0
```

Set `TWITCH_CLIENT_ID` and either:

- `TWITCH_ACCESS_TOKEN` for an existing token, or
- `TWITCH_CLIENT_SECRET` so the service can obtain and refresh an app access
  token with the client-credentials flow.

For an interactive shell:

```bash
export TWITCH_CLIENT_ID='<client-id>'
export TWITCH_CLIENT_SECRET='<client-secret>'
```

For systemd, put the assignments in the mode-`0600` file
`~/.config/asmr-tg-backup/env`. The shipped user unit loads this optional file.
Do not commit it.

## Twitch VOD versus live recording

Twitch VODs can become subscriber-only as soon as a stream ends. Choose the
recording behavior globally under `[twitch]`, or override it on one Twitch
`kind = "vods"` `[[origins]]` entry:

- `recording_mode = "vod"` keeps the previous behavior: poll Helix Get Videos
  and download the archived broadcast after Twitch publishes it. Public VODs
  work without a Twitch login. Subscriber-only VODs still require a Twitch
  account that is entitled to view them; this service does not bypass that
  restriction.
- `recording_mode = "live"` polls Helix Get Streams on the separate
  `live_poll_interval_seconds` schedule. When Twitch returns a new stream ID,
  a dedicated live worker runs `yt-dlp` against the channel URL from the
  current position until the broadcast ends.

The effective priority is the channel-specific mode stored by the panel or
static origin, then `[twitch].recording_mode` as the fallback default. Twitch
`highlights` and `uploads` origins always use post-publication downloads and do
not expose this switch.

The default 30-second live poll uses the same app access token as VOD discovery,
so it does not depend on email delivery or require a public webhook. Twitch
also offers the near-real-time EventSub `stream.online` event, but WebSocket
subscriptions require a user access token and still need a Get Streams
reconciliation after disconnects; EventSub is not required for this polling
mode.

Live jobs skip `[app].download_delay_seconds` and do not occupy the normal
download/Telegram worker lane. `live_download_timeout_seconds = 0` allows a
long stream to finish without the normal six-hour download timeout. Stopping
the service or losing the SQLite job lease terminates the `yt-dlp` process
group and leaves the job retryable, so it will not continue as an orphan.
The process is interrupted with `SIGINT` first so ffmpeg can finalize its
current container. Each interrupted attempt is retained as a separate segment;
after reconnecting, the service concatenates all usable segments before
delivery. A restart can only reconnect at the channel's then-current live
position, so it preserves material recorded before the restart but cannot
recover the interval while the service itself was stopped.

Twitch live HLS uses yt-dlp's ffmpeg downloader, so `ffmpeg` is required for
live mode. The worker adds bounded ffmpeg network reconnects; if a classified
transient network/ffmpeg failure occurs and a new probe confirms that the same
stream ID remains online, the job retries without consuming its failure budget.
Other fixed failures use the normal bounded retry budget. One live worker
records one channel at a time; increase `live_worker_count` when multiple
configured channels may overlap. TOML configuration is loaded at process
start, so restart the service after changing the global/static mode or worker
count. A bot-managed channel changed from the panel is stored in SQLite and
takes effect on the next relevant poll without a restart. If that channel is
already recording, the current attempt is allowed to finish safely; the new
mode controls subsequent discovery.

The existing Twitch download profile controls whether either mode keeps audio
or video. The shipped example extracts audio. To retain video, replace that
profile with:

```toml
[download.provider_profiles.twitch]
format = "bestvideo+bestaudio/best"
merge_output_format = "mp4"
extract_audio = false

[telegram]
media_type = "audio"
```

With this combination, the local Twitch master remains video while Telegram
gets a separate audio derivative; preparing the upload does not replace the
master artifact.

Live recording explicitly uses yt-dlp's default current-position behavior.
`--live-from-start` is experimental for Twitch and may depend on the associated
VOD, so it is not used for subscriber-locked archival.

## First setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
cp config.example.toml config.toml
chmod 600 config.toml
asmr-tg-backup init --config config.toml
asmr-tg-backup poll --config config.toml --once
```

Required host tools:

- `python3 >= 3.11`
- `yt-dlp`
- `curl` when Telegram delivery is enabled

`ffmpeg` and `ffprobe` are recommended for audio extraction, media merging,
upload-size reduction, and thumbnail conversion. `ffmpeg` is required when
Twitch `recording_mode = "live"` is enabled.

## State and automatic migration

State lives below `[app].data_dir` in `state.db`, `downloads/`, and the yt-dlp
archive file. Schema v2 stores `origins`, `media_items`, `origin_items`, `jobs`,
`artifacts`, and `deliveries` separately.

Opening an existing v1 database automatically:

1. creates `state.db.bak-v1-<UTC timestamp>` before changing the schema;
2. migrates existing videos, subscriptions, files, retries, and Telegram
   message IDs into the v2 tables;
3. retains the old tables as `videos_v1` and `subscriptions_v1`; and
4. creates compatibility views named `videos` and `subscriptions`.

The migration is versioned and idempotent. The backup is created with mode
`0600`; keep it until the migrated service has been verified.

## Download and delivery jobs

Discovery only creates or updates media and queues work. Downloading and
Telegram delivery are separate leased jobs with independent failure counts:

```text
origin discovery -> download job -> master artifact -> telegram_delivery job -> delivery record
```

An upload failure does not discard the master artifact or restart the download.
`run` keeps normal source polling and fast Twitch live polling on separate
threads, runs Telegram control long polling on its own thread, starts
`[app].worker_count` background workers, and starts
`[twitch].live_worker_count` isolated live-recording workers.
Workers atomically claim jobs and renew their leases while long subprocesses
run. Expired download leases return to retry; an expired Telegram delivery
lease becomes `uncertain` because Telegram has no idempotency key and the
remote message may already exist.

`uncertain` is deliberately not retried automatically. Inspect the destination
and logs before changing or requeueing that job, otherwise a duplicate Telegram
message may be sent. It is visible in `asmr-tg-backup status` and the job table.

Live, upcoming, and post-live media in VOD mode are deferred without consuming
the failure budget until a VOD is ready. Twitch live-recording jobs proceed
only while yt-dlp confirms `live_status=is_live`. Probe and download failures
consume the configured failure budget.

Important worker and timeout settings are:

```toml
[app]
worker_count = 1
worker_poll_interval_seconds = 2
job_lease_seconds = 900

[download]
probe_timeout_seconds = 180
download_timeout_seconds = 21600
ffmpeg_timeout_seconds = 7200

[telegram]
upload_timeout_seconds = 7200

[twitch]
request_timeout_seconds = 30
live_poll_interval_seconds = 30
live_retry_seconds = 15
live_worker_count = 1
live_download_timeout_seconds = 0
```

The lease heartbeat renews a running job periodically. Keep
`job_lease_seconds >= 30`; external-command timeouts bound hung work independently
of the lease.

## Telegram control panel

Use `/panel` (or `/start`) for the normal workflow. Every explicit command
creates a fresh panel message below that command. The bot then edits that new
message in place while navigating:

- view provider-neutral origins and their polling errors;
- add YouTube uploads;
- add a Twitch channel after choosing `直播中录制` or `直播结束后下载`;
- see each Twitch channel's current `LIVE`/`VOD` mode and switch it in place;
- enable, disable, or confirm deletion of bot-managed origins;
- view media/job statistics; and
- inspect, set, disable, or reset the global source filter.

Adding a source or entering a filter requires one user text message. For Twitch,
the mode is selected with an inline button before entering the login/name. The
bot stores the pending action and then returns to the current panel message;
it does not create a new bot response for every navigation action. Opening a
new panel disables the previous panel's buttons. Panel state is scoped by user,
chat, and message thread and survives service restarts.

By default, the panel automatically closes after one hour without a valid
button press or accepted text input. Closing edits the same Telegram message,
removes its inline keyboard, and rejects delayed callbacks from that expired
message. Every redraw also invalidates buttons from the previous keyboard
revision. Send `/panel` to open a fresh panel below the new command. This only
closes the control UI;
discovery, live recording, downloading, and Telegram delivery continue in the
background. The idle deadline survives service restarts and is checked within
one control long-poll window. Set `panel_idle_timeout_seconds = 0` to disable
expiry.

Panel data is read from the materialized `panel_snapshots` row instead of
re-running all status queries for every button press. SQLite triggers mark that
row dirty whenever origins, discovery state, media, jobs, artifacts, deliveries,
or the source filter change. It is rebuilt on the next panel read and maintained
at least every 30 seconds by the control loop. The control bot uses Telegram
long polling, so `poll_interval_seconds = 10` is the server-side long-poll
window, not an added 0-10 second delay before handling an update.

Equivalent command interfaces remain available:

```text
/panel
/origin add youtube <@handle|channel_id> [name]
/origin add twitch [vods|highlights|uploads] <login|user_id> [name]
/origin list
/origin enable|disable <origin_id>
/origin mode <origin_id> <vod|live>
/origin del <origin_id>
/sub add [live|channel] <@handle|channel_id> [name]
/sub del <id>
/sub list
/source_filter [regex|off|reset]
/stats
/start
/help
```

The `/origin add twitch ...` command stores the current global fallback on the
new channel. Use `/origin mode ...` to change that bot-managed channel later;
the normal `/panel` flow asks for the mode before the channel name.

Twitch credentials are never accepted from a Telegram message. A Twitch origin
added without service credentials is saved disabled; configure the environment,
restart the service, and enable it from the panel. Config-managed origins are
visible with their effective mode but remain read-only in the panel; change
their TOML entry instead. Deleting a bot-managed origin retains its historical
media, artifacts, and delivery records.

Authorization is an AND across every configured dimension. An empty dimension
is ignored, but each non-empty allowlist must match the incoming message. If all
three allowlists are empty, all commands are denied. For example, configuring a
user and chat requires both that user and that chat; adding a topic also requires
the matching topic.

```toml
[control]
enabled = true
# Telegram getUpdates long-poll window; accepted range is 1-30 seconds.
poll_interval_seconds = 10
# Close /panel after one idle hour; use 0 to keep it active indefinitely.
panel_idle_timeout_seconds = 3600
delete_webhook_on_startup = true
default_routes = ["live"]
allowed_user_ids = ["123456789"]
allowed_chat_ids = ["-1001234567890"]
allowed_message_thread_ids = ["42"]
```

Prefer at least `allowed_user_ids`; a chat-only allowlist intentionally permits
every member of that allowed chat. The default source filter is `/ASMR/i`. Use
`/source_filter off` to allow every source or `/source_filter reset` to restore
the default.

## Telegram delivery

Uploads remain disabled until `[telegram].enabled = true` and a bot token and
destination are configured. For large archives, use the local Telegram Bot API
service under `deploy/telegram-bot-api` or choose a smaller download format.
Files above `telegram.max_upload_bytes` are reduced when possible; an
unshrinkable oversize file is blocked rather than retried forever.

Caption templates may use `{title}`, `{url}`, `{feed_name}`, `{video_id}`, and
`{tag}`. Downloaded files are stored below provider-specific directories such as
`downloads/youtube/` and `downloads/twitch/`, with separate yt-dlp archive files
per provider. The default Twitch profile selects the best audio stream and
extracts it to M4A; the source video is not retained. Telegram therefore sends
the audio master directly unless it must derive a smaller audio artifact to fit
the configured upload limit. If the Twitch profile is changed to retain video
while `[telegram].media_type = "audio"`, Telegram instead creates and sends a
separate audio derivative and leaves the local video master intact.

## Permissions and user service

The service enforces mode `0700` on its data/download directories and `0600` on
SQLite state and migration backups. Keep the real TOML config and environment
file at `0600`. The shipped systemd unit also uses `UMask=0077` and several
hardening options.

```bash
mkdir -p ~/.config/systemd/user ~/.config/asmr-tg-backup
chmod 700 ~/.config/asmr-tg-backup
cp deploy/asmr-tg-backup.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now asmr-tg-backup.service
journalctl --user -u asmr-tg-backup.service -f
```

The unit expects the repo at `~/dev/asmr-tg-backup` and config at
`~/.config/asmr-tg-backup/config.toml`. SIGTERM stops new claims and gives worker
threads up to 25 seconds to drain. `KillMode=mixed` lets the main process first
interrupt live ffmpeg children cleanly; systemd then bounds the whole control
group with `TimeoutStopSec=30s`.

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

Tests should mock provider APIs, Telegram calls, and media subprocesses. Keep
real `config.toml`, secrets, state databases, downloads, and migration backups
out of git.
