#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sys
import tomllib

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))
sys.path.insert(0, str(SCRIPT_DIR))

from migrate_config_to_channels import render_toml  # noqa: E402
from ytb_tg_backup.config import load_config  # noqa: E402
from ytb_tg_backup.store import Store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("--created-by", default="config-migration")
    args = parser.parse_args()

    path = Path(args.config).expanduser()
    raw = tomllib.loads(path.read_text())
    config = load_config(path)
    store = Store(config.db_path)
    store.initialize()

    moved = 0
    for channel in config.channels:
        store.upsert_subscription(
            sub_id=channel.id,
            name=channel.name,
            channel_id=channel.channel_id,
            routes=channel.routes,
            created_by=args.created_by,
        )
        moved += 1

    if moved:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.bak.move-channels.{timestamp}")
        shutil.copy2(path, backup)
        raw["channels"] = []
        path.write_text(render_toml(raw), encoding="utf-8")
        print(f"moved {moved} channels into {config.db_path}")
        print(f"backup {backup}")
    else:
        print("no config channels to move")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
