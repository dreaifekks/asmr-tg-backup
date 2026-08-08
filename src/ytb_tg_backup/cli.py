from __future__ import annotations

import argparse
from importlib import resources
import logging
import os
from pathlib import Path
import shlex
import signal
import sys

from . import __version__
from .config import load_config
from .service import BackupService
from .setup import SetupError, default_config_path, run_interactive_setup
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

    init_parser = subparsers.add_parser("init", help="Create data directories and initialize SQLite")
    _add_late_config(init_parser)

    run_parser = subparsers.add_parser("run", help="Run continuous polling worker")
    _add_late_config(run_parser)
    run_parser.set_defaults(command="run")

    poll_parser = subparsers.add_parser("poll", help="Run one poll cycle")
    _add_late_config(poll_parser)
    poll_parser.add_argument("--once", action="store_true", help="Accepted for readability; poll already runs once")
    poll_parser.add_argument("--no-process", action="store_true", help="Only fetch feeds and enqueue items")

    process_parser = subparsers.add_parser("process", help="Process queued/downloaded items without fetching feeds")
    _add_late_config(process_parser)
    process_parser.set_defaults(command="process")

    status_parser = subparsers.add_parser("status", help="Print queue status")
    _add_late_config(status_parser)
    status_parser.add_argument("--limit", type=int, default=20)

    enqueue_parser = subparsers.add_parser("enqueue", help="Manually enqueue one YouTube URL")
    _add_late_config(enqueue_parser)
    enqueue_parser.add_argument("url")
    enqueue_parser.add_argument("--feed-id", default="manual")
    enqueue_parser.add_argument("--feed-name", default="Manual")
    enqueue_parser.add_argument("--title")

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
        if result.local_service_unit:
            print(f"enabled user service {result.local_service_unit}")
            print(
                "important: setup did not call Telegram cloud logOut; "
                "migrate an already-used bot before the first local run: "
                "https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server"
            )
        print(
            "next: asmr-tg-backup run --config "
            f"{shlex.quote(str(result.config_path))}"
        )
        print("then send /panel to the bot to add a YouTube or Twitch source")
        return 0

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
