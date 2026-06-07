from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config
from .service import BackupService
from .store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ytb-tg-backup")
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

    args = parser.parse_args(argv)
    config = load_config(args.config)
    _configure_logging(config.app.log_level)

    service = BackupService(config)
    if args.command == "init":
        service.initialize()
        print(f"initialized {config.db_path}")
        return 0
    if args.command == "run":
        service.run_forever()
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
