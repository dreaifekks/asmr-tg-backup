#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from ytb_tg_backup.config import load_config
from ytb_tg_backup.proxy import build_proxy_env, build_url_opener
from ytb_tg_backup.store import Store, now_iso
from ytb_tg_backup.youtube import is_channel_id, resolve_channel_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve DB subscription channel refs to UC channel ids.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    store = Store(config.db_path)
    store.initialize()
    rows = store.conn.execute("SELECT id, channel_id FROM subscriptions ORDER BY id").fetchall()
    changed: list[dict[str, str]] = []
    already_uc: list[dict[str, str]] = []
    now = now_iso()
    proxy_url = config.proxy.url if config.proxy.sources else ""
    opener = build_url_opener(proxy_url) if proxy_url else None
    subprocess_env = build_proxy_env(proxy_url)

    for row in rows:
        sub_id = str(row["id"])
        old = str(row["channel_id"])
        if is_channel_id(old):
            already_uc.append({"id": sub_id, "channel_id": old})
            continue
        if proxy_url:
            new = resolve_channel_id(
                old,
                config.download.yt_dlp,
                opener=opener,
                subprocess_env=subprocess_env,
            )
        else:
            new = resolve_channel_id(old, config.download.yt_dlp)
        if not args.dry_run:
            store.conn.execute(
                "UPDATE subscriptions SET channel_id = ?, updated_at = ? WHERE id = ?",
                (new, now, sub_id),
            )
        changed.append({"id": sub_id, "old": old, "new": new})

    if not args.dry_run:
        store.conn.commit()
    print(json.dumps({"changed": changed, "already_uc": already_uc, "dry_run": args.dry_run}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
