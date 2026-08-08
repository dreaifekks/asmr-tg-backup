# Operate the service

## CLI workflow

Runtime commands accept `--config`. The option may appear before or after the
subcommand.

```bash
asmr-tg-backup init --config config.toml
asmr-tg-backup status --config config.toml
asmr-tg-backup poll --config config.toml --once --no-process
asmr-tg-backup process --config config.toml
asmr-tg-backup run --config config.toml
```

- `init` creates the data directories and SQLite schema without polling.
- `status` prints job counts and recent items.
- `poll --no-process` discovers and queues without downloading.
- `process` handles queued work without fetching sources.
- `run` starts continuous source polling, workers, Twitch live polling, and the
  optional control loop.

Use `enqueue` for one explicit YouTube URL:

```bash
asmr-tg-backup enqueue --config config.toml \
  https://www.youtube.com/watch?v=VIDEO_ID
```

Install or remove the native background service with:

```bash
asmr-tg-backup service install
asmr-tg-backup service uninstall
```

The uninstall command removes only the generated systemd unit. Runtime files
remain in the configuration and data directories.

Prefer `/panel` for source and filter changes. For a manual catalog edit,
validate and apply it explicitly:

```bash
asmr-tg-backup sources path --config config.toml
asmr-tg-backup sources validate --config config.toml
asmr-tg-backup sources apply --config config.toml
asmr-tg-backup sources list --config config.toml
```

`sources apply` reconciles the running database mirror; it does not rewrite
`config.toml`. Global settings in `config.toml` still require a service restart.

## State and backups

State lives under `[app].data_dir`, or the `ASMR_TG_BACKUP_DATA_DIR` environment
override. It includes:

- `state.db` and versioned migration backups;
- provider-specific downloads and derived Telegram files;
- yt-dlp archive files; and
- the MTProto `.session` file when that transport has been used.

`sources.toml` normally lives in the configuration directory rather than the
data directory. A complete backup includes `config.toml`, `sources.toml`, the
optional `env`, and the data-directory contents above. You can also make an
independent catalog snapshot with
`asmr-tg-backup sources export --output sources.backup.toml`. Stop the application
before a filesystem-level data backup and use a secret-safe destination. A
session carries reusable bot authorization, so its backup needs the same access
controls as the bot token. The Bot API volume is a separate service volume, not
an application archive.

## Update a Compose installation

Back up `./settings/sources.toml` and the data volume first, then pull and
recreate the official application image:

```bash
docker compose pull asmr-tg-backup
docker compose up -d asmr-tg-backup
docker compose ps
docker compose logs --tail=200 asmr-tg-backup
```

Include `--profile local-api` when the bundled Bot API is part of the stack. Do
not use `docker compose down -v` during a normal update; it removes named
volumes. For a deliberate source build, run
`docker compose build --pull asmr-tg-backup` before recreation instead.

## Update a PyPI installation

Stop the user service, back up data, and upgrade the `pipx` installation:

```bash
systemctl --user stop asmr-tg-backup.service
pipx upgrade asmr-tg-backup
asmr-tg-backup --version
asmr-tg-backup service install
systemctl --user status asmr-tg-backup.service
```

If a schema migration runs, retain its `state.db.bak-*` file until the service,
job counts, and recent files have been verified.

## Graceful shutdown

SIGTERM stops new claims and allows workers to drain. Twitch live recording
first interrupts ffmpeg so its current segment can be finalized. Supervisors
should provide a bounded grace period before force-killing descendants.
