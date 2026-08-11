# asmr-tg-backup

`asmr-tg-backup` discovers YouTube channel uploads and Twitch VOD/live media,
archives it with `yt-dlp`, and can optionally deliver the files to Telegram.
Optional extensions add providers such as Niconico or route selected network
operations through a proxy. The core continues to own scheduling, durable
state, downloads, and delivery.

The Telegram panel and source CLI manage an editable `sources.toml` catalog.
YouTube and Twitch buttons appear by default; every enabled source extension
adds its own provider button, while RSS feeds use the manual catalog path.
SQLite records the catalog's runtime mirror plus discovery, download,
delivery, and control-panel state.

[View the Telegram showcase](https://t.me/+9-Cy-yue1PJiMWY9){ target="_blank" rel="noopener noreferrer" }

## Start here

- Use [PyPI and native Linux](getting-started/pypi.md) for a lightweight service
  with guided setup.
- Use [Docker Compose](getting-started/docker-compose.md) for a reproducible
  container with persistent `/data`.
- Read [Choose a deployment](getting-started/index.md) to compare both paths and
  prepare the bot, destination, and administrator ID.
- Add [optional extensions](configuration/extensions.md) after the core
  installation and basic configuration when the deployment needs another
  source provider or scoped network routing. If the first connection itself
  requires a proxy, enable `proxy-router` before starting the service.

Official PyPI and GHCR releases use direct MTProto upload after you provide the
bot token and destination, so no separate Bot API server is required. You can
instead connect an existing, local, or Telegram-hosted Bot API endpoint.

## What is covered

- [Control panel](configuration/control-panel.md): the recommended source and
  filter workflow, status, and tracked-file deletion.
- [Sources and downloads](configuration/sources.md): complete catalog fields,
  manual tuning, built-in and extension providers, and download profiles.
- [Extensions](configuration/extensions.md): one-command enablement, generated
  Panel provider buttons, private configuration, and scoped connection routing.
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
