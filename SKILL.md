---
name: ytb-tg-backup
description: Use when working on ytb-tg-backup, a Python CLI and leased background worker that discovers public YouTube, RSS, and Twitch media through provider-backed origins, archives it with yt-dlp, stores provider-neutral state in SQLite, and optionally delivers artifacts to Telegram.
---

# ytb-tg-backup

## Project Shape

- Python 3.11+ package under `src/ytb_tg_backup`; console entrypoint is
  `ytb-tg-backup = ytb_tg_backup.cli:main`.
- Runtime Python dependencies are intentionally empty. Host tools perform media
  and Telegram work: `yt-dlp`, `curl`, and preferably `ffmpeg`/`ffprobe`.
- Main config is `config.toml`, copied from `config.example.toml`. Never commit a
  real config, Telegram token/chat ID, Twitch credential, or environment file.
- State lives under `[app].data_dir`: `state.db`, provider-specific downloads,
  and the yt-dlp archive file.
- New source configuration uses `[[origins]]`. Legacy `[[channels]]` and
  `[[feeds]]` are translated to origins and remain supported.

## Quick Start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
cp config.example.toml config.toml
chmod 600 config.toml
ytb-tg-backup init --config config.toml
ytb-tg-backup status --config config.toml
```

Use `ytb-tg-backup poll --config config.toml --once --no-process` for discovery
without running jobs. Use `poll --once` or `process` only when the user expects
real probing, downloading, transcoding, or Telegram work.

For local imports without an editable install:

```bash
PYTHONPATH=src python3 -m ytb_tg_backup status --config config.toml
```

## Test

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

Tests must mock provider APIs, Telegram API calls, and subprocess calls to
`yt-dlp`, `curl`, or `ffmpeg`. Use temporary `data_dir` values and synthetic v1
databases instead of the user's real state.

## Provider and Origin Model

- An `Origin` is one configured channel, broadcaster, or feed. `provider`
  selects the discovery adapter and `kind` selects the remote collection.
- Supported public adapters are YouTube official feeds, generic RSS, and Twitch
  Helix. Twitch `kind = "vods"` maps to public archived broadcasts.
- RSS media URLs are validated as public HTTP(S) destinations before both
  discovery and download. Use an origin `allowed_media_hosts` list where
  possible; private destinations require the explicit `allow_private_media`
  opt-in.
- A media item is unique by `(provider, content_kind, external_id)`. The
  `origin_items` join preserves all origins that discovered it and stores
  per-origin filtering/bootstrap decisions.
- `bootstrap = "latest"` keeps only the newest matching item eligible on first
  discovery; `all` permits backfill. Twitch bounds a backfill poll with
  `[twitch].max_pages_per_poll`.
- Public Twitch discovery reads `TWITCH_CLIENT_ID` and either
  `TWITCH_ACCESS_TOKEN` or `TWITCH_CLIENT_SECRET` through the environment names
  configured in `[twitch]`. Keep secret values out of TOML.

## Code Map

- `src/ytb_tg_backup/cli.py`: `init`, `run`, `poll`, `process`, `status`, and
  YouTube-compatible manual `enqueue` commands.
- `src/ytb_tg_backup/config.py`: TOML dataclasses, legacy config translation,
  worker/timeout settings, origins, and Twitch environment lookup.
- `src/ytb_tg_backup/models.py`: provider-neutral origin, media candidate,
  discovery result, and claimed-job values.
- `src/ytb_tg_backup/sources.py`: adapter registry plus YouTube, RSS, and Twitch
  Helix discovery and Twitch app-token refresh.
- `src/ytb_tg_backup/service.py`: polling scheduler, control loop, worker pool,
  lease heartbeat, and download/delivery job handlers.
- `src/ytb_tg_backup/store.py`: schema migrations; origins, media, jobs,
  artifacts, deliveries; atomic job claiming; compatibility APIs/views; and
  the trigger-invalidated `panel_snapshots` materialized panel row.
- `src/ytb_tg_backup/feed.py`: Atom/RSS parsing and YouTube ID extraction.
- `src/ytb_tg_backup/youtube.py`: official feed URL creation and safe channel
  ID resolution.
- `src/ytb_tg_backup/downloader.py`: provider-namespaced `yt-dlp` downloads and
  bounded `yt-dlp`/`ffmpeg` subprocesses, per-provider formats, and independent
  archive files. Twitch defaults to an M4A audio master and does not retain the
  source video.
- `src/ytb_tg_backup/telegram.py`: bounded Telegram upload via `curl`, with the
  token supplied through stdin rather than the process command line.
- `src/ytb_tg_backup/control.py`: provider-neutral `/origin` management and a
  persistent single-message Telegram inline-keyboard panel for origins,
  filtering, and statistics. It reads the cached panel snapshot and receives
  updates through Telegram long polling. Legacy `/sub` remains a YouTube alias.
- `scripts/`: one-off config/state repair utilities.
- `deploy/`: hardened user systemd unit and optional local Telegram Bot API.

## State and Job Flow

```text
origin discovery -> download job -> master artifact -> telegram_delivery job -> delivery record
```

- Download and Telegram delivery have independent job rows, failure budgets,
  retry schedules, and lease ownership. An upload error must not re-download an
  existing master artifact.
- `run` leaves source polling on the main thread, starts one control long-poll
  thread, and starts `[app].worker_count` job threads. Each thread owns its
  SQLite connection. Job workers atomically claim either job type and renew
  leases in a heartbeat thread.
- Live/not-ready media is deferred without consuming the failure budget. Probe,
  download, transcode, and definitive Telegram failures consume their own job's
  budget.
- An expired download lease becomes retryable. An expired or response-ambiguous
  Telegram delivery becomes `uncertain` and is not claimed automatically,
  because Telegram does not provide an idempotency key. Verify the destination
  before any operator requeue.
- Relevant settings are `[app].worker_count`,
  `worker_poll_interval_seconds`, `job_lease_seconds`;
  `[download].probe_timeout_seconds`, `download_timeout_seconds`,
  `ffmpeg_timeout_seconds`; `[telegram].upload_timeout_seconds`; and
  `[twitch].request_timeout_seconds`.

## Schema Migration

- `Store.initialize()` automatically detects an existing v1 `videos` or
  `subscriptions` table before applying schema v2.
- It first creates one `state.db.bak-v1-<UTC timestamp>` SQLite backup, migrates
  legacy jobs/artifacts/deliveries, renames old tables to `videos_v1` and
  `subscriptions_v1`, and creates read compatibility views.
- Migration versions are recorded in `schema_migrations`; repeated initialize
  calls must not create another v1 backup or duplicate rows.
- `panel_snapshots` is an additive schema-v2 cache. Triggers mark it dirty on
  relevant table/filter changes; control-loop maintenance refreshes it at least
  every 30 seconds, and the panel refresh buttons force a rebuild.
- Do not delete the backup or old tables until the migrated runtime has been
  checked against the real database.

## Control Authorization

- Authorization requires every configured dimension to match: user, chat, and
  message thread are combined with AND.
- An empty dimension is ignored. If all three allowlists are empty, all control
  commands are denied.
- Prefer `allowed_user_ids`. A chat-only configuration intentionally authorizes
  every member of that allowed chat.
- Preserve this behavior in `tests/test_control.py`; do not restore the former
  OR semantics.

## Change Checklist

- Config changes: update `config.py`, both example TOMLs, README/SKILL, and
  `tests/test_config.py`.
- Adapter changes: update `models.py`/`sources.py`, mock all network paths, and
  cover provider ID collisions plus multi-origin discovery.
- Worker changes: preserve atomic claims, lease-token checks, heartbeat renewal,
  independent failure budgets, and stale-delivery `uncertain` behavior.
- Store changes: add a versioned migration and synthetic old-database tests;
  never silently replace or discard an existing `state.db`.
- Telegram changes: keep the bot token out of argv/errors and distinguish
  ambiguous results from definitive failures.
- Control changes: preserve all-configured-dimensions-must-match authorization.

## Operations Notes

- Do not run live provider, media, or Telegram operations unless requested.
- Keep data/download directories at `0700`; keep config, environment, SQLite,
  and migration backup files at `0600`. The shipped unit uses `UMask=0077`.
- For systemd Twitch credentials, use a mode-`0600` `EnvironmentFile` outside
  the repository and add it through the installed user-unit configuration.
- The unit expects the repo at `~/dev/ytb-tg-backup` and config at
  `~/.config/ytb-tg-backup/config.toml`.
- The local Telegram Bot API service is under `deploy/telegram-bot-api`; set
  `telegram.api_base = "http://127.0.0.1:18081"` when using it.
- Do not commit `.venv/`, `__pycache__/`, `*.egg-info/`, real configs,
  environment files, SQLite/WAL files, downloads, archives, or migration
  backups.
