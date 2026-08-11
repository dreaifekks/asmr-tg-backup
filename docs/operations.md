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
- `run` starts continuous source polling, workers, provider-neutral live
  polling, and the optional control loop.

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
data directory. Here `<config-stem>` means the main config filename without its
final `.toml`. A complete backup contains:

- the main config, source catalog, and optional `env`;
- the managed extension sidecar named `<config-stem>.extensions.toml`;
- private setup files below `extensions/<config-stem>/`, plus any manually
  configured `config_file` stored elsewhere; and
- the data-directory contents above.

When a native local Bot API service is part of the deployment, also include
`~/.config/asmr-tg-backup/telegram-bot-api.env`,
`~/.config/systemd/user/asmr-tg-backup-telegram-bot-api.service`, and the
`telegram-bot-api` directory below the application data root.

For the default `config.toml`, the managed files are
`config.extensions.toml` and `extensions/config/`. These files may contain proxy
subscription URLs or other credentials, so store the backup with the same
access controls as the bot token and MTProto session.

You can also make an independent catalog snapshot with
`asmr-tg-backup sources export --config /path/to/config.toml --output sources.backup.toml`.
Stop the application before a filesystem-level data backup so SQLite,
downloads, and the session are captured together. Stop the native or Compose
Bot API service too before copying its data. The Compose Bot API volume is a
separate service volume; back it up separately when the deployment depends on
that local server.

## Update a Compose installation

The update replaces the application image while preserving configuration and
persistent volumes. Stop the application, then back up `.env`, `config.toml`,
`./settings/`, the data volume, every host-mounted extension config, and its
Compose override. Include the Dockerfile or pinned package list for a derived
image. When the `local-api` profile is part of the deployment, stop that service
and include its volume in the snapshot.

Pull and validate the official application image before recreation:

```bash
docker compose stop asmr-tg-backup
docker compose --profile local-api stop telegram-bot-api  # local-api deployments only
docker compose pull asmr-tg-backup
docker compose run --rm asmr-tg-backup \
  extensions doctor --config /config/config.toml
docker compose run --rm asmr-tg-backup \
  sources validate --config /config/config.toml
```

After the checks pass, start the application by itself or restore the complete
local Bot API stack:

```bash
# Application only:
docker compose up -d asmr-tg-backup

# Application plus the local Bot API profile:
docker compose --profile local-api up -d

docker compose ps
docker compose logs --tail=200 asmr-tg-backup
```

Keep named volumes in place during a normal update; `docker compose down -v`
deletes them.

For a source build, run `docker compose build --pull asmr-tg-backup` before the
two validation commands. For an extension-enabled derived image, update the
core tag in its `FROM` line, keep extension versions pinned, rebuild the derived
image, and point `ASMR_TG_BACKUP_IMAGE` at its new tag. Run the same
`extensions doctor` and `sources validate` checks against that image before
recreating the service.

## Update a PyPI installation

This update keeps the existing configuration and data while refreshing the
systemd unit for the upgraded executable. Stop the application first. If the
deployment uses the native local Bot API, stop that service too. Take the
filesystem-level backup while both services are stopped:

```bash
systemctl --user stop asmr-tg-backup.service
systemctl --user stop asmr-tg-backup-telegram-bot-api.service  # local Bot API only
```

Upgrade and validate while the services remain stopped:

```bash
pipx upgrade asmr-tg-backup
asmr-tg-backup --version
asmr-tg-backup extensions doctor \
  --config ~/.config/asmr-tg-backup/config.toml
asmr-tg-backup sources validate \
  --config ~/.config/asmr-tg-backup/config.toml
```

Resolve any extension compatibility or source validation error before starting
the services. For a trusted catalog extension, rerun
`extensions enable <slug>` to install the version selected by the upgraded
core; update a third-party extension through the same pipx environment and its
documented installation method. Then rerun both validations.

Start the local Bot API first when the deployment uses it, then refresh and
start the application service:

```bash
systemctl --user start asmr-tg-backup-telegram-bot-api.service  # local Bot API only
asmr-tg-backup service install
systemctl --user status asmr-tg-backup.service
```

`service install` regenerates the unit and starts the worker.

If a schema migration runs, retain its `state.db.bak-*` file until the service,
job counts, and recent files have been verified.

## Graceful shutdown

SIGTERM stops new claims and allows workers to drain. A live recording first
interrupts ffmpeg so its current segment can be finalized. Supervisors should
provide a bounded grace period before force-killing descendants.
