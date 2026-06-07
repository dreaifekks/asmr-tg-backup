from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3

from .feed import FeedEntry


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS videos (
  video_id TEXT PRIMARY KEY,
  feed_id TEXT NOT NULL,
  feed_name TEXT NOT NULL,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  published_at TEXT,
  updated_at TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_retry_at TEXT,
  file_path TEXT,
  file_size INTEGER,
  telegram_message_id INTEGER,
  last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_videos_status_retry
ON videos(status, next_retry_at, first_seen_at);

CREATE TABLE IF NOT EXISTS subscriptions (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  routes_json TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  created_by TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Subscription:
    id: str
    name: str
    channel_id: str
    routes: list[str]
    enabled: bool


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def initialize(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def has_entries_for_feed(self, feed_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM videos WHERE feed_id = ? LIMIT 1", (feed_id,)).fetchone()
        return row is not None

    def upsert_entry(self, entry: FeedEntry, *, status: str = "seen", last_error: str | None = None) -> bool:
        now = now_iso()
        existing = self.conn.execute("SELECT video_id FROM videos WHERE video_id = ?", (entry.video_id,)).fetchone()
        if existing:
            self.conn.execute(
                """
                UPDATE videos
                SET feed_id = ?, feed_name = ?, title = ?, url = ?, published_at = ?,
                    updated_at = ?, last_seen_at = ?
                WHERE video_id = ?
                """,
                (
                    entry.feed_id,
                    entry.feed_name,
                    entry.title,
                    entry.url,
                    entry.published_at,
                    entry.updated_at,
                    now,
                    entry.video_id,
                ),
            )
            self.conn.commit()
            return False

        self.conn.execute(
            """
            INSERT INTO videos (
              video_id, feed_id, feed_name, title, url, published_at, updated_at,
              first_seen_at, last_seen_at, status, last_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.video_id,
                entry.feed_id,
                entry.feed_name,
                entry.title,
                entry.url,
                entry.published_at,
                entry.updated_at,
                now,
                now,
                status,
                last_error,
            ),
        )
        self.conn.commit()
        return True

    def enqueue_manual(self, url: str, feed_id: str = "manual", feed_name: str = "Manual", title: str | None = None) -> str:
        from .feed import extract_video_id

        video_id = extract_video_id(url)
        if not video_id:
            raise ValueError(f"Could not extract YouTube video id from {url}")
        self.upsert_entry(
            FeedEntry(
                feed_id=feed_id,
                feed_name=feed_name,
                video_id=video_id,
                title=title or video_id,
                url=url,
                published_at=None,
                updated_at=None,
            )
        )
        return video_id

    def upsert_subscription(
        self,
        *,
        sub_id: str,
        name: str,
        channel_id: str,
        routes: list[str],
        created_by: str | None,
    ) -> bool:
        now = now_iso()
        clean_routes = [route.strip("/") for route in routes if route.strip("/")]
        if not clean_routes:
            clean_routes = ["live"]
        existing = self.conn.execute("SELECT id FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
        if existing:
            self.conn.execute(
                """
                UPDATE subscriptions
                SET name = ?, channel_id = ?, routes_json = ?, enabled = 1, updated_at = ?
                WHERE id = ?
                """,
                (name, channel_id, json.dumps(clean_routes), now, sub_id),
            )
            self.conn.commit()
            return False
        self.conn.execute(
            """
            INSERT INTO subscriptions (
              id, name, channel_id, routes_json, enabled, created_at, created_by, updated_at
            )
            VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (sub_id, name, channel_id, json.dumps(clean_routes), now, created_by, now),
        )
        self.conn.commit()
        return True

    def delete_subscription(self, sub_id: str) -> bool:
        cursor = self.conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
        self.conn.commit()
        return cursor.rowcount > 0

    def list_subscriptions(self) -> list[Subscription]:
        rows = self.conn.execute("SELECT * FROM subscriptions ORDER BY id ASC").fetchall()
        return [
            Subscription(
                id=str(row["id"]),
                name=str(row["name"]),
                channel_id=str(row["channel_id"]),
                routes=[str(route) for route in json.loads(str(row["routes_json"]))],
                enabled=bool(row["enabled"]),
            )
            for row in rows
        ]

    def list_pending(self, limit: int, max_attempts: int, include_downloaded: bool) -> list[sqlite3.Row]:
        statuses = ["seen", "waiting_ready", "failed"]
        if include_downloaded:
            statuses.append("downloaded")
        placeholders = ",".join("?" for _ in statuses)
        params: list[object] = [*statuses, now_iso(), max_attempts, limit]
        return list(
            self.conn.execute(
                f"""
                SELECT * FROM videos
                WHERE status IN ({placeholders})
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                  AND attempts < ?
                ORDER BY first_seen_at ASC
                LIMIT ?
                """,
                params,
            )
        )

    def begin_download(self, video_id: str) -> None:
        self.conn.execute(
            """
            UPDATE videos
            SET status = 'downloading', attempts = attempts + 1, last_error = NULL, next_retry_at = NULL
            WHERE video_id = ?
            """,
            (video_id,),
        )
        self.conn.commit()

    def mark_waiting(self, video_id: str, reason: str, retry_seconds: int) -> None:
        self._mark(video_id, "waiting_ready", reason, retry_seconds)

    def update_title(self, video_id: str, title: str | None) -> None:
        if not title:
            return
        self.conn.execute("UPDATE videos SET title = ? WHERE video_id = ?", (title, video_id))
        self.conn.commit()

    def mark_failed(self, video_id: str, error: str, retry_seconds: int) -> None:
        self._mark(video_id, "failed", error, retry_seconds)

    def mark_blocked(self, video_id: str, error: str) -> None:
        self.conn.execute(
            "UPDATE videos SET status = 'blocked', last_error = ?, next_retry_at = NULL WHERE video_id = ?",
            (error, video_id),
        )
        self.conn.commit()

    def mark_downloaded(self, video_id: str, file_path: Path, file_size: int) -> None:
        self.conn.execute(
            """
            UPDATE videos
            SET status = 'downloaded', file_path = ?, file_size = ?, last_error = NULL, next_retry_at = NULL
            WHERE video_id = ?
            """,
            (str(file_path), file_size, video_id),
        )
        self.conn.commit()

    def mark_uploaded(self, video_id: str, message_id: int) -> None:
        self.conn.execute(
            """
            UPDATE videos
            SET status = 'uploaded', telegram_message_id = ?, last_error = NULL, next_retry_at = NULL
            WHERE video_id = ?
            """,
            (message_id, video_id),
        )
        self.conn.commit()

    def list_recent(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM videos ORDER BY first_seen_at DESC LIMIT ?",
                (limit,),
            )
        )

    def counts_by_status(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS count FROM videos GROUP BY status").fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def backup_summary(self) -> dict[str, int]:
        counts = self.counts_by_status()
        file_row = self.conn.execute(
            """
            SELECT COUNT(*) AS count, COALESCE(SUM(file_size), 0) AS bytes
            FROM videos
            WHERE status IN ('downloaded', 'uploaded')
            """
        ).fetchone()
        uploaded = counts.get("uploaded", 0)
        downloaded = counts.get("downloaded", 0)
        return {
            "known": sum(counts.values()),
            "downloaded": downloaded,
            "uploaded": uploaded,
            "backed_up": downloaded + uploaded,
            "ignored": counts.get("ignored", 0),
            "blocked": counts.get("blocked", 0),
            "failed": counts.get("failed", 0),
            "waiting_ready": counts.get("waiting_ready", 0),
            "file_count": int(file_row["count"]),
            "file_bytes": int(file_row["bytes"]),
        }

    def get_bot_offset(self) -> int:
        row = self.conn.execute("SELECT value FROM bot_state WHERE key = 'last_update_id'").fetchone()
        return int(row["value"]) if row else 0

    def set_bot_offset(self, update_id: int) -> None:
        self.conn.execute(
            """
            INSERT INTO bot_state(key, value)
            VALUES ('last_update_id', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(update_id),),
        )
        self.conn.commit()

    def _mark(self, video_id: str, status: str, error: str, retry_seconds: int) -> None:
        self.conn.execute(
            "UPDATE videos SET status = ?, last_error = ?, next_retry_at = ? WHERE video_id = ?",
            (status, error, future_iso(retry_seconds), video_id),
        )
        self.conn.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def future_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def row_dict(row: sqlite3.Row) -> dict[str, object]:
    return dict(row)
