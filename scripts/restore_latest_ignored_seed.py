#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from ytb_tg_backup.config import load_config
from ytb_tg_backup.store import Store


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore the latest ignored initial seed row per feed to seen.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    store = Store(config.db_path)
    store.initialize()
    feed_rows = store.conn.execute(
        """
        SELECT DISTINCT feed_id
        FROM videos
        WHERE status = 'ignored'
          AND last_error LIKE 'initial %feed seed ignored%'
        ORDER BY feed_id
        """
    ).fetchall()

    restored: list[dict[str, str]] = []
    for feed_row in feed_rows:
        feed_id = str(feed_row["feed_id"])
        row = store.conn.execute(
            """
            SELECT video_id, title, published_at, updated_at, first_seen_at
            FROM videos
            WHERE feed_id = ?
              AND status = 'ignored'
              AND last_error LIKE 'initial %feed seed ignored%'
            ORDER BY COALESCE(published_at, updated_at, first_seen_at) DESC
            LIMIT 1
            """,
            (feed_id,),
        ).fetchone()
        if row is None:
            continue
        if not args.dry_run:
            store.conn.execute(
                """
                UPDATE videos
                SET status = 'seen',
                    last_error = NULL,
                    next_retry_at = NULL
                WHERE video_id = ?
                """,
                (row["video_id"],),
            )
        restored.append(
            {
                "feed_id": feed_id,
                "video_id": str(row["video_id"]),
                "title": str(row["title"]),
                "published_at": str(row["published_at"] or ""),
            }
        )

    if not args.dry_run:
        store.conn.commit()
    print(json.dumps({"restored": restored, "dry_run": args.dry_run}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
