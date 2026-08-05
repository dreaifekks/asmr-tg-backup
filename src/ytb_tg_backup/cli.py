from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading

from .config import load_config
from .service import BackupService
from .store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="asmr-tg-backup")
    parser.add_argument("--config", default="config.toml", help="Path to TOML config")
    subparsers = parser.add_subparsers(dest="command", required=True)

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

    dev_parser = subparsers.add_parser(
        "dev",
        help="Run isolated development-only tools",
    )
    dev_tools = dev_parser.add_subparsers(dest="dev_tool", required=True)
    membership_parser = dev_tools.add_parser(
        "youtube-membership",
        help="Observe anonymous YouTube membership metadata without downloads",
    )
    membership_commands = membership_parser.add_subparsers(
        dest="dev_action",
        required=True,
    )
    for action in ("once", "run", "status"):
        action_parser = membership_commands.add_parser(action)
        _add_late_config(action_parser)
        if action == "status":
            action_parser.add_argument("--limit", type=int, default=20)

    args = parser.parse_args(argv)
    config = load_config(args.config)
    _configure_logging(config.app.log_level)

    if args.command == "dev":
        if args.dev_tool != "youtube-membership":
            parser.error("unknown dev tool")
        if (
            args.dev_action in {"once", "run"}
            and not config.dev.youtube_membership.enabled
        ):
            parser.error(
                "dev.youtube_membership.enabled=true is required for this command"
            )
        return _run_dev_youtube_membership(config, args)

    service = BackupService(config)
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
        video_id = service.store.enqueue_manual(args.url, args.feed_id, args.feed_name, args.title)
        print(f"enqueued {video_id}")
        return 0
    parser.error("unknown command")
    return 2


def _add_late_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=argparse.SUPPRESS, help="Path to TOML config")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _run_dev_youtube_membership(config, args) -> int:
    # This command deliberately does not construct BackupService: the dev
    # runner owns separate state and never enters the production Store or
    # Downloader paths.
    from .dev.youtube_membership import YoutubeMembershipDevRunner

    stop_event = threading.Event()
    runner = YoutubeMembershipDevRunner(config, stop_event=stop_event)
    try:
        if args.dev_action == "once":
            print(json.dumps(runner.run_once(), ensure_ascii=False, indent=2))
            return 0
        if args.dev_action == "status":
            print(
                json.dumps(
                    runner.status(limit=max(1, args.limit)),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.dev_action == "run":
            previous_handlers: dict[signal.Signals, object] = {}

            def request_stop(_signum, _frame) -> None:
                stop_event.set()

            for sig in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, request_stop)
            try:
                runner.run_forever()
            finally:
                for sig, handler in previous_handlers.items():
                    signal.signal(sig, handler)
            return 0
        raise ValueError(f"unsupported dev action: {args.dev_action}")
    finally:
        runner.close()


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
