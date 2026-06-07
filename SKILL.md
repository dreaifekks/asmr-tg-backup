---
name: ytb-tg-backup
description: Use when working on ytb-tg-backup, a Python CLI and background worker that polls YouTube official channel Atom feeds, downloads new videos with yt-dlp, tracks state in SQLite, optionally uploads archives to Telegram, and provides a Telegram control bot for subscriptions.
---

# ytb-tg-backup

## Project Shape

- Python 3.11+ package under `src/ytb_tg_backup`; console entrypoint is `ytb-tg-backup = ytb_tg_backup.cli:main`.
- Runtime Python dependencies are intentionally empty; host tools do the external work.
- Required host tools: `yt-dlp` for probing, downloading, and resolving `@handle` channel IDs; `curl` for Telegram uploads.
- Recommended host tools: `ffmpeg` and `ffprobe` for audio extraction, size reduction, and thumbnail conversion.
- Main config is `config.toml`, copied from `config.example.toml`. Treat real configs, Telegram bot tokens, chat IDs, and local paths as secrets.
- State lives under `[app].data_dir`: `state.db`, `downloads/`, and `download-archive.txt`.

## Quick Start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
cp config.example.toml config.toml
ytb-tg-backup init --config config.toml
ytb-tg-backup status --config config.toml
```

Use `ytb-tg-backup poll --config config.toml --once --no-process` to fetch feeds and enqueue items without downloading. Use `ytb-tg-backup poll --config config.toml --once` only when the user expects real YouTube probing/download work and the config is safe.

For local imports without an editable install:

```bash
PYTHONPATH=src python3 -m ytb_tg_backup status --config config.toml
```

## Test

After `pip install -e .`:

```bash
python3 -m unittest discover -s tests
```

Without installing the package:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

Tests should mock network calls, Telegram API calls, and subprocess calls to `yt-dlp`, `curl`, or `ffmpeg`. Use temporary `data_dir` values instead of the user's real config/state.

## Code Map

- `src/ytb_tg_backup/cli.py`: argparse CLI commands: `init`, `run`, `poll`, `process`, `status`, and `enqueue`.
- `src/ytb_tg_backup/config.py`: TOML loading and dataclass defaults. `[[channels]]` expand to YouTube official Atom feed URLs; `[[feeds]]` is the raw RSS escape hatch.
- `src/ytb_tg_backup/service.py`: orchestration for feed polling, queue processing, live/VOD waiting, downloading, shrinking, thumbnail prep, Telegram upload, and control bot polling.
- `src/ytb_tg_backup/store.py`: SQLite schema and state transitions for `videos`, `subscriptions`, and bot offset state.
- `src/ytb_tg_backup/feed.py`: Atom/RSS parsing and YouTube video ID extraction.
- `src/ytb_tg_backup/youtube.py`: official feed URL creation and `UC...` channel ID resolution from `@handle` or URL.
- `src/ytb_tg_backup/downloader.py`: `yt-dlp` and `ffmpeg` subprocess integration.
- `src/ytb_tg_backup/telegram.py`: Telegram Bot API upload via `curl`.
- `src/ytb_tg_backup/control.py`: Telegram `getUpdates` control bot for `/sub` and `/stats`.
- `scripts/`: one-off migration and repair utilities for configs, subscriptions, and initial seed rows.
- `deploy/`: user systemd unit and optional local Telegram Bot API Docker Compose service.

## State Flow

- Feed entries are inserted as `seen`; on a feed's first poll, only the newest entry stays `seen` and older seed entries become `ignored`.
- Processing waits for `[app].download_delay_seconds`, probes with `yt-dlp`, and keeps live/upcoming/post-live items in `waiting_ready`.
- A successful download becomes `downloaded`.
- If Telegram is disabled, `downloaded` is the terminal local archive state.
- If Telegram is enabled, upload success becomes `uploaded`; oversized unshrinkable files become `blocked`; retryable errors become `failed` with `next_retry_at`.

## Change Checklist

- Config changes: update dataclasses in `config.py`, `load_config`, `config.example.toml`, README/SKILL docs when user-facing, and `tests/test_config.py`.
- CLI changes: update `cli.py`, README examples, and add or adjust tests.
- Feed parsing changes: update `feed.py` and `tests/test_feed.py`.
- Download/subprocess changes: update `downloader.py` and mock subprocesses in tests.
- Telegram upload changes: update `telegram.py` and `tests/test_telegram.py`.
- Control bot changes: update `control.py` and `tests/test_control.py`; preserve allowlist behavior where user/chat/topic allowlists are OR-based and empty allowlists deny all commands.
- Store/schema changes: update `store.py`, add migration or compatibility handling when existing SQLite state matters, and cover the state transition in tests.

## Operations Notes

- Do not commit `.venv/`, `__pycache__/`, `*.egg-info/`, `config.toml`, `outputs/`, `work/`, SQLite files, downloads, or archives.
- Do not run commands that contact YouTube, Telegram, or download media unless the user asked for live operations.
- The systemd unit expects the repo at `~/dev/ytb-tg-backup` and config at `~/.config/ytb-tg-backup/config.toml`.
- The local Telegram Bot API compose service lives in `deploy/telegram-bot-api`; set `telegram.api_base = "http://127.0.0.1:8081"` when using it.
- If `git status` says this is not a repository while an empty `.git/` directory exists, inspect `.git/`; initialize or replace it only after the user agrees to that repository operation.
