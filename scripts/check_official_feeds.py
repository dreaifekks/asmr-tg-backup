#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from ytb_tg_backup.config import ChannelConfig, expand_channel_feeds, load_config
from ytb_tg_backup.feed import fetch_feed, parse_feed
from ytb_tg_backup.store import Store


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch configured official YouTube feeds without writing state.")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    config = load_config(args.config)
    store = Store(config.db_path)
    store.initialize()
    subscriptions = store.list_subscriptions()
    dynamic_channels = [
        ChannelConfig(
            id=sub.id,
            name=sub.name,
            channel_id=sub.channel_id,
            routes=sub.routes,
            enabled=sub.enabled,
        )
        for sub in subscriptions
    ]
    feeds = list(config.feeds)
    feeds.extend(expand_channel_feeds(config.rsshub, dynamic_channels, prefix="db:"))

    result = []
    for feed in feeds:
        if not feed.enabled:
            continue
        xml = fetch_feed(feed.url)
        entries = parse_feed(xml, feed.id, feed.name)
        result.append(
            {
                "id": feed.id,
                "entries": len(entries),
                "first_video_id": entries[0].video_id if entries else None,
                "url": feed.url,
            }
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
