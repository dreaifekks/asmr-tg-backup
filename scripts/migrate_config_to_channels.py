#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import tomllib
from urllib.parse import unquote, urlparse


AUDIO_FORMAT = "bestaudio/best"
OLD_VIDEO_FORMATS = {
    "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/best[height<=1080]/best",
    "best[height<=720][ext=mp4]/best[height<=720]/best",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("--ffmpeg", default=None)
    args = parser.parse_args()

    path = Path(args.config).expanduser()
    raw = tomllib.loads(path.read_text())
    migrated = migrate(raw, ffmpeg=args.ffmpeg)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.bak.{timestamp}")
    shutil.copy2(path, backup)
    path.write_text(render_toml(migrated), encoding="utf-8")
    print(f"wrote {path}")
    print(f"backup {backup}")
    return 0


def migrate(raw: dict, *, ffmpeg: str | None) -> dict:
    rsshub = dict(raw.get("rsshub") or {})
    channels = [dict(item) for item in raw.get("channels", [])]
    retained_feeds = []
    converted_by_key: dict[tuple[str, str], dict] = {}

    for feed in raw.get("feeds", []):
        converted = convert_feed(feed)
        if converted is None:
            retained_feeds.append(dict(feed))
            continue
        if "base_url" not in rsshub:
            rsshub["base_url"] = converted["base_url"]
        key = (converted["name"], converted["channel_id"])
        existing = converted_by_key.setdefault(
            key,
            {
                "id": converted["id"],
                "name": converted["name"],
                "channel_id": converted["channel_id"],
                "routes": [],
                "enabled": converted["enabled"],
            },
        )
        if converted["route"] not in existing["routes"]:
            existing["routes"].append(converted["route"])
        existing["enabled"] = bool(existing["enabled"] or converted["enabled"])

    channels.extend(converted_by_key.values())

    download = dict(raw.get("download") or {})
    if ffmpeg:
        download["ffmpeg"] = ffmpeg
        if download.get("format") in {None, "", *OLD_VIDEO_FORMATS}:
            download["format"] = AUDIO_FORMAT
        download["merge_output_format"] = ""
        download["extract_audio"] = True
        download.setdefault("audio_format", "m4a")
        download.setdefault("audio_quality", "0")

    telegram = dict(raw.get("telegram") or {})
    telegram.setdefault("media_type", "audio")

    control = dict(raw.get("control") or {})
    control.setdefault("enabled", False)
    control.setdefault("poll_interval_seconds", 10)
    control.setdefault("delete_webhook_on_startup", True)
    control.setdefault("default_routes", ["live"])
    control.setdefault("allowed_user_ids", [])
    control.setdefault("allowed_chat_ids", [])
    control.setdefault("allowed_message_thread_ids", [])

    return {
        "app": dict(raw.get("app") or {}),
        "rsshub": rsshub or {"base_url": "https://rss.dreaife.tokyo"},
        "channels": channels,
        "feeds": retained_feeds,
        "download": download,
        "telegram": telegram,
        "control": control,
    }


def convert_feed(feed: dict) -> dict | None:
    url = str(feed.get("url", ""))
    parsed = urlparse(url)
    parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 3 or parts[0] != "youtube":
        return None
    route = parts[1]
    if route not in {"channel", "live", "user"}:
        return None
    channel_id = "/".join(parts[2:])
    base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "https://rss.dreaife.tokyo"
    feed_id = str(feed.get("id") or channel_id)
    name = str(feed.get("name") or feed_id)
    return {
        "base_url": base_url.rstrip("/"),
        "id": feed_id,
        "name": name,
        "channel_id": channel_id,
        "route": route,
        "enabled": bool(feed.get("enabled", True)),
    }


def render_toml(data: dict) -> str:
    lines: list[str] = []
    render_table(lines, "app", data.get("app", {}))
    render_table(lines, "rsshub", data.get("rsshub", {}))
    render_array(lines, "channels", data.get("channels", []))
    render_array(lines, "feeds", data.get("feeds", []))
    render_table(lines, "download", data.get("download", {}))
    render_table(lines, "telegram", data.get("telegram", {}))
    render_table(lines, "control", data.get("control", {}))
    return "\n".join(lines).rstrip() + "\n"


def render_table(lines: list[str], name: str, values: dict) -> None:
    if not values:
        return
    if lines:
        lines.append("")
    lines.append(f"[{name}]")
    for key, value in values.items():
        lines.append(f"{key} = {format_value(value)}")


def render_array(lines: list[str], name: str, items: list[dict]) -> None:
    for item in items:
        if lines:
            lines.append("")
        lines.append(f"[[{name}]]")
        for key, value in item.items():
            lines.append(f"{key} = {format_value(value)}")


def format_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(format_value(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


if __name__ == "__main__":
    raise SystemExit(main())
