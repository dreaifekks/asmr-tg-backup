# asmr-tg-backup

`asmr-tg-backup` discovers YouTube channel uploads and Twitch VOD/live media,
archives it with `yt-dlp`, and can optionally deliver the files to Telegram.
The Telegram panel and source CLI manage an editable `sources.toml` catalog;
SQLite records its runtime mirror plus discovery, download, delivery, and
control-panel state.

[View the Telegram showcase](https://t.me/+9-Cy-yue1PJiMWY9){ target="_blank" rel="noopener noreferrer" }

## Start here

- Use [PyPI and native Linux](getting-started/pypi.md) for a lightweight service
  with guided setup.
- Use [Docker Compose](getting-started/docker-compose.md) for a reproducible
  container with persistent `/data`.
- Read [Choose a deployment](getting-started/index.md) to compare both paths and
  prepare the bot, destination, and administrator ID.

Official PyPI and GHCR releases use direct MTProto upload after you provide the
bot token and destination, so no separate Bot API server is required. You can
instead connect an existing, local, or Telegram-hosted Bot API endpoint.

## What is covered

- [Control panel](configuration/control-panel.md): the recommended source and
  filter workflow, status, and tracked-file deletion.
- [Sources and downloads](configuration/sources.md): complete catalog fields,
  manual tuning, YouTube, Twitch, and download profiles.
- [Telegram delivery](configuration/telegram.md): transports, sessions, size
  limits, and security boundaries.
- [Operate](operations.md): commands, backups, updates, and shutdown.
- [Troubleshoot](troubleshooting.md): common setup and runtime failures.
- [Reference](reference.md): CLI, paths, configuration sections, and environment
  overrides.
- [Architecture and development](development.md): runtime boundaries and the
  external contribution workflow.
- [Contributing](contributing.md): local environment, tests, documentation, and
  the pre-change checklist.
