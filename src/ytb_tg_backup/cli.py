from __future__ import annotations

import argparse
from importlib import resources
import json
import logging
import os
from pathlib import Path
import shlex
import signal
import sqlite3
import sys

from . import __version__
from .config import load_config
from .extension_api import EXTENSION_API_LEVEL, ExtensionError
from .extension_catalog import TRUSTED_EXTENSIONS, trusted_extension_by_id
from .extension_management import ExtensionManagementError, enable_extension
from .extensions import (
    ExtensionHost,
    SourceProviderCatalog,
    build_runtime,
    doctor_extension_runtime,
)
from .service import BackupService
from .setup import (
    APPLICATION_UNIT,
    SetupError,
    default_config_path,
    install_application_service,
    run_interactive_setup,
    uninstall_application_service,
)
from .source_catalog import (
    SourceCatalogManager,
    load_source_catalog,
    write_source_catalog,
)
from .store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="asmr-tg-backup")
    parser.add_argument("--config", help="Path to TOML config (default: config.toml)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_config_parser = subparsers.add_parser(
        "init-config",
        help="Create a private configuration from the packaged example",
    )
    init_config_parser.add_argument(
        "--output",
        required=True,
        help="Destination path; existing files are never overwritten",
    )

    setup_parser = subparsers.add_parser(
        "setup",
        help="Interactively create a ready-to-run private configuration",
    )
    _add_late_config(setup_parser)

    service_parser = subparsers.add_parser(
        "service",
        help="Install or uninstall the systemd user service",
    )
    service_subparsers = service_parser.add_subparsers(
        dest="service_action",
        required=True,
    )
    service_install_parser = service_subparsers.add_parser(
        "install",
        help="Install, enable, and start the user service",
    )
    _add_late_config(service_install_parser)
    service_subparsers.add_parser(
        "uninstall",
        help="Stop and remove the user service without deleting application data",
    )

    init_parser = subparsers.add_parser("init", help="Create data directories and initialize SQLite")
    _add_late_config(init_parser)

    run_parser = subparsers.add_parser("run", help="Run continuous polling worker")
    _add_late_config(run_parser)
    run_parser.set_defaults(command="run")

    poll_parser = subparsers.add_parser("poll", help="Run one poll cycle")
    _add_late_config(poll_parser)
    poll_parser.add_argument("--once", action="store_true", help="Accepted for readability; poll already runs once")
    poll_parser.add_argument("--no-process", action="store_true", help="Only fetch sources and enqueue items")

    process_parser = subparsers.add_parser("process", help="Process queued/downloaded items without fetching sources")
    _add_late_config(process_parser)
    process_parser.set_defaults(command="process")

    status_parser = subparsers.add_parser("status", help="Print queue status")
    _add_late_config(status_parser)
    status_parser.add_argument("--limit", type=int, default=20)

    enqueue_parser = subparsers.add_parser("enqueue", help="Manually enqueue one YouTube URL")
    _add_late_config(enqueue_parser)
    enqueue_parser.add_argument("url")
    enqueue_parser.add_argument("--origin-id", dest="feed_id", default="manual")
    enqueue_parser.add_argument("--origin-name", dest="feed_name", default="Manual")
    enqueue_parser.add_argument("--title")

    sources_parser = subparsers.add_parser(
        "sources",
        help="Inspect, validate, and apply the editable source catalog",
    )
    _add_late_config(sources_parser)
    sources_subparsers = sources_parser.add_subparsers(
        dest="sources_action",
        required=True,
    )

    sources_path_parser = sources_subparsers.add_parser(
        "path",
        help="Print the canonical source catalog path",
    )
    _add_late_config(sources_path_parser)

    sources_list_parser = sources_subparsers.add_parser(
        "list",
        help="List the source filter and every configured source field",
    )
    _add_late_config(sources_list_parser)

    sources_validate_parser = sources_subparsers.add_parser(
        "validate",
        help="Validate the canonical catalog or another catalog file",
    )
    _add_late_config(sources_validate_parser)
    sources_validate_parser.add_argument("--file", help="Catalog file to validate")

    sources_apply_parser = sources_subparsers.add_parser(
        "apply",
        help="Apply the canonical catalog or atomically replace it from a file",
    )
    _add_late_config(sources_apply_parser)
    sources_apply_parser.add_argument("--file", help="Validated catalog to make canonical")

    sources_export_parser = sources_subparsers.add_parser(
        "export",
        help="Export a private snapshot of the canonical catalog",
    )
    _add_late_config(sources_export_parser)
    sources_export_parser.add_argument("--output", required=True, help="Snapshot path")

    sources_migrate_parser = sources_subparsers.add_parser(
        "migrate",
        help="Create the catalog from existing database or legacy TOML sources",
    )
    _add_late_config(sources_migrate_parser)

    extensions_parser = subparsers.add_parser(
        "extensions",
        help="Inspect and validate installed extension packages",
    )
    _add_late_config(extensions_parser)
    extensions_subparsers = extensions_parser.add_subparsers(
        dest="extensions_action",
        required=True,
    )
    extensions_list_parser = extensions_subparsers.add_parser(
        "list",
        help="List installed and configured extensions without importing them",
    )
    _add_late_config(extensions_list_parser)
    extensions_doctor_parser = extensions_subparsers.add_parser(
        "doctor",
        help="Load enabled extensions and validate the composed runtime",
    )
    _add_late_config(extensions_doctor_parser)
    extensions_enable_parser = extensions_subparsers.add_parser(
        "enable",
        help="Install, configure, validate, and activate one trusted extension",
    )
    _add_late_config(extensions_enable_parser)
    extensions_enable_parser.add_argument(
        "extension",
        help="Trusted short name, such as proxy-router or niconico-origin",
    )
    extensions_enable_parser.add_argument(
        "--reconfigure",
        action="store_true",
        help="Run the extension's setup again even when it is already enabled",
    )
    extensions_enable_parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Do not restart a matching managed user service",
    )

    args = parser.parse_args(argv)
    if args.command == "init-config":
        output_path = Path(args.output).expanduser()
        try:
            _write_initial_config(output_path)
        except _ConfigAlreadyExistsError:
            parser.error(f"refusing to overwrite existing config: {output_path}")
        except OSError as exc:
            parser.error(f"could not create config at {output_path}: {exc}")
        print(f"created {output_path}")
        return 0

    if args.command == "setup":
        output_path = Path(args.config or default_config_path()).expanduser()
        try:
            result = run_interactive_setup(output_path)
        except (EOFError, KeyboardInterrupt):
            print("\nsetup cancelled", file=sys.stderr)
            return 130
        except SetupError as exc:
            parser.error(str(exc))

        print(f"created private config {result.config_path}")
        print(f"initialized {result.db_path}")
        print(f"initialized source catalog {result.sources_path}")
        if result.local_service_unit:
            print(f"enabled user service {result.local_service_unit}")
            print(
                "important: setup did not call Telegram cloud logOut; "
                "migrate an already-used bot before the first local run: "
                "https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server"
            )
        print(
            "next: asmr-tg-backup service install --config "
            f"{shlex.quote(str(result.config_path))}"
        )
        print(
            "foreground: asmr-tg-backup run --config "
            f"{shlex.quote(str(result.config_path))}"
        )
        print("then send /panel to the bot to add a YouTube or Twitch source")
        return 0

    if args.command == "service":
        try:
            if args.service_action == "install":
                result = install_application_service(
                    Path(args.config or default_config_path()),
                )
                print(f"installed and started {APPLICATION_UNIT}")
                print(f"unit: {result.unit_path}")
                print(f"config: {result.config_path}")
                print(f"optional environment file: {result.environment_path}")
                print(f"enabled boot-time user services for {result.user_name}")
                print("uninstall: asmr-tg-backup service uninstall")
                return 0
            if args.service_action == "uninstall":
                removed = uninstall_application_service()
                if removed is None:
                    print(f"{APPLICATION_UNIT} is not installed")
                else:
                    print(f"stopped and removed {APPLICATION_UNIT}")
                    print("configuration, environment, database, and downloads were kept")
                return 0
        except SetupError as exc:
            parser.error(str(exc))

    if args.command == "extensions":
        try:
            extension_config_path = _extension_command_config_path(args.config)
            if args.extensions_action == "enable":
                result = enable_extension(
                    args.extension,
                    extension_config_path,
                    reconfigure=args.reconfigure,
                    restart_service=not args.no_restart,
                )
                if result.already_enabled:
                    print(
                        f"extension already enabled and healthy: "
                        f"{result.extension.slug}"
                    )
                else:
                    print(f"enabled extension: {result.extension.slug}")
                print(f"id: {result.extension.extension_id}")
                print(
                    f"package: {result.extension.distribution} "
                    f"{result.extension.version}"
                )
                if result.config_path is not None:
                    print(f"private config: {result.config_path}")
                if result.service_restarted:
                    print(f"restarted {APPLICATION_UNIT}")
                else:
                    print("service restart: not needed")
                for suggestion in result.suggested_origins:
                    print(
                        "optional source: "
                        f"{suggestion.provider}/{suggestion.kind} "
                        f"{suggestion.external_id!r}; add it to sources.toml"
                    )
                return 0

            config = load_config(extension_config_path)
            _configure_logging(config.app.log_level)
            if args.extensions_action == "list":
                host = ExtensionHost(
                    config.extensions,
                    data_dir=config.app.data_dir,
                    logger=logging.getLogger("asmr_tg_backup"),
                )
                installed = {item.id: item for item in host.installed()}
                all_ids = sorted(
                    set(installed)
                    | set(config.extensions.enabled)
                    | {item.extension_id for item in TRUSTED_EXTENSIONS}
                )
                print(f"extension API level: {EXTENSION_API_LEVEL}")
                print(f"extensions: {len(all_ids)}")
                for extension_id in all_ids:
                    item = installed.get(extension_id)
                    print(f"- id: {extension_id}")
                    print(f"  installed: {str(item is not None).lower()}")
                    print(
                        "  enabled: "
                        + str(extension_id in config.extensions.enabled).lower()
                    )
                    trusted = trusted_extension_by_id(extension_id)
                    if trusted is not None:
                        print(f"  trusted_slug: {trusted.slug}")
                    if item is not None:
                        print(f"  distribution: {item.distribution}")
                        print(f"  version: {item.version}")
                return 0
            if args.extensions_action == "doctor":
                doctor_extension_runtime(config)
                print(
                    f"extension runtime healthy: {len(config.extensions.enabled)} "
                    f"enabled, API level {EXTENSION_API_LEVEL}"
                )
                return 0
            raise ValueError(f"unknown extensions command: {args.extensions_action}")
        except ExtensionManagementError as exc:
            print(f"extension operation failed: {exc}", file=sys.stderr)
            return 1
        except (ExtensionError, OSError, TypeError, ValueError) as exc:
            parser.error(str(exc))
        except (EOFError, KeyboardInterrupt):
            print("\nextension setup cancelled", file=sys.stderr)
            return 130

    if args.command == "sources":
        try:
            config = load_config(args.config or "config.toml")
            _configure_logging(config.app.log_level)

            if args.sources_action == "path":
                print(config.sources.path)
                return 0
            runtime = build_runtime(config) if hasattr(config, "extensions") else None
            providers = (
                runtime.providers
                if runtime is not None
                and isinstance(runtime.providers, SourceProviderCatalog)
                else None
            )
            if args.sources_action == "validate":
                source_path = (
                    Path(args.file).expanduser()
                    if args.file
                    else config.sources.path
                )
                catalog = _load_catalog(source_path, providers)
                print(
                    f"valid source catalog: {source_path} "
                    f"({len(catalog.origins)} sources)"
                )
                return 0
            if args.sources_action == "list":
                catalog = _load_catalog(config.sources.path, providers)
                _print_sources(
                    catalog.version,
                    catalog.source_filter,
                    catalog.origins,
                )
                return 0
            if args.sources_action == "export":
                catalog = _load_catalog(config.sources.path, providers)
                output_path = Path(args.output).expanduser()
                _write_catalog(output_path, catalog, providers)
                print(f"exported source catalog to {output_path}")
                return 0

            store = Store(config.db_path)
            try:
                store.initialize()
                manager = SourceCatalogManager(
                    config.sources.path,
                    store,
                    config.app.max_attempts,
                    providers=providers,
                )
                return _run_sources_command(args, config, manager)
            finally:
                store.close()
        except (ExtensionError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
            parser.error(str(exc))

    config = load_config(args.config or "config.toml")
    _configure_logging(config.app.log_level)

    service = BackupService(config)
    try:
        if args.command == "init":
            service.initialize()
            print(f"initialized {config.db_path}")
            return 0
        if args.command == "run":
            previous_handlers: dict[signal.Signals, object] = {}

            def request_stop(_signum, _frame) -> None:
                service.stop()

            for sig in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, request_stop)
            try:
                service.run_forever()
            finally:
                for sig, handler in previous_handlers.items():
                    signal.signal(sig, handler)
            return 0
        if args.command == "poll":
            service.poll_once(process=not args.no_process)
            return 0
        if args.command == "process":
            service.initialize()
            service.process_pending()
            return 0
        if args.command == "status":
            service.initialize()
            _print_status(config, args.limit)
            return 0
        if args.command == "enqueue":
            service.initialize()
            video_id = service.store.enqueue_manual(
                args.url,
                args.feed_id,
                args.feed_name,
                args.title,
            )
            print(f"enqueued {video_id}")
            return 0
        parser.error("unknown command")
        return 2
    finally:
        service.close()


def _add_late_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=argparse.SUPPRESS, help="Path to TOML config")


def _extension_command_config_path(value: str | None) -> Path:
    if value:
        return Path(value).expanduser()
    local = Path("config.toml")
    if local.is_file():
        return local
    return default_config_path()


class _ConfigAlreadyExistsError(RuntimeError):
    pass


def _write_initial_config(output_path: Path) -> None:
    template = resources.files("ytb_tg_backup").joinpath("config.example.toml").read_bytes()

    _write_private_file(output_path, template)


def _write_private_file(output_path: Path, content: bytes) -> None:

    previous_umask = os.umask(0o077)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    finally:
        os.umask(previous_umask)

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(output_path, flags, 0o600)
    except FileExistsError as exc:
        raise _ConfigAlreadyExistsError from exc

    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        output_path.chmod(0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            output_path.unlink()
        except OSError:
            pass
        raise


def _run_sources_command(args, config, manager: SourceCatalogManager) -> int:
    if args.sources_action == "apply":
        if args.file:
            source_path = Path(args.file).expanduser()
            providers = (
                manager.providers
                if isinstance(manager.providers, SourceProviderCatalog)
                else None
            )
            catalog = _load_catalog(source_path, providers)
            catalog = manager.replace(catalog)
        else:
            catalog = manager.apply()
        print(f"applied {len(catalog.origins)} sources from {manager.path}")
        return 0

    if args.sources_action == "migrate":
        catalog_existed = manager.path.exists()
        catalog = manager.ensure(
            legacy_origins=config.origins,
            legacy_declared=config.legacy_sources_declared,
        )
        if catalog_existed and config.legacy_sources_declared:
            print(
                "warning: legacy source declarations in config.toml were ignored "
                "because sources.toml already exists",
                file=sys.stderr,
            )
        print(f"source catalog ready at {manager.path} ({len(catalog.origins)} sources)")
        return 0

    raise ValueError(f"unknown sources command: {args.sources_action}")


def _load_catalog(path: Path, providers: SourceProviderCatalog | None):
    if providers is None:
        return load_source_catalog(path)
    return load_source_catalog(path, providers)


def _write_catalog(path: Path, catalog, providers: SourceProviderCatalog | None) -> None:
    if providers is None:
        write_source_catalog(path, catalog)
        return
    write_source_catalog(path, catalog, providers)


def _print_sources(version: int, source_filter: str, origins) -> None:
    print(f"version: {version}")
    print(f"source_filter: {source_filter or '<off>'}")
    print(f"sources: {len(origins)}")
    for origin in origins:
        print(f"- id: {origin.id}")
        print(f"  provider: {origin.provider}")
        print(f"  kind: {origin.kind}")
        print(f"  name: {origin.name}")
        print(f"  external_id: {origin.external_id}")
        print(f"  enabled: {str(origin.enabled).lower()}")
        print(f"  bootstrap: {origin.bootstrap}")
        print(f"  credential_ref: {origin.credential_ref or '<none>'}")
        print(
            "  options: "
            + json.dumps(origin.options, ensure_ascii=False, sort_keys=True)
        )


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _print_status(config, limit: int) -> None:
    store = Store(config.db_path)
    store.initialize()
    counts = store.counts_by_status()
    if counts:
        print("counts:")
        for status, count in sorted(counts.items()):
            print(f"  {status}: {count}")
    else:
        print("counts: none")
    print("recent:")
    for row in store.list_recent(limit):
        error = f" error={row['last_error']}" if row["last_error"] else ""
        print(f"  {row['status']:13} {row['video_id']} {row['title']}{error}")
    store.close()
