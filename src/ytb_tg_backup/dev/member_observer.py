"""Development-only anonymous YouTube membership metadata observer."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import threading
import time
import tomllib
from typing import Callable, Iterable

from ..feed import fetch_feed, parse_feed


MEMBERSHIP_TITLE_RE = re.compile(
    r"member(?:s|ship)?\s*only|members?-only|メンバー限定|メン限|會員限定|会员限定",
    re.IGNORECASE,
)
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
GLOBAL_CHALLENGE_ERRORS = {"rate_limited", "bot_check"}
CRITICAL_SURFACE_WARNING_ERRORS = {
    "rate_limited",
    "bot_check",
    "login_required",
    "po_token_required",
    "no_formats",
    "members_only_denied",
}
RETRYABLE_PROBE_ERRORS = {
    "timeout",
    "network",
    "rate_limited",
    "bot_check",
    "login_required",
    "po_token_required",
    "tool_missing",
    "extractor_error",
}
TERMINAL_PROBE_ERRORS = {"removed", "unavailable", "private", "no_formats"}


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS cycles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  success INTEGER,
  channel_count INTEGER NOT NULL,
  surface_errors INTEGER NOT NULL DEFAULT 0,
  item_sightings INTEGER NOT NULL DEFAULT 0,
  probe_count INTEGER NOT NULL DEFAULT 0,
  members_only_count INTEGER NOT NULL DEFAULT 0,
  probe_errors INTEGER NOT NULL DEFAULT 0,
  surface_skips INTEGER NOT NULL DEFAULT 0,
  interrupted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS items (
  channel_id TEXT NOT NULL,
  video_id TEXT NOT NULL,
  channel_name TEXT NOT NULL,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  PRIMARY KEY (channel_id, video_id)
);

CREATE TABLE IF NOT EXISTS surface_items (
  channel_id TEXT NOT NULL,
  video_id TEXT NOT NULL,
  surface TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  PRIMARY KEY (channel_id, video_id, surface)
);

CREATE TABLE IF NOT EXISTS sightings (
  cycle_id INTEGER NOT NULL,
  observed_at TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  video_id TEXT NOT NULL,
  surface TEXT NOT NULL,
  title TEXT NOT NULL,
  position INTEGER,
  availability TEXT,
  live_status TEXT,
  source_timestamp TEXT,
  release_timestamp INTEGER,
  duration_seconds REAL,
  PRIMARY KEY (cycle_id, channel_id, video_id, surface)
);

CREATE TABLE IF NOT EXISTS probes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id INTEGER NOT NULL,
  probed_at TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  video_id TEXT NOT NULL,
  mode TEXT NOT NULL,
  ok INTEGER NOT NULL,
  access_class TEXT NOT NULL,
  availability TEXT,
  live_status TEXT,
  release_timestamp INTEGER,
  atom_seen_this_cycle INTEGER,
  error_kind TEXT,
  error_message TEXT,
  metadata_json TEXT NOT NULL,
  UNIQUE (cycle_id, channel_id, video_id, mode)
);

CREATE TABLE IF NOT EXISTS surface_state (
  channel_id TEXT NOT NULL,
  surface TEXT NOT NULL,
  seeded_at TEXT,
  last_success_at TEXT,
  last_error_at TEXT,
  last_error_kind TEXT,
  last_error_message TEXT,
  PRIMARY KEY (channel_id, surface)
);

CREATE TABLE IF NOT EXISTS surface_runs (
  cycle_id INTEGER NOT NULL,
  observed_at TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  surface TEXT NOT NULL,
  status TEXT NOT NULL,
  item_count INTEGER,
  error_kind TEXT,
  error_message TEXT,
  PRIMARY KEY (cycle_id, channel_id, surface)
);

CREATE TABLE IF NOT EXISTS observer_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS probe_queue (
  channel_id TEXT NOT NULL,
  video_id TEXT NOT NULL,
  mode TEXT NOT NULL,
  queued_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  reason TEXT NOT NULL,
  PRIMARY KEY (channel_id, video_id, mode)
);

CREATE INDEX IF NOT EXISTS idx_surface_items_surface
ON surface_items(surface, first_seen_at);

CREATE INDEX IF NOT EXISTS idx_probes_access
ON probes(access_class, probed_at);

CREATE INDEX IF NOT EXISTS idx_surface_runs_status
ON surface_runs(status, observed_at);

CREATE INDEX IF NOT EXISTS idx_probe_queue_order
ON probe_queue(mode, queued_at);
"""


@dataclass(frozen=True)
class ObservedChannel:
    id: str
    name: str
    channel_id: str
    enabled: bool = True


@dataclass(frozen=True)
class ObserverConfig:
    path: Path
    data_dir: Path
    yt_dlp: str
    poll_interval_seconds: int
    request_timeout_seconds: int
    request_spacing_seconds: float
    tab_limit: int
    seed_probe_keyword_limit_per_channel: int
    anonymous_tabs: list[str]
    anonymous_members_playlist: bool
    stop_at: datetime | None
    log_level: str
    channels: list[ObservedChannel]
    retry_probe_limit_per_channel: int = 5
    pause_hours_on_challenge: int = 6

    @property
    def db_path(self) -> Path:
        return self.data_dir / "member-observer.db"

    @property
    def event_log_path(self) -> Path:
        return self.data_dir / "events.jsonl"


@dataclass(frozen=True)
class SurfaceItem:
    video_id: str
    title: str
    url: str
    position: int | None = None
    availability: str | None = None
    live_status: str | None = None
    source_timestamp: str | None = None
    release_timestamp: int | None = None
    duration_seconds: float | None = None


@dataclass(frozen=True)
class ProbeOutcome:
    ok: bool
    access_class: str
    availability: str | None = None
    live_status: str | None = None
    release_timestamp: int | None = None
    error_kind: str | None = None
    error_message: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


class InspectionError(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def load_observer_config(path: str | Path) -> ObserverConfig:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    observer_raw = raw.get("observer", {})
    data_dir = Path(observer_raw.get("data_dir", "work/member-observer")).expanduser()
    if not data_dir.is_absolute():
        data_dir = config_path.parent / data_dir

    channels = [
        ObservedChannel(
            id=str(item["id"]),
            name=str(item.get("name") or item["id"]),
            channel_id=str(item["channel_id"]),
            enabled=bool(item.get("enabled", True)),
        )
        for item in raw.get("channels", [])
    ]
    if not any(channel.enabled for channel in channels):
        raise ValueError("member observer requires at least one enabled channel")
    enabled_channel_ids = [channel.channel_id for channel in channels if channel.enabled]
    if len(enabled_channel_ids) != len(set(enabled_channel_ids)):
        raise ValueError("member observer channel_id values must be unique")

    anonymous_tabs = _validated_tabs(observer_raw.get("anonymous_tabs", ["streams", "videos"]))
    auth_raw = raw.get("auth", {})
    if not isinstance(auth_raw, dict):
        raise ValueError("auth must be a TOML table when present")
    if bool(auth_raw.get("enabled", False)) or bool(auth_raw.get("extra_args", [])):
        raise ValueError(
            "the dev membership observer is anonymous-only; auth and cookies are not supported"
        )

    yt_dlp = str(observer_raw.get("yt_dlp", "yt-dlp"))
    if not Path(yt_dlp).is_absolute() and "/" in yt_dlp:
        yt_dlp = str((config_path.parent / yt_dlp).resolve())

    return ObserverConfig(
        path=config_path,
        data_dir=data_dir.resolve(),
        yt_dlp=yt_dlp,
        poll_interval_seconds=max(300, int(observer_raw.get("poll_interval_seconds", 1800))),
        request_timeout_seconds=max(30, int(observer_raw.get("request_timeout_seconds", 180))),
        request_spacing_seconds=max(0.0, float(observer_raw.get("request_spacing_seconds", 5))),
        tab_limit=max(1, min(100, int(observer_raw.get("tab_limit", 30)))),
        seed_probe_keyword_limit_per_channel=max(
            0, int(observer_raw.get("seed_probe_keyword_limit_per_channel", 5))
        ),
        anonymous_tabs=anonymous_tabs,
        anonymous_members_playlist=bool(observer_raw.get("anonymous_members_playlist", True)),
        stop_at=_parse_datetime(observer_raw.get("stop_at")),
        log_level=str(observer_raw.get("log_level", "INFO")),
        channels=channels,
        retry_probe_limit_per_channel=max(
            1, int(observer_raw.get("retry_probe_limit_per_channel", 5))
        ),
        pause_hours_on_challenge=max(
            1, int(observer_raw.get("pause_hours_on_challenge", 6))
        ),
    )


def _validated_tabs(raw_tabs: Iterable[object]) -> list[str]:
    allowed = {"videos", "streams"}
    tabs: list[str] = []
    for raw in raw_tabs:
        tab = str(raw).strip().lower()
        if tab not in allowed:
            raise ValueError(f"unsupported YouTube observer tab: {tab}")
        if tab not in tabs:
            tabs.append(tab)
    if not tabs:
        raise ValueError("at least one observer tab is required")
    return tabs


def _parse_datetime(value: object) -> datetime | None:
    if value in {None, ""}:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class ObserverStore:
    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = path
        if read_only:
            self.conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            self.conn.row_factory = sqlite3.Row
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        self.conn = sqlite3.connect(path)
        path.chmod(0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._ensure_schema_columns()
        self.conn.commit()

    def _ensure_schema_columns(self) -> None:
        sighting_columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(sightings)")
        }
        additions = {
            "source_timestamp": "TEXT",
            "release_timestamp": "INTEGER",
            "duration_seconds": "REAL",
        }
        for name, column_type in additions.items():
            if name not in sighting_columns:
                self.conn.execute(f"ALTER TABLE sightings ADD COLUMN {name} {column_type}")
        cycle_columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(cycles)")
        }
        if "interrupted" not in cycle_columns:
            self.conn.execute(
                "ALTER TABLE cycles ADD COLUMN interrupted INTEGER NOT NULL DEFAULT 0"
            )
        for name in ("probe_errors", "surface_skips"):
            if name not in cycle_columns:
                self.conn.execute(
                    f"ALTER TABLE cycles ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
                )
        probe_columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(probes)")
        }
        probe_additions = {
            "release_timestamp": "INTEGER",
            "atom_seen_this_cycle": "INTEGER",
        }
        for name, column_type in probe_additions.items():
            if name not in probe_columns:
                self.conn.execute(f"ALTER TABLE probes ADD COLUMN {name} {column_type}")
        retry_errors = sorted(RETRYABLE_PROBE_ERRORS)
        placeholders = ", ".join("?" for _ in retry_errors)
        self.conn.execute(
            f"""
            INSERT OR IGNORE INTO probe_queue (
              channel_id, video_id, mode, queued_at, updated_at, reason
            )
            SELECT p.channel_id, p.video_id, p.mode, p.probed_at, p.probed_at,
                   COALESCE(p.error_kind, p.live_status, p.access_class)
            FROM probes p
            JOIN (
              SELECT channel_id, video_id, mode, MAX(id) AS latest_id
              FROM probes GROUP BY channel_id, video_id, mode
            ) latest ON latest.latest_id = p.id
            WHERE p.error_kind IN ({placeholders})
               OR p.access_class = 'upcoming'
               OR p.live_status IN ('is_upcoming', 'is_live')
               OR (p.live_status = 'post_live' AND p.ok = 0)
            """,
            retry_errors,
        )

    def close(self) -> None:
        self.conn.close()

    def begin_cycle(self, channel_count: int) -> int:
        cursor = self.conn.execute(
            "INSERT INTO cycles (started_at, channel_count) VALUES (?, ?)",
            (iso_now(), channel_count),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def finish_cycle(self, cycle_id: int, stats: dict[str, int]) -> None:
        self.conn.execute(
            """
            UPDATE cycles
            SET finished_at = ?, success = ?, surface_errors = ?, item_sightings = ?,
                probe_count = ?, members_only_count = ?, probe_errors = ?,
                surface_skips = ?, interrupted = ?
            WHERE id = ?
            """,
            (
                iso_now(),
                int(
                    stats["surface_errors"] == 0
                    and stats["probe_errors"] == 0
                    and stats["surface_skips"] == 0
                    and stats["interrupted"] == 0
                ),
                stats["surface_errors"],
                stats["item_sightings"],
                stats["probe_count"],
                stats["members_only_count"],
                stats["probe_errors"],
                stats["surface_skips"],
                stats["interrupted"],
                cycle_id,
            ),
        )
        self.conn.commit()

    def surface_seeded(self, channel_id: str, surface: str) -> bool:
        row = self.conn.execute(
            "SELECT seeded_at FROM surface_state WHERE channel_id = ? AND surface = ?",
            (channel_id, surface),
        ).fetchone()
        return bool(row and row["seeded_at"])

    def record_surface_success(self, channel_id: str, surface: str, observed_at: str) -> None:
        self.conn.execute(
            """
            INSERT INTO surface_state (
              channel_id, surface, seeded_at, last_success_at,
              last_error_at, last_error_kind, last_error_message
            )
            VALUES (?, ?, ?, ?, NULL, NULL, NULL)
            ON CONFLICT(channel_id, surface) DO UPDATE SET
              seeded_at = COALESCE(surface_state.seeded_at, excluded.seeded_at),
              last_success_at = excluded.last_success_at,
              last_error_at = NULL,
              last_error_kind = NULL,
              last_error_message = NULL
            """,
            (channel_id, surface, observed_at, observed_at),
        )
        self.conn.commit()

    def record_surface_error(
        self,
        channel_id: str,
        surface: str,
        observed_at: str,
        kind: str,
        message: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO surface_state (
              channel_id, surface, last_error_at, last_error_kind, last_error_message
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(channel_id, surface) DO UPDATE SET
              last_error_at = excluded.last_error_at,
              last_error_kind = excluded.last_error_kind,
              last_error_message = excluded.last_error_message
            """,
            (channel_id, surface, observed_at, kind, message),
        )
        self.conn.commit()

    def record_surface_run(
        self,
        *,
        cycle_id: int,
        observed_at: str,
        channel_id: str,
        surface: str,
        status: str,
        item_count: int | None,
        error_kind: str | None = None,
        error_message: str | None = None,
        overwrite: bool = True,
    ) -> bool:
        insert_mode = "INSERT OR REPLACE" if overwrite else "INSERT OR IGNORE"
        cursor = self.conn.execute(
            f"""
            {insert_mode} INTO surface_runs (
              cycle_id, observed_at, channel_id, surface, status, item_count,
              error_kind, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_id,
                observed_at,
                channel_id,
                surface,
                status,
                item_count,
                error_kind,
                error_message,
            ),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_state(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM observer_state WHERE key = ?",
            (key,),
        ).fetchone()
        return str(row["value"]) if row else None

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO observer_state (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    def delete_state(self, key: str) -> None:
        self.conn.execute("DELETE FROM observer_state WHERE key = ?", (key,))
        self.conn.commit()

    def record_item(
        self,
        *,
        cycle_id: int,
        channel: ObservedChannel,
        surface: str,
        item: SurfaceItem,
        observed_at: str,
    ) -> tuple[bool, bool, dict[str, dict[str, object | None]]]:
        item_exists = self.conn.execute(
            "SELECT 1 FROM items WHERE channel_id = ? AND video_id = ?",
            (channel.channel_id, item.video_id),
        ).fetchone()
        surface_exists = self.conn.execute(
            "SELECT 1 FROM surface_items WHERE channel_id = ? AND video_id = ? AND surface = ?",
            (channel.channel_id, item.video_id, surface),
        ).fetchone()
        previous = self.conn.execute(
            """
            SELECT title, availability, live_status, source_timestamp,
                   release_timestamp, duration_seconds
            FROM sightings
            WHERE channel_id = ? AND video_id = ? AND surface = ?
            ORDER BY cycle_id DESC LIMIT 1
            """,
            (channel.channel_id, item.video_id, surface),
        ).fetchone()
        self.conn.execute(
            """
            INSERT INTO items (
              channel_id, video_id, channel_name, title, url, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id, video_id) DO UPDATE SET
              channel_name = excluded.channel_name,
              title = excluded.title,
              url = excluded.url,
              last_seen_at = excluded.last_seen_at
            """,
            (
                channel.channel_id,
                item.video_id,
                channel.name,
                item.title,
                item.url,
                observed_at,
                observed_at,
            ),
        )
        self.conn.execute(
            """
            INSERT INTO surface_items (
              channel_id, video_id, surface, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(channel_id, video_id, surface) DO UPDATE SET
              last_seen_at = excluded.last_seen_at
            """,
            (channel.channel_id, item.video_id, surface, observed_at, observed_at),
        )
        self.conn.execute(
            """
            INSERT OR REPLACE INTO sightings (
              cycle_id, observed_at, channel_id, video_id, surface, title,
              position, availability, live_status, source_timestamp,
              release_timestamp, duration_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_id,
                observed_at,
                channel.channel_id,
                item.video_id,
                surface,
                item.title,
                item.position,
                item.availability,
                item.live_status,
                item.source_timestamp,
                item.release_timestamp,
                item.duration_seconds,
            ),
        )
        changes: dict[str, dict[str, object | None]] = {}
        if previous:
            current = {
                "title": item.title,
                "availability": item.availability,
                "live_status": item.live_status,
                "source_timestamp": item.source_timestamp,
                "release_timestamp": item.release_timestamp,
                "duration_seconds": item.duration_seconds,
            }
            for key, new_value in current.items():
                old_value = previous[key]
                if old_value != new_value:
                    changes[key] = {"old": old_value, "new": new_value}
        return item_exists is None, surface_exists is None, changes

    def record_probe(
        self,
        *,
        cycle_id: int,
        channel_id: str,
        video_id: str,
        mode: str,
        atom_seen_this_cycle: bool | None,
        outcome: ProbeOutcome,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO probes (
              cycle_id, probed_at, channel_id, video_id, mode, ok, access_class,
              availability, live_status, release_timestamp, atom_seen_this_cycle,
              error_kind, error_message, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_id,
                iso_now(),
                channel_id,
                video_id,
                mode,
                int(outcome.ok),
                outcome.access_class,
                outcome.availability,
                outcome.live_status,
                outcome.release_timestamp,
                None if atom_seen_this_cycle is None else int(atom_seen_this_cycle),
                outcome.error_kind,
                outcome.error_message,
                json.dumps(outcome.metadata, ensure_ascii=False, sort_keys=True),
            ),
        )
        self.conn.commit()

    def latest_probe_access_class(self, channel_id: str, video_id: str, mode: str) -> str | None:
        access_class, _error_kind, _live_status = self.latest_probe_state(
            channel_id, video_id, mode
        )
        return access_class

    def latest_probe_state(
        self, channel_id: str, video_id: str, mode: str
    ) -> tuple[str | None, str | None, str | None]:
        row = self.conn.execute(
            """
            SELECT access_class, error_kind, live_status FROM probes
            WHERE channel_id = ? AND video_id = ? AND mode = ?
            ORDER BY id DESC LIMIT 1
            """,
            (channel_id, video_id, mode),
        ).fetchone()
        if not row:
            return None, None, None
        access_class = str(row["access_class"]) if row["access_class"] is not None else None
        error_kind = str(row["error_kind"]) if row["error_kind"] is not None else None
        live_status = str(row["live_status"]) if row["live_status"] is not None else None
        return access_class, error_kind, live_status

    def pending_probe_items(
        self,
        channel_id: str,
        mode: str,
        *,
        limit: int,
    ) -> list[SurfaceItem]:
        rows = self.conn.execute(
            """
            SELECT i.video_id, i.title, i.url
            FROM probe_queue q
            JOIN items i
              ON i.channel_id = q.channel_id AND i.video_id = q.video_id
            WHERE q.channel_id = ? AND q.mode = ?
            ORDER BY q.updated_at ASC, q.queued_at ASC
            LIMIT ?
            """,
            (channel_id, mode, limit),
        ).fetchall()
        return [
            SurfaceItem(
                video_id=str(row["video_id"]),
                title=str(row["title"]),
                url=str(row["url"]),
            )
            for row in rows
        ]

    def queue_probe(
        self,
        channel_id: str,
        video_id: str,
        mode: str,
        *,
        reason: str,
        move_to_back: bool = False,
    ) -> None:
        now = iso_now()
        self.conn.execute(
            """
            INSERT INTO probe_queue (
              channel_id, video_id, mode, queued_at, updated_at, reason
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id, video_id, mode) DO UPDATE SET
              updated_at = CASE
                WHEN ? THEN excluded.updated_at
                ELSE probe_queue.updated_at
              END,
              reason = excluded.reason
            """,
            (channel_id, video_id, mode, now, now, reason, int(move_to_back)),
        )
        self.conn.commit()

    def remove_queued_probe(self, channel_id: str, video_id: str, mode: str) -> None:
        self.conn.execute(
            "DELETE FROM probe_queue WHERE channel_id = ? AND video_id = ? AND mode = ?",
            (channel_id, video_id, mode),
        )
        self.conn.commit()

    def order_probe_candidates(
        self,
        channel_id: str,
        candidates: dict[tuple[str, str], SurfaceItem],
    ) -> list[tuple[tuple[str, str], SurfaceItem]]:
        rows = self.conn.execute(
            """
            SELECT mode, video_id
            FROM probe_queue
            WHERE channel_id = ?
            ORDER BY updated_at ASC, queued_at ASC, mode ASC, video_id ASC
            """,
            (channel_id,),
        ).fetchall()
        ranks = {
            (str(row["mode"]), str(row["video_id"])): index
            for index, row in enumerate(rows)
        }
        fallback = len(ranks)
        return sorted(
            candidates.items(),
            key=lambda pair: ranks.get(pair[0], fallback),
        )

    def report(self, limit: int = 20) -> dict[str, object]:
        self.conn.execute("BEGIN")
        latest_cycle = self.conn.execute(
            "SELECT * FROM cycles WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        active_cycle = self.conn.execute(
            """
            SELECT * FROM cycles
            WHERE id = (SELECT MAX(id) FROM cycles) AND finished_at IS NULL
            """
        ).fetchone()
        surfaces = {
            str(row["surface"]): int(row["count"])
            for row in self.conn.execute(
                "SELECT surface, COUNT(*) AS count FROM surface_items GROUP BY surface ORDER BY surface"
            )
        }
        probe_classes = {
            f"{row['mode']}:{row['access_class']}": int(row["count"])
            for row in self.conn.execute(
                """
                SELECT mode, access_class, COUNT(*) AS count
                FROM probes GROUP BY mode, access_class ORDER BY mode, access_class
                """
            )
        }
        members_only = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT p.probed_at, i.channel_name, p.channel_id, p.video_id, i.title,
                       p.mode, p.access_class, p.error_kind,
                       (
                         SELECT current.access_class FROM probes current
                         WHERE current.channel_id = p.channel_id
                           AND current.video_id = p.video_id
                           AND current.mode = p.mode
                         ORDER BY current.id DESC LIMIT 1
                       ) AS current_access_class,
                       EXISTS (
                         SELECT 1 FROM surface_items s
                         WHERE s.channel_id = p.channel_id
                           AND s.video_id = p.video_id
                           AND s.surface = 'atom'
                       ) AS atom_seen_ever
                FROM probes p
                JOIN (
                  SELECT channel_id, video_id, mode, MAX(id) AS latest_id
                  FROM probes
                  WHERE access_class IN ('members_only', 'members_only_accessible')
                  GROUP BY channel_id, video_id, mode
                ) confirmed ON confirmed.latest_id = p.id
                JOIN items i
                  ON i.channel_id = p.channel_id AND i.video_id = p.video_id
                ORDER BY p.probed_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        ]
        recent_discoveries = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT i.first_seen_at, i.channel_name, i.channel_id, i.video_id, i.title,
                       GROUP_CONCAT(s.surface, ',') AS surfaces
                FROM items i
                JOIN surface_items s
                  ON s.channel_id = i.channel_id AND s.video_id = i.video_id
                GROUP BY i.channel_id, i.video_id
                ORDER BY i.first_seen_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        ]
        recent_probes = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT p.probed_at, i.channel_name, p.video_id, i.title, p.mode,
                       p.ok, p.access_class, p.availability, p.live_status,
                       p.release_timestamp, p.atom_seen_this_cycle, p.error_kind
                FROM probes p
                JOIN items i
                  ON i.channel_id = p.channel_id AND i.video_id = p.video_id
                ORDER BY p.probed_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        ]
        channel_coverage = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT i.channel_id, MAX(i.channel_name) AS channel_name,
                       COUNT(DISTINCT i.video_id) AS unique_items,
                       COUNT(DISTINCT am.video_id) AS members_playlist_items,
                       COUNT(DISTINCT pm.video_id) AS probe_confirmed_member_items,
                       COUNT(DISTINCT CASE WHEN am.video_id IS NOT NULL
                                            OR pm.video_id IS NOT NULL
                                          THEN i.video_id END) AS membership_surface_items,
                       COUNT(DISTINCT CASE WHEN (am.video_id IS NOT NULL
                                                 OR pm.video_id IS NOT NULL)
                                            AND a.video_id IS NOT NULL
                                           THEN i.video_id END) AS members_also_atom,
                       COUNT(DISTINCT CASE WHEN (am.video_id IS NOT NULL
                                                 OR pm.video_id IS NOT NULL)
                                            AND st.video_id IS NOT NULL
                                           THEN i.video_id END) AS members_also_streams,
                       COUNT(DISTINCT CASE WHEN (am.video_id IS NOT NULL
                                                 OR pm.video_id IS NOT NULL)
                                            AND v.video_id IS NOT NULL
                                           THEN i.video_id END) AS members_also_videos
                FROM items i
                LEFT JOIN surface_items am
                  ON am.channel_id = i.channel_id AND am.video_id = i.video_id
                 AND am.surface = 'anon:members_playlist'
                LEFT JOIN (
                  SELECT DISTINCT channel_id, video_id
                  FROM probes
                  WHERE access_class IN ('members_only', 'members_only_accessible')
                ) pm ON pm.channel_id = i.channel_id AND pm.video_id = i.video_id
                LEFT JOIN surface_items a
                  ON a.channel_id = i.channel_id AND a.video_id = i.video_id
                 AND a.surface = 'atom'
                LEFT JOIN surface_items st
                  ON st.channel_id = i.channel_id AND st.video_id = i.video_id
                 AND st.surface = 'anon:streams'
                LEFT JOIN surface_items v
                  ON v.channel_id = i.channel_id AND v.video_id = i.video_id
                 AND v.surface = 'anon:videos'
                GROUP BY i.channel_id
                ORDER BY channel_name
                """
            )
        ]
        active_watches = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT p.probed_at, i.channel_name, p.channel_id, p.video_id, i.title,
                       p.mode, p.access_class, p.atom_seen_this_cycle, p.error_kind
                FROM probes p
                JOIN (
                  SELECT channel_id, video_id, mode, MAX(id) AS latest_id
                  FROM probes GROUP BY channel_id, video_id, mode
                ) latest ON latest.latest_id = p.id
                JOIN items i
                  ON i.channel_id = p.channel_id AND i.video_id = p.video_id
                WHERE p.access_class = 'upcoming'
                   OR p.live_status IN ('is_upcoming', 'is_live', 'post_live')
                ORDER BY p.probed_at DESC
                """
            )
        ]
        surface_run_statuses = {
            str(row["status"]): int(row["count"])
            for row in self.conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM surface_runs GROUP BY status ORDER BY status
                """
            )
        }
        latest_surface_runs = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT r.cycle_id, i.channel_name, r.channel_id, r.surface,
                       r.status, r.item_count, r.error_kind
                FROM surface_runs r
                LEFT JOIN (
                  SELECT channel_id, MAX(channel_name) AS channel_name
                  FROM items GROUP BY channel_id
                ) i ON i.channel_id = r.channel_id
                WHERE r.cycle_id = (
                  SELECT id FROM cycles
                  WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1
                )
                ORDER BY channel_name, r.surface
                """
            )
        ]
        observer_state = {
            str(row["key"]): str(row["value"])
            for row in self.conn.execute(
                "SELECT key, value FROM observer_state ORDER BY key"
            )
        }
        payload = {
            "database": str(self.path),
            "latest_cycle": dict(latest_cycle) if latest_cycle else None,
            "active_cycle": dict(active_cycle) if active_cycle else None,
            "totals": {
                "items": int(self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]),
                "cycles": int(self.conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]),
                "probes": int(self.conn.execute("SELECT COUNT(*) FROM probes").fetchone()[0]),
                "pending_probes": int(
                    self.conn.execute("SELECT COUNT(*) FROM probe_queue").fetchone()[0]
                ),
                "instrumented_cycles": int(
                    self.conn.execute(
                        "SELECT COUNT(DISTINCT cycle_id) FROM surface_runs"
                    ).fetchone()[0]
                ),
                "unfinished_cycles": int(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM cycles WHERE finished_at IS NULL"
                    ).fetchone()[0]
                ),
            },
            "surface_item_counts": surfaces,
            "probe_class_counts": probe_classes,
            "surface_run_status_counts": surface_run_statuses,
            "latest_surface_runs": latest_surface_runs,
            "observer_state": observer_state,
            "channel_coverage": channel_coverage,
            "active_watches": active_watches,
            "members_only": members_only,
            "recent_probes": recent_probes,
            "recent_discoveries": recent_discoveries,
        }
        self.conn.rollback()
        return payload


class YtDlpInspector:
    def __init__(self, config: ObserverConfig):
        self.config = config
        self._last_request_at = 0.0

    def list_tab(self, channel_id: str, tab: str) -> list[SurfaceItem]:
        url = f"https://www.youtube.com/channel/{channel_id}/{tab}"
        return self._list_url(url)

    def list_members_playlist(self, channel_id: str) -> list[SurfaceItem]:
        playlist_id = f"UUMO{channel_id[2:]}"
        url = f"https://www.youtube.com/playlist?list={playlist_id}"
        return [
            SurfaceItem(
                video_id=item.video_id,
                title=item.title,
                url=item.url,
                position=item.position,
                availability=item.availability or "subscriber_only",
                live_status=item.live_status,
                source_timestamp=item.source_timestamp,
                release_timestamp=item.release_timestamp,
                duration_seconds=item.duration_seconds,
            )
            for item in self._list_url(url)
        ]

    def _list_url(self, url: str) -> list[SurfaceItem]:
        cmd = [
            self.config.yt_dlp,
            "--ignore-config",
            "--flat-playlist",
            "--playlist-end",
            str(self.config.tab_limit),
            "--dump-single-json",
            "--extractor-retries",
            "2",
            "--socket-timeout",
            str(min(self.config.request_timeout_seconds, 60)),
            url,
        ]
        completed = self._run(cmd)
        warning_message = _safe_error(completed.stderr)
        if warning_message:
            warning_kind = classify_probe_error(warning_message)
            if warning_kind in CRITICAL_SURFACE_WARNING_ERRORS:
                raise InspectionError(warning_kind, warning_message)
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            message = _safe_error(completed.stderr or completed.stdout)
            raise InspectionError(classify_probe_error(message), message) from exc
        entries = payload.get("entries") or []
        result: list[SurfaceItem] = []
        for position, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            video_id = str(entry["id"])
            result.append(
                SurfaceItem(
                    video_id=video_id,
                    title=str(entry.get("title") or video_id),
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    position=position,
                    availability=_optional_str(entry.get("availability")),
                    live_status=_optional_str(entry.get("live_status")),
                    source_timestamp=_timestamp_iso(entry.get("timestamp")),
                    release_timestamp=_optional_int(entry.get("release_timestamp")),
                    duration_seconds=_optional_float(entry.get("duration")),
                )
            )
        return result

    def probe(self, url: str) -> ProbeOutcome:
        cmd = [
            self.config.yt_dlp,
            "--ignore-config",
            "--skip-download",
            "--ignore-no-formats-error",
            "--dump-single-json",
            "--no-playlist",
            "--extractor-retries",
            "2",
            "--socket-timeout",
            str(min(self.config.request_timeout_seconds, 60)),
            url,
        ]
        try:
            completed = self._run(cmd)
        except InspectionError as exc:
            return ProbeOutcome(
                ok=False,
                access_class=_access_class_for_error(exc.kind),
                error_kind=exc.kind,
                error_message=exc.message,
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            message = _safe_error(completed.stderr or completed.stdout)
            kind = classify_probe_error(message)
            return ProbeOutcome(
                ok=False,
                access_class=_access_class_for_error(kind),
                error_kind=kind,
                error_message=message,
            )

        metadata_keys = (
            "id",
            "title",
            "channel_id",
            "channel",
            "availability",
            "live_status",
            "timestamp",
            "release_timestamp",
            "duration",
            "was_live",
        )
        metadata = {key: payload[key] for key in metadata_keys if payload.get(key) is not None}
        availability = _optional_str(payload.get("availability"))
        warning_message = _safe_error(completed.stderr)
        warning_kind = classify_probe_error(warning_message) if warning_message else None
        formats = payload.get("formats")
        has_media_format = isinstance(formats, list) and any(
            isinstance(item, dict)
            and (item.get("url") or item.get("manifest_url"))
            and (
                item.get("vcodec") not in {None, "none"}
                or item.get("acodec") not in {None, "none"}
            )
            for item in formats
        )
        live_status = _optional_str(payload.get("live_status"))
        if availability == "subscriber_only":
            access_class = "members_only"
        elif availability == "premium_only":
            access_class = "premium_only"
        elif live_status == "is_upcoming":
            access_class = "upcoming"
        else:
            access_class = "accessible"
        if (
            live_status != "is_upcoming"
            and not has_media_format
            and warning_kind in RETRYABLE_PROBE_ERRORS | TERMINAL_PROBE_ERRORS
            and availability not in {"subscriber_only", "premium_only"}
        ):
            return ProbeOutcome(
                ok=False,
                access_class=_access_class_for_error(warning_kind),
                availability=availability,
                live_status=live_status,
                release_timestamp=_optional_int(payload.get("release_timestamp")),
                error_kind=warning_kind,
                error_message=warning_message,
                metadata=metadata,
            )
        return ProbeOutcome(
            ok=True,
            access_class=access_class,
            availability=availability,
            live_status=live_status,
            release_timestamp=_optional_int(payload.get("release_timestamp")),
            metadata=metadata,
        )

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.config.request_spacing_seconds:
            time.sleep(self.config.request_spacing_seconds - elapsed)
        try:
            completed = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=self.config.request_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            self._last_request_at = time.monotonic()
            raise InspectionError("timeout", f"yt-dlp timed out after {self.config.request_timeout_seconds}s") from exc
        except FileNotFoundError as exc:
            self._last_request_at = time.monotonic()
            raise InspectionError("tool_missing", f"yt-dlp executable not found: {self.config.yt_dlp}") from exc
        self._last_request_at = time.monotonic()
        if completed.returncode != 0:
            message = _safe_error(completed.stderr or completed.stdout)
            raise InspectionError(classify_probe_error(message), message)
        return completed


class MemberObserver:
    def __init__(
        self,
        config: ObserverConfig,
        *,
        feed_fetcher: Callable[[str], bytes] = fetch_feed,
        inspector: YtDlpInspector | None = None,
    ):
        self.config = config
        self.logger = logging.getLogger("ytb_tg_backup.dev.member_observer")
        self.store = ObserverStore(config.db_path)
        self.feed_fetcher = feed_fetcher
        self.inspector = inspector or YtDlpInspector(config)
        self.stop_event = threading.Event()
        self.request_paused_until = _parse_datetime(self.store.get_state("request_paused_until"))

    def close(self) -> None:
        self.store.close()

    def run_forever(self) -> None:
        self._install_signal_handlers()
        while not self.stop_event.is_set():
            if self.config.stop_at and utc_now() >= self.config.stop_at:
                self.logger.info("configured stop_at reached: %s", self.config.stop_at.isoformat())
                self._write_final_report()
                return
            self.run_cycle()
            wait_seconds = self.config.poll_interval_seconds
            if self.config.stop_at:
                remaining = (self.config.stop_at - utc_now()).total_seconds()
                if remaining <= 0:
                    self._write_final_report()
                    return
                wait_seconds = min(wait_seconds, remaining)
            self.stop_event.wait(wait_seconds)

    def run_cycle(self) -> dict[str, int]:
        channels = [channel for channel in self.config.channels if channel.enabled]
        cycle_id = self.store.begin_cycle(len(channels))
        stats = {
            "surface_errors": 0,
            "item_sightings": 0,
            "probe_count": 0,
            "members_only_count": 0,
            "probe_errors": 0,
            "surface_skips": 0,
            "interrupted": 0,
        }
        member_video_ids: set[tuple[str, str]] = set()
        observed_at = iso_now()
        halt_requests = self._requests_are_paused()
        for channel in channels:
            if halt_requests:
                break
            if self._should_interrupt():
                stats["interrupted"] = 1
                break
            atom_ids, atom_ok, candidates = self._observe_atom(
                cycle_id, channel, observed_at, stats
            )
            if self._requests_are_paused():
                halt_requests = True
                break
            for mode, video_id in candidates:
                self.store.queue_probe(
                    channel.channel_id,
                    video_id,
                    mode,
                    reason="surface:atom",
                )
            for item in self.store.pending_probe_items(
                channel.channel_id,
                "anon",
                limit=self.config.retry_probe_limit_per_channel,
            ):
                candidates.setdefault(("anon", item.video_id), item)
            seed_counts = {"anon": 0}
            for mode, tabs in [("anon", self.config.anonymous_tabs)]:
                surfaces: list[tuple[str, str | None]] = [(tab, tab) for tab in tabs]
                if self.config.anonymous_members_playlist:
                    surfaces.insert(0, ("members_playlist", None))
                for surface_index, (surface_name, tab) in enumerate(surfaces):
                    surface = f"{mode}:{surface_name}"
                    if self._should_interrupt():
                        stats["interrupted"] = 1
                        stats["surface_skips"] += self._record_surface_skips(
                            cycle_id=cycle_id,
                            observed_at=observed_at,
                            channel=channel,
                            mode=mode,
                            surfaces=surfaces[surface_index:],
                            reason="interrupted",
                        )
                        break
                    baseline = self.store.surface_seeded(channel.channel_id, surface)
                    try:
                        if surface_name == "members_playlist":
                            items = self.inspector.list_members_playlist(channel.channel_id)
                        else:
                            assert tab is not None
                            items = self.inspector.list_tab(channel.channel_id, tab)
                    except InspectionError as exc:
                        stats["surface_errors"] += 1
                        self.store.record_surface_error(
                            channel.channel_id,
                            surface,
                            observed_at,
                            exc.kind,
                            exc.message,
                        )
                        self.store.record_surface_run(
                            cycle_id=cycle_id,
                            observed_at=observed_at,
                            channel_id=channel.channel_id,
                            surface=surface,
                            status="error",
                            item_count=None,
                            error_kind=exc.kind,
                            error_message=exc.message,
                        )
                        self._write_event(
                            {
                                "type": "surface_error",
                                "observed_at": observed_at,
                                "cycle_id": cycle_id,
                                "channel_id": channel.channel_id,
                                "channel_name": channel.name,
                                "surface": surface,
                                "error_kind": exc.kind,
                                "error_message": exc.message,
                            }
                        )
                        self.logger.warning(
                            "surface failed channel=%s surface=%s kind=%s",
                            channel.id,
                            surface,
                            exc.kind,
                        )
                        if exc.kind in GLOBAL_CHALLENGE_ERRORS:
                            self._pause_requests(exc.kind)
                            halt_requests = True
                            break
                        continue

                    for item in items:
                        _, new_to_surface, changes = self.store.record_item(
                            cycle_id=cycle_id,
                            channel=channel,
                            surface=surface,
                            item=item,
                            observed_at=observed_at,
                        )
                        stats["item_sightings"] += 1
                        if new_to_surface:
                            self._write_event(
                                {
                                    "type": "discovery",
                                    "observed_at": observed_at,
                                    "cycle_id": cycle_id,
                                    "channel_id": channel.channel_id,
                                    "channel_name": channel.name,
                                    "surface": surface,
                                    "video_id": item.video_id,
                                    "title": item.title,
                                    "baseline": not baseline,
                                }
                            )
                        if changes:
                            self._write_event(
                                {
                                    "type": "state_change",
                                    "observed_at": observed_at,
                                    "cycle_id": cycle_id,
                                    "channel_id": channel.channel_id,
                                    "channel_name": channel.name,
                                    "surface": surface,
                                    "video_id": item.video_id,
                                    "title": item.title,
                                    "changes": changes,
                                }
                            )
                        should_probe = baseline and new_to_surface
                        previous_access, previous_error, previous_live_status = (
                            self.store.latest_probe_state(
                                channel.channel_id,
                                item.video_id,
                                mode,
                            )
                        )
                        if (
                            previous_access == "upcoming"
                            or previous_error in RETRYABLE_PROBE_ERRORS
                            or previous_live_status in {"is_upcoming", "is_live"}
                        ):
                            should_probe = True
                        if item.live_status in {"is_upcoming", "is_live"}:
                            should_probe = True
                        if (
                            surface_name in {"streams", "videos"}
                            and (item.position or self.config.tab_limit + 1) <= 5
                            and MEMBERSHIP_TITLE_RE.search(item.title)
                            and previous_access is None
                        ):
                            should_probe = True
                        if (
                            not baseline
                            and new_to_surface
                            and (
                                surface_name == "members_playlist"
                                or MEMBERSHIP_TITLE_RE.search(item.title)
                            )
                            and seed_counts[mode] < self.config.seed_probe_keyword_limit_per_channel
                        ):
                            should_probe = True
                            seed_counts[mode] += 1
                        if should_probe:
                            candidates.setdefault((mode, item.video_id), item)
                            self.store.queue_probe(
                                channel.channel_id,
                                item.video_id,
                                mode,
                                reason=f"surface:{surface}",
                            )
                    self.store.record_surface_success(channel.channel_id, surface, observed_at)
                    self.store.record_surface_run(
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        channel_id=channel.channel_id,
                        surface=surface,
                        status="success" if items else "empty",
                        item_count=len(items),
                    )
                    if self._should_interrupt():
                        stats["interrupted"] = 1
                        break

                if halt_requests or stats["interrupted"]:
                    break

            for (mode, video_id), item in self.store.order_probe_candidates(
                channel.channel_id,
                candidates,
            ):
                if halt_requests:
                    break
                if self._should_interrupt():
                    stats["interrupted"] = 1
                    break
                outcome = self.inspector.probe(item.url)
                atom_seen = video_id in atom_ids if atom_ok else None
                self.store.record_probe(
                    cycle_id=cycle_id,
                    channel_id=channel.channel_id,
                    video_id=video_id,
                    mode=mode,
                    atom_seen_this_cycle=atom_seen,
                    outcome=outcome,
                )
                if _probe_outcome_needs_retry(outcome):
                    self.store.queue_probe(
                        channel.channel_id,
                        video_id,
                        mode,
                        reason=outcome.error_kind or outcome.live_status or outcome.access_class,
                        move_to_back=True,
                    )
                else:
                    self.store.remove_queued_probe(
                        channel.channel_id,
                        video_id,
                        mode,
                    )
                stats["probe_count"] += 1
                if outcome.access_class in {"members_only", "members_only_accessible"}:
                    member_video_ids.add((channel.channel_id, video_id))
                    stats["members_only_count"] = len(member_video_ids)
                if outcome.error_kind in RETRYABLE_PROBE_ERRORS:
                    stats["probe_errors"] += 1
                self._write_event(
                    {
                        "type": "probe",
                        "observed_at": iso_now(),
                        "cycle_id": cycle_id,
                        "channel_id": channel.channel_id,
                        "channel_name": channel.name,
                        "video_id": video_id,
                        "title": item.title,
                        "mode": mode,
                        "atom_seen_this_cycle": atom_seen,
                        "ok": outcome.ok,
                        "access_class": outcome.access_class,
                        "availability": outcome.availability,
                        "live_status": outcome.live_status,
                        "error_kind": outcome.error_kind,
                        "error_message": outcome.error_message,
                    }
                )
                if outcome.error_kind in GLOBAL_CHALLENGE_ERRORS:
                    self._pause_requests(outcome.error_kind)
                    halt_requests = True
                    break
                if self._should_interrupt():
                    stats["interrupted"] = 1
                    break

            if halt_requests or stats["interrupted"]:
                break

        if self._should_interrupt():
            stats["interrupted"] = 1
        if halt_requests or stats["interrupted"]:
            reason = "interrupted" if stats["interrupted"] else "request_paused"
            stats["surface_skips"] += self._record_missing_surface_skips(
                cycle_id=cycle_id,
                observed_at=observed_at,
                channels=channels,
                reason=reason,
            )
        self.store.finish_cycle(cycle_id, stats)
        self._write_event(
            {
                "type": "cycle",
                "observed_at": iso_now(),
                "cycle_id": cycle_id,
                **stats,
            }
        )
        self.logger.info(
            "cycle=%s sightings=%s probes=%s members_only=%s probe_errors=%s "
            "surface_errors=%s surface_skips=%s interrupted=%s",
            cycle_id,
            stats["item_sightings"],
            stats["probe_count"],
            stats["members_only_count"],
            stats["probe_errors"],
            stats["surface_errors"],
            stats["surface_skips"],
            stats["interrupted"],
        )
        return stats

    def _observe_atom(
        self,
        cycle_id: int,
        channel: ObservedChannel,
        observed_at: str,
        stats: dict[str, int],
    ) -> tuple[set[str], bool, dict[tuple[str, str], SurfaceItem]]:
        surface = "atom"
        baseline = self.store.surface_seeded(channel.channel_id, surface)
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel.channel_id}"
        try:
            entries = parse_feed(self.feed_fetcher(url), channel.id, channel.name)
        except Exception as exc:
            stats["surface_errors"] += 1
            kind = classify_probe_error(str(exc))
            message = _safe_error(str(exc))
            self.store.record_surface_error(channel.channel_id, surface, observed_at, kind, message)
            self.store.record_surface_run(
                cycle_id=cycle_id,
                observed_at=observed_at,
                channel_id=channel.channel_id,
                surface=surface,
                status="error",
                item_count=None,
                error_kind=kind,
                error_message=message,
            )
            self._write_event(
                {
                    "type": "surface_error",
                    "observed_at": observed_at,
                    "cycle_id": cycle_id,
                    "channel_id": channel.channel_id,
                    "channel_name": channel.name,
                    "surface": surface,
                    "error_kind": kind,
                    "error_message": message,
                }
            )
            if kind in GLOBAL_CHALLENGE_ERRORS:
                self._pause_requests(kind)
            self.logger.warning("atom failed channel=%s kind=%s", channel.id, kind)
            return set(), False, {}
        ids: set[str] = set()
        candidates: dict[tuple[str, str], SurfaceItem] = {}
        candidate_modes = ["anon"]
        seed_counts = {mode: 0 for mode in candidate_modes}
        for position, entry in enumerate(entries, start=1):
            item = SurfaceItem(
                video_id=entry.video_id,
                title=entry.title,
                url=entry.url,
                position=position,
                source_timestamp=entry.published_at,
            )
            _, new_to_surface, changes = self.store.record_item(
                cycle_id=cycle_id,
                channel=channel,
                surface=surface,
                item=item,
                observed_at=observed_at,
            )
            stats["item_sightings"] += 1
            ids.add(entry.video_id)
            if new_to_surface:
                self._write_event(
                    {
                        "type": "discovery",
                        "observed_at": observed_at,
                        "cycle_id": cycle_id,
                        "channel_id": channel.channel_id,
                        "channel_name": channel.name,
                        "surface": surface,
                        "video_id": item.video_id,
                        "title": item.title,
                        "baseline": not baseline,
                    }
                )
            if changes:
                self._write_event(
                    {
                        "type": "state_change",
                        "observed_at": observed_at,
                        "cycle_id": cycle_id,
                        "channel_id": channel.channel_id,
                        "channel_name": channel.name,
                        "surface": surface,
                        "video_id": item.video_id,
                        "title": item.title,
                        "changes": changes,
                    }
                )
            for mode in candidate_modes:
                previous_access, previous_error, previous_live_status = (
                    self.store.latest_probe_state(
                        channel.channel_id, item.video_id, mode
                    )
                )
                should_probe = baseline and new_to_surface
                if (
                    previous_access == "upcoming"
                    or previous_error in RETRYABLE_PROBE_ERRORS
                    or previous_live_status in {"is_upcoming", "is_live"}
                ):
                    should_probe = True
                if (
                    not baseline
                    and new_to_surface
                    and MEMBERSHIP_TITLE_RE.search(item.title)
                    and seed_counts[mode] < self.config.seed_probe_keyword_limit_per_channel
                ):
                    should_probe = True
                    seed_counts[mode] += 1
                if should_probe:
                    candidates[(mode, item.video_id)] = item
        self.store.record_surface_success(channel.channel_id, surface, observed_at)
        self.store.record_surface_run(
            cycle_id=cycle_id,
            observed_at=observed_at,
            channel_id=channel.channel_id,
            surface=surface,
            status="success" if entries else "empty",
            item_count=len(entries),
        )
        return ids, True, candidates

    def _record_surface_skips(
        self,
        *,
        cycle_id: int,
        observed_at: str,
        channel: ObservedChannel,
        mode: str,
        surfaces: list[tuple[str, str | None]],
        reason: str,
    ) -> int:
        inserted = 0
        for surface_name, _tab in surfaces:
            if self.store.record_surface_run(
                cycle_id=cycle_id,
                observed_at=observed_at,
                channel_id=channel.channel_id,
                surface=f"{mode}:{surface_name}",
                status="skipped",
                item_count=None,
                error_kind=reason,
                overwrite=False,
            ):
                inserted += 1
        return inserted

    def _record_missing_surface_skips(
        self,
        *,
        cycle_id: int,
        observed_at: str,
        channels: list[ObservedChannel],
        reason: str,
    ) -> int:
        inserted = 0
        for channel in channels:
            for surface in self._expected_surfaces():
                if self.store.record_surface_run(
                    cycle_id=cycle_id,
                    observed_at=observed_at,
                    channel_id=channel.channel_id,
                    surface=surface,
                    status="skipped",
                    item_count=None,
                    error_kind=reason,
                    overwrite=False,
                ):
                    inserted += 1
        return inserted

    def _expected_surfaces(self) -> list[str]:
        surfaces = ["atom"]
        if self.config.anonymous_members_playlist:
            surfaces.append("anon:members_playlist")
        surfaces.extend(f"anon:{tab}" for tab in self.config.anonymous_tabs)
        return surfaces

    def _should_interrupt(self) -> bool:
        return self.stop_event.is_set() or bool(
            self.config.stop_at and utc_now() >= self.config.stop_at
        )

    def _write_event(self, payload: dict[str, object]) -> None:
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self.config.data_dir.chmod(0o700)
        with self.config.event_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        self.config.event_log_path.chmod(0o600)

    def _write_final_report(self) -> None:
        path = self.config.data_dir / "final-report.json"
        write_report(path, self.store.report(limit=100))
        self.logger.info("final observation report written path=%s", path)

    def _requests_are_paused(self) -> bool:
        if not self.request_paused_until:
            return False
        if utc_now() < self.request_paused_until:
            return True
        self.request_paused_until = None
        self.store.delete_state("request_paused_until")
        self.store.delete_state("request_pause_reason")
        return False

    def _pause_requests(self, reason: str) -> None:
        self.request_paused_until = utc_now() + timedelta(
            hours=self.config.pause_hours_on_challenge
        )
        self.store.set_state("request_paused_until", self.request_paused_until.isoformat())
        self.store.set_state("request_pause_reason", reason)
        self.logger.warning(
            "all YouTube requests paused until=%s reason=%s",
            self.request_paused_until.isoformat(),
            reason,
        )

    def _install_signal_handlers(self) -> None:
        def stop(_signum, _frame):
            self.stop_event.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)


def classify_probe_error(message: str) -> str:
    text = message.lower()
    if "removed by the uploader" in text or "video has been removed" in text:
        return "removed"
    if "private video" in text or "video is private" in text:
        return "private"
    if (
        "video unavailable" in text
        or "this video is unavailable" in text
        or "this video is not available" in text
    ):
        return "unavailable"
    if "po token" in text or "proof of origin" in text:
        return "po_token_required"
    if "live event will begin" in text or "premieres in" in text:
        return "upcoming"
    if "429" in text or "too many requests" in text or "try again later" in text:
        return "rate_limited"
    if "confirm you’re not a bot" in text or "confirm you're not a bot" in text or "not a bot" in text:
        return "bot_check"
    if (
        "members-only" in text
        or "available to this channel's members" in text
        or "join this channel to get access" in text
    ):
        return "members_only_denied"
    if "does not have a membership tab" in text:
        return "no_membership_tab"
    if "sign in" in text or "login required" in text:
        return "login_required"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "http error" in text or "urlopen error" in text or "network" in text:
        return "network"
    if (
        "no video formats" in text
        or "no formats found" in text
        or "requested format is not available" in text
    ):
        return "no_formats"
    return "extractor_error"


def _access_class_for_error(kind: str) -> str:
    if kind == "members_only_denied":
        return "members_only"
    if kind == "login_required":
        return "login_required"
    if kind in {"private", "removed"}:
        return kind
    if kind == "unavailable":
        return "unavailable"
    if kind == "upcoming":
        return "upcoming"
    return "probe_error"


def _probe_outcome_needs_retry(outcome: ProbeOutcome) -> bool:
    return bool(
        outcome.error_kind in RETRYABLE_PROBE_ERRORS
        or outcome.access_class == "upcoming"
        or outcome.live_status in {"is_upcoming", "is_live"}
        or (outcome.live_status == "post_live" and not outcome.ok)
    )


def _safe_error(value: str) -> str:
    text = ANSI_ESCAPE_RE.sub("", value or "")
    text = text.replace(str(Path.home()), "~")
    return re.sub(r"\s+", " ", text).strip()[:1000]


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp_iso(value: object) -> str | None:
    parsed = _optional_float(value)
    if parsed is None:
        return None
    try:
        return datetime.fromtimestamp(parsed, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def write_report(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


@contextmanager
def _single_instance_lock(config: ObserverConfig):
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.data_dir.chmod(0o700)
    lock_path = config.data_dir / ".observer.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        lock_path.chmod(0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"another member observer is already running for {config.data_dir}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(
        prog="python -m ytb_tg_backup.dev.member_observer",
        description=(
            "Development-only anonymous YouTube membership metadata observer; "
            "never downloads media."
        ),
    )
    parser.add_argument("command", choices=("once", "run", "report"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--limit", type=int, default=20, help="Rows shown by report")
    parser.add_argument("--output", help="Write report JSON to this path instead of stdout")
    args = parser.parse_args(argv)

    config = load_observer_config(args.config)
    if args.command == "run" and config.stop_at is None:
        parser.error("the run command requires observer.stop_at")
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command == "report":
        store = ObserverStore(config.db_path, read_only=True)
        try:
            report = store.report(max(1, args.limit))
            if args.output:
                write_report(Path(args.output).expanduser(), report)
            else:
                print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            store.close()
        return 0

    with _single_instance_lock(config):
        observer = MemberObserver(config)
        try:
            if args.command == "once":
                observer.run_cycle()
            else:
                observer.run_forever()
        finally:
            observer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
