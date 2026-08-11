# Contributing

Contributions may improve the application, its supported deployment paths, or
its user and developer documentation.

## Development environment

The project supports Python 3.11 and newer.

```bash
git clone https://github.com/dreaifekks/asmr-tg-backup.git
cd asmr-tg-backup
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[docs]"
```

Install `ffmpeg` and `ffprobe` when working on media workflows. A source build
that sends real MTProto uploads needs its own Telegram API ID/hash; normal unit
tests do not use real provider or Telegram credentials.

## Tests

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  .venv/bin/python -m unittest discover -s tests -v
```

Tests use temporary data directories and mock provider, Telegram, and media
process boundaries. Add focused coverage for every behavior change, including
failure and rollback paths where applicable.

## Documentation

User-facing behavior is documented in English and Simplified Chinese. The
MkDocs i18n plugin pairs `page.md` with `page.zh.md`.

```bash
.venv/bin/mkdocs build --strict --clean
.venv/bin/mkdocs serve --dev-addr 127.0.0.1:8001
```

Keep the README as the project landing page. Put detailed setup, operation, and
troubleshooting instructions in MkDocs, and implementation constraints in the
[architecture guide](development.md).

## Project invariants

- Discovery, downloads, and Telegram delivery use durable SQLite state.
- Download and delivery jobs have separate retries and leases.
- Ambiguous Telegram sends become `uncertain` and are not retried
  automatically.
- One application process owns a database and MTProto session pair.
- `sources.toml` is authoritative for sources and the filter. Panel and CLI
  changes go through the catalog manager; SQLite holds only the synchronized
  mirror and operational state.
- Extensions register capabilities while the core retains ownership of SQLite,
  jobs, downloads, delivery state, and the source catalog. Import only extension
  IDs explicitly enabled for the active process configuration.
- Keep one network route fixed for the complete request, subprocess, upload, or
  client connection. Select another route only at its retry or reconnect
  boundary.
- Tokens, API credentials, private configuration, databases, downloads, and
  sessions stay out of logs, command-line arguments, fixtures, packages, and
  commits.
- Local resource deletion remains opt-in and operates only on exact tracked
  paths below configured managed storage roots (`downloads` and an optional
  mounted archive root).

## Before opening a change

```bash
git diff --check
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  .venv/bin/python -m unittest discover -s tests
.venv/bin/mkdocs build --strict --clean
```

Update `CHANGELOG.md` under `Unreleased` for user-visible behavior.
