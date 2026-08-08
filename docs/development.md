# Architecture and development

This page is for people who want to understand, modify, or contribute to the
application. Installation and operation instructions remain in the user guides.

## Runtime flow

```text
Panel / source CLI
  -> atomic sources.toml replacement
  -> transactional SQLite source runtime mirror
YouTube / Twitch
  -> provider discovery using that mirror
  -> SQLite media and job state
  -> yt-dlp / ffmpeg download artifacts
  -> MTProto or Bot API delivery
  -> Telegram delivery records
```

Discovery records media before work is queued. Download and Telegram delivery
are separate durable jobs, so a delivery failure does not repeat a completed
download. Workers claim jobs with leases. An ambiguous Telegram result becomes
`uncertain` and is not retried automatically because Telegram may already have
accepted the message.

## Module map

| Module | Responsibility |
| --- | --- |
| `cli.py`, `setup.py` | Commands, guided setup, and generated private configuration |
| `config.py` | TOML parsing, environment overrides, and validation |
| `source_catalog.py` | Catalog validation, atomic writes, and SQLite reconciliation |
| `sources.py`, `youtube.py` | YouTube/Twitch discovery and normalized media metadata |
| `service.py` | Polling, worker orchestration, retries, and graceful shutdown |
| `store.py` | SQLite schema, migrations, jobs, leases, and tracked resources |
| `downloader.py` | `yt-dlp`/`ffmpeg` execution and derived media artifacts |
| `telegram_mtproto.py`, `telegram.py` | MTProto and Bot API media transports |
| `control.py` | Authorized Telegram control panel and tracked-file operations |

`config.toml` supplies process-wide settings and the catalog path. Application
writes and database reconciliation pass through `SourceCatalogManager`: it
locks the catalog directory, replaces `sources.toml` atomically, then reconciles
the database in one transaction. Manual edits become active through its
`apply` path. A failed reconciliation restores the previous catalog bytes. The
SQLite copy is operational state, not a second configuration authority.

## Local environment

Python 3.11 or newer is supported. Install `ffmpeg` and `ffprobe` when testing
media workflows.

```bash
git clone https://github.com/dreaifekks/asmr-tg-backup.git
cd asmr-tg-backup
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[docs]"
```

Source builds that exercise MTProto need their own complete Telegram
application configuration. Unit tests must use mocks and temporary files; they
must not contact a real provider, bot, destination, or private data directory.

For a long-running source checkout, create a private config from the
[source configuration example](https://github.com/dreaifekks/asmr-tg-backup/blob/master/deploy/source-config.example.toml){ target="_blank" rel="noopener noreferrer" },
then register the current virtual environment directly:

```bash
.venv/bin/asmr-tg-backup service install \
  --config ~/.config/asmr-tg-backup/config.toml
```

The generated unit records the current virtualenv interpreter and config path.
Packaged installations use the same command from the
[PyPI guide](getting-started/pypi.md#run-as-a-user-service).

## Validate a change

```bash
git diff --check
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  .venv/bin/python -m unittest discover -s tests -v
.venv/bin/mkdocs build --strict --clean
```

Serve the bilingual documentation locally with:

```bash
.venv/bin/mkdocs serve --dev-addr 127.0.0.1:8001
```

English pages use `page.md`; Simplified Chinese pages use `page.zh.md`. Keep
paired pages structurally aligned whenever user-visible behavior changes.

## Invariants

- Run only one application process against a database and MTProto session pair.
- Keep discovery, download, and delivery state durable across restarts.
- Keep `sources.toml` authoritative for sources and the filter; SQLite holds
  only their runtime mirror and operational state.
- Never retry `uncertain` Telegram delivery automatically.
- Keep tokens, application credentials, Twitch credentials, private
  configuration, databases, downloads, and sessions out of logs, command-line
  arguments, fixtures, packages built from a source checkout, and commits.
- Keep local resource deletion opt-in and limited to exact SQLite-tracked
  regular files below the configured download root.

See [Contributing](contributing.md) for the complete contribution workflow and
checklist.
