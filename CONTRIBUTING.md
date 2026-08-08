# Contributing

[English documentation](docs/contributing.md) ·
[简体中文](docs/contributing.zh.md)

Contributions should improve the application, its supported deployment paths,
or its user and developer documentation.

## Development environment

The project supports Python 3.11 and newer.

```bash
git clone https://github.com/dreaifekks/asmr-tg-backup.git
cd asmr-tg-backup
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[docs]"
```

`ffmpeg` and `ffprobe` are required for the default audio workflow and tests
that execute real media tools. Source builds that exercise MTProto need their
own Telegram application configuration; normal unit tests do not use real
Telegram credentials.

## Tests

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  .venv/bin/python -m unittest discover -s tests -v
```

Tests must use temporary data directories and mocked provider, Telegram, and
media-process boundaries. Do not run a test against a real bot, destination,
SQLite database, download directory, or MTProto session.

## Documentation

User-facing behavior must be documented in both English and Simplified Chinese.
The MkDocs i18n plugin pairs `page.md` with `page.zh.md`.

```bash
.venv/bin/mkdocs build --strict --clean
.venv/bin/mkdocs serve --dev-addr 127.0.0.1:8001
```

Keep the README as a short project landing page. Detailed setup, operation, and
troubleshooting belong in MkDocs; implementation constraints belong in the
[architecture and development guide](docs/development.md).

## Design constraints

- Discovery, downloads, and Telegram delivery use durable SQLite state.
- Download and delivery jobs have separate retries and leases.
- Ambiguous Telegram sends become `uncertain` and are not retried
  automatically.
- One application process owns a database and MTProto session pair.
- Bot tokens, application credentials, Twitch credentials, and sessions must
  never enter logs, command-line arguments, fixtures, packages built from a
  source checkout, or commits.
- Local resource deletion remains opt-in and operates only on exact tracked
  paths below the configured download root.

## Before opening a change

```bash
git diff --check
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  .venv/bin/python -m unittest discover -s tests
.venv/bin/mkdocs build --strict --clean
```

Update `CHANGELOG.md` under `Unreleased` for user-visible behavior. Do not
commit real `config.toml`, `.env`, SQLite/WAL files, downloads, archives,
MTProto sessions, or virtual environments.
