#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from ytb_tg_backup.config import load_config
from ytb_tg_backup.store import Store


def main() -> int:
    parser = argparse.ArgumentParser(description="Mark initial official-feed seed rows as ignored.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--first-seen-at-or-after", required=True)
    args = parser.parse_args()

    config = load_config(args.config)
    store = Store(config.db_path)
    store.initialize()
    cursor = store.conn.execute(
        """
        UPDATE videos
        SET status = 'ignored',
            last_error = 'initial official feed seed ignored',
            next_retry_at = NULL
        WHERE feed_id LIKE 'db:%'
          AND status IN ('seen', 'waiting_ready')
          AND file_path IS NULL
          AND first_seen_at >= ?
        """,
        (args.first_seen_at_or_after,),
    )
    store.conn.commit()
    print(json.dumps({"ignored_seed_rows": cursor.rowcount}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
