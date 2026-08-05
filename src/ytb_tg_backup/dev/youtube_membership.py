"""Development-only anonymous YouTube membership notifications.

This module intentionally lives outside the production discovery/download
pipeline.  It never opens the main ``state.db`` and never passes download
arguments, cookies, browser profiles, or other authentication material to
yt-dlp.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Callable, Iterator

from ..config import Config
from ..feed import fetch_feed, parse_feed
from ..models import Origin
from ..telegram import TelegramTextNotifier, TelegramUploadError
from .member_observer import (
    GLOBAL_CHALLENGE_ERRORS,
    MEMBERSHIP_TITLE_RE,
    InspectionError,
    ObserverConfig,
    ProbeOutcome,
    SurfaceItem,
    YtDlpInspector,
    classify_probe_error,
)


MAX_PROBE_FAILURES = 5
CHALLENGE_COOLDOWN_HOURS = 6
TERMINAL_PROBE_ERRORS = {"removed", "private", "unavailable"}
RETRYABLE_PROBE_ERRORS = {
    "timeout",
    "network",
    "rate_limited",
    "bot_check",
    "login_required",
    "po_token_required",
    "tool_missing",
    "extractor_error",
    "no_formats",
}


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS cycles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  success INTEGER,
  stats_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS surface_state (
  origin_id TEXT NOT NULL,
  surface TEXT NOT NULL,
  seeded_at TEXT,
  last_success_at TEXT,
  last_error_at TEXT,
  last_error_kind TEXT,
  last_error_message TEXT,
  PRIMARY KEY (origin_id, surface)
);

CREATE TABLE IF NOT EXISTS items (
  origin_id TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  channel_name TEXT NOT NULL,
  video_id TEXT NOT NULL,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  member_evidence INTEGER NOT NULL DEFAULT 0,
  member_confirmed INTEGER NOT NULL DEFAULT 0,
  access_class TEXT,
  availability TEXT,
  live_status TEXT,
  observed_live_status TEXT,
  release_timestamp INTEGER,
  last_probe_at TEXT,
  terminal_reason TEXT,
  PRIMARY KEY (origin_id, video_id)
);

CREATE TABLE IF NOT EXISTS surface_items (
  origin_id TEXT NOT NULL,
  video_id TEXT NOT NULL,
  surface TEXT NOT NULL,
  baseline INTEGER NOT NULL DEFAULT 0,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  PRIMARY KEY (origin_id, video_id, surface)
);

CREATE TABLE IF NOT EXISTS probes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id INTEGER NOT NULL,
  origin_id TEXT NOT NULL,
  video_id TEXT NOT NULL,
  probed_at TEXT NOT NULL,
  ok INTEGER NOT NULL,
  access_class TEXT NOT NULL,
  availability TEXT,
  live_status TEXT,
  release_timestamp INTEGER,
  error_kind TEXT,
  error_message TEXT,
  metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS probe_queue (
  origin_id TEXT NOT NULL,
  video_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  failure_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  suppress_notifications INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_error_kind TEXT,
  PRIMARY KEY (origin_id, video_id)
);

CREATE TABLE IF NOT EXISTS notification_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  origin_id TEXT NOT NULL,
  video_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  destination TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  available_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  telegram_message_id INTEGER,
  last_error TEXT,
  payload_json TEXT NOT NULL,
  UNIQUE (origin_id, video_id, event_type, destination)
);

CREATE TABLE IF NOT EXISTS dev_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_probe_queue_due
ON probe_queue(next_attempt_at, updated_at);

CREATE INDEX IF NOT EXISTS idx_notification_outbox_due
ON notification_outbox(status, available_at, created_at);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _lifecycle_event(
    previous: sqlite3.Row,
    outcome: ProbeOutcome,
    member_confirmed: bool,
    *,
    effective_live_status: str | None = None,
) -> str | None:
    if not member_confirmed:
        return None
    # A playlist sighting is useful membership evidence, but a failed or
    # terminal metadata probe is not a publish/live event. Membership denial
    # and YouTube's upcoming response are expected errors that themselves
    # carry useful lifecycle information.
    if outcome.error_kind and outcome.error_kind not in {
        "members_only_denied",
        "upcoming",
    }:
        return None
    previous_member = bool(previous["member_confirmed"])
    previous_live = str(previous["live_status"] or "")
    current_live = str(effective_live_status or outcome.live_status or "")
    if not current_live and outcome.access_class == "upcoming":
        current_live = "is_upcoming"
    if not previous_member:
        if current_live == "is_upcoming":
            return "upcoming"
        if current_live == "is_live":
            return "live"
        return "published"
    if previous_live == "is_upcoming" and current_live == "is_live":
        return "live"
    if previous_live in {"is_upcoming", "is_live"} and current_live in {
        "post_live",
        "was_live",
        "not_live",
    }:
        return "ended"
    return None


class AnonymousYoutubeInspector(YtDlpInspector):
    """A yt-dlp inspector constructed from an explicit anonymous allow-list.

    It uses a dev-specific executable setting. ``download.yt_dlp`` and
    ``download.extra_args`` are deliberately unreachable here. The inherited
    commands always include ``--ignore-config``.
    """

    def __init__(self, config: Config):
        dev = config.dev.youtube_membership
        safe_config = ObserverConfig(
            path=config.path,
            data_dir=config.app.data_dir / "dev",
            yt_dlp=dev.yt_dlp,
            poll_interval_seconds=dev.poll_interval_seconds,
            request_timeout_seconds=dev.request_timeout_seconds,
            request_spacing_seconds=dev.request_spacing_seconds,
            tab_limit=dev.tab_limit,
            seed_probe_keyword_limit_per_channel=0,
            anonymous_tabs=["streams"],
            anonymous_members_playlist=True,
            stop_at=None,
            log_level=config.app.log_level,
            channels=[],
            retry_probe_limit_per_channel=MAX_PROBE_FAILURES,
            pause_hours_on_challenge=CHALLENGE_COOLDOWN_HOURS,
        )
        super().__init__(safe_config)


class _DevStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(items)")
        }
        if "observed_live_status" not in columns:
            try:
                self.conn.execute(
                    "ALTER TABLE items ADD COLUMN observed_live_status TEXT"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        surface_columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(surface_items)")
        }
        if "baseline" not in surface_columns:
            try:
                self.conn.execute(
                    "ALTER TABLE surface_items ADD COLUMN baseline INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        # Preserve first-cycle notification suppression for databases created
        # before ``surface_items.baseline`` existed.  Those rows were already
        # present when their surface was seeded, so treating them as new would
        # emit a burst of historical notifications after an upgrade.
        self.conn.execute(
            """
            UPDATE surface_items
            SET baseline = 1
            WHERE baseline = 0
              AND EXISTS (
                SELECT 1
                FROM surface_state
                WHERE surface_state.origin_id = surface_items.origin_id
                  AND surface_state.surface = surface_items.surface
                  AND surface_state.seeded_at IS NOT NULL
                  AND surface_items.first_seen_at <= surface_state.seeded_at
              )
            """
        )
        self.conn.commit()
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        self.conn.close()

    def recover_sending_notifications(self) -> None:
        # Call only while holding the process-level cycle lock. A second
        # status/once process may open SQLite while the active sender is inside
        # Telegram, and must not mark that live claim as uncertain.
        self.conn.execute(
            """
            UPDATE notification_outbox
            SET status = 'uncertain', updated_at = ?,
                last_error = COALESCE(last_error, 'sender stopped after claim')
            WHERE status = 'sending'
            """,
            (_iso_now(),),
        )
        self.conn.commit()

    def begin_cycle(self) -> int:
        cursor = self.conn.execute(
            "INSERT INTO cycles (started_at) VALUES (?)",
            (_iso_now(),),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def finish_cycle(self, cycle_id: int, stats: dict[str, object], *, success: bool) -> None:
        self.conn.execute(
            """
            UPDATE cycles
            SET finished_at = ?, success = ?, stats_json = ?
            WHERE id = ?
            """,
            (_iso_now(), int(success), json.dumps(stats, sort_keys=True), cycle_id),
        )
        self.conn.commit()

    def surface_seeded(self, origin_id: str, surface: str) -> bool:
        row = self.conn.execute(
            "SELECT seeded_at FROM surface_state WHERE origin_id = ? AND surface = ?",
            (origin_id, surface),
        ).fetchone()
        return bool(row and row["seeded_at"])

    def record_surface_success(self, origin_id: str, surface: str, observed_at: str) -> None:
        self.conn.execute(
            """
            INSERT INTO surface_state (
              origin_id, surface, seeded_at, last_success_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(origin_id, surface) DO UPDATE SET
              seeded_at = COALESCE(surface_state.seeded_at, excluded.seeded_at),
              last_success_at = excluded.last_success_at,
              last_error_at = NULL,
              last_error_kind = NULL,
              last_error_message = NULL
            """,
            (origin_id, surface, observed_at, observed_at),
        )
        self.conn.commit()

    def record_surface_error(
        self,
        origin_id: str,
        surface: str,
        kind: str,
        message: str,
    ) -> None:
        now = _iso_now()
        self.conn.execute(
            """
            INSERT INTO surface_state (
              origin_id, surface, last_error_at, last_error_kind,
              last_error_message
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(origin_id, surface) DO UPDATE SET
              last_error_at = excluded.last_error_at,
              last_error_kind = excluded.last_error_kind,
              last_error_message = excluded.last_error_message
            """,
            (origin_id, surface, now, kind, message[:1000]),
        )
        self.conn.commit()

    def record_surface_item(
        self,
        origin: Origin,
        surface: str,
        item: SurfaceItem,
        observed_at: str,
        *,
        member_evidence: bool,
        baseline: bool,
    ) -> tuple[bool, sqlite3.Row | None, bool]:
        previous = self.item(origin.id, item.video_id)
        surface_row = self.conn.execute(
            """
            SELECT baseline FROM surface_items
            WHERE origin_id = ? AND video_id = ? AND surface = ?
            """,
            (origin.id, item.video_id, surface),
        ).fetchone()
        self.conn.execute(
            """
            INSERT INTO items (
              origin_id, channel_id, channel_name, video_id, title, url,
              first_seen_at, last_seen_at, member_evidence, availability,
              live_status, observed_live_status, release_timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(origin_id, video_id) DO UPDATE SET
              channel_id = excluded.channel_id,
              channel_name = excluded.channel_name,
              title = excluded.title,
              url = excluded.url,
              last_seen_at = excluded.last_seen_at,
              member_evidence = MAX(items.member_evidence, excluded.member_evidence),
              availability = CASE
                WHEN items.last_probe_at IS NULL
                THEN COALESCE(excluded.availability, items.availability)
                ELSE items.availability
              END,
              live_status = CASE
                WHEN items.last_probe_at IS NULL
                THEN COALESCE(excluded.live_status, items.live_status)
                ELSE items.live_status
              END,
              observed_live_status = COALESCE(
                excluded.observed_live_status, items.observed_live_status
              ),
              release_timestamp = CASE
                WHEN items.last_probe_at IS NULL
                THEN COALESCE(excluded.release_timestamp, items.release_timestamp)
                ELSE items.release_timestamp
              END
            """,
            (
                origin.id,
                origin.external_id,
                origin.name,
                item.video_id,
                item.title,
                item.url,
                observed_at,
                observed_at,
                int(member_evidence),
                item.availability,
                item.live_status,
                item.live_status,
                item.release_timestamp,
            ),
        )
        self.conn.execute(
            """
            INSERT INTO surface_items (
              origin_id, video_id, surface, baseline, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(origin_id, video_id, surface) DO UPDATE SET
              last_seen_at = excluded.last_seen_at
            """,
            (
                origin.id,
                item.video_id,
                surface,
                int(baseline),
                observed_at,
                observed_at,
            ),
        )
        self.conn.commit()
        surface_is_baseline = (
            bool(surface_row["baseline"]) if surface_row is not None else baseline
        )
        return surface_row is None, previous, surface_is_baseline

    def item(self, origin_id: str, video_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM items WHERE origin_id = ? AND video_id = ?",
            (origin_id, video_id),
        ).fetchone()

    def queue_probe(
        self,
        origin_id: str,
        video_id: str,
        *,
        reason: str,
        suppress_notifications: bool,
        available_at: str | None = None,
    ) -> bool:
        item = self.item(origin_id, video_id)
        if item is None or item["terminal_reason"]:
            return False
        now = _iso_now()
        due = available_at or now
        self.conn.execute(
            """
            INSERT INTO probe_queue (
              origin_id, video_id, reason, next_attempt_at,
              suppress_notifications, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(origin_id, video_id) DO UPDATE SET
              reason = excluded.reason,
              next_attempt_at = MIN(probe_queue.next_attempt_at, excluded.next_attempt_at),
              -- Once work was seeded as baseline it stays silent until a
              -- usable probe completes, even when backlog spills into later
              -- cycles and those surfaces are now marked seeded.
              suppress_notifications = probe_queue.suppress_notifications,
              updated_at = excluded.updated_at
            """,
            (
                origin_id,
                video_id,
                reason,
                due,
                int(suppress_notifications),
                now,
                now,
            ),
        )
        self.conn.commit()
        return True

    def due_probes(self, origin_id: str, *, limit: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT q.*, i.title, i.url, i.channel_name, i.channel_id,
                   i.member_evidence, i.member_confirmed, i.access_class,
                   i.availability, i.live_status, i.observed_live_status,
                   i.release_timestamp,
                   i.terminal_reason
            FROM probe_queue q
            JOIN items i
              ON i.origin_id = q.origin_id AND i.video_id = q.video_id
            WHERE q.origin_id = ? AND q.next_attempt_at <= ?
              AND i.terminal_reason IS NULL
            ORDER BY q.updated_at, q.created_at
            LIMIT ?
            """,
            (origin_id, _iso_now(), limit),
        ).fetchall()

    def record_probe(
        self,
        *,
        cycle_id: int,
        row: sqlite3.Row,
        outcome: ProbeOutcome,
        poll_interval_seconds: int,
        notification_destination: str,
        suppress_notification: bool,
    ) -> tuple[str | None, bool]:
        now = _iso_now()
        previous = self.item(str(row["origin_id"]), str(row["video_id"]))
        assert previous is not None
        direct_member = (
            outcome.availability == "subscriber_only"
            or outcome.access_class in {"members_only", "members_only_accessible"}
            or outcome.error_kind == "members_only_denied"
        )
        evidence_is_usable = outcome.error_kind in {
            None,
            "members_only_denied",
            "upcoming",
        }
        member_confirmed = bool(
            previous["member_confirmed"]
            or (
                evidence_is_usable
                and (previous["member_evidence"] or direct_member)
            )
        )
        live_status = outcome.live_status
        if live_status is None and outcome.access_class == "upcoming":
            live_status = "is_upcoming"
        if live_status is None and row["observed_live_status"] in {
            "is_upcoming",
            "is_live",
            "post_live",
            "was_live",
            "not_live",
        }:
            live_status = str(row["observed_live_status"])
        terminal_reason: str | None = None
        failure_count = int(row["failure_count"])
        retryable = outcome.error_kind in RETRYABLE_PROBE_ERRORS
        if outcome.error_kind in TERMINAL_PROBE_ERRORS:
            terminal_reason = outcome.error_kind
        elif retryable:
            failure_count += 1
            if failure_count >= MAX_PROBE_FAILURES:
                terminal_reason = f"retry_exhausted:{outcome.error_kind}"
        else:
            # The cap is for consecutive failures. A healthy metadata result
            # resets intermittent network/no-format history for active streams.
            failure_count = 0
        event_type = _lifecycle_event(
            previous,
            outcome,
            member_confirmed,
            effective_live_status=live_status,
        )
        notification_queued = False
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO probes (
                  cycle_id, origin_id, video_id, probed_at, ok, access_class,
                  availability, live_status, release_timestamp, error_kind,
                  error_message, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle_id,
                    row["origin_id"],
                    row["video_id"],
                    now,
                    int(outcome.ok),
                    outcome.access_class,
                    outcome.availability,
                    live_status,
                    outcome.release_timestamp,
                    outcome.error_kind,
                    (outcome.error_message or "")[:1000] or None,
                    json.dumps(outcome.metadata, ensure_ascii=False, sort_keys=True),
                ),
            )
            self.conn.execute(
                """
                UPDATE items
                SET member_confirmed = ?, access_class = ?,
                    availability = COALESCE(?, availability),
                    live_status = COALESCE(?, live_status),
                    release_timestamp = COALESCE(?, release_timestamp),
                    last_probe_at = ?, terminal_reason = COALESCE(?, terminal_reason)
                WHERE origin_id = ? AND video_id = ?
                """,
                (
                    int(member_confirmed),
                    outcome.access_class,
                    outcome.availability,
                    live_status,
                    outcome.release_timestamp,
                    now,
                    terminal_reason,
                    row["origin_id"],
                    row["video_id"],
                ),
            )

            if terminal_reason or (
                not retryable and live_status not in {"is_upcoming", "is_live"}
            ):
                self.conn.execute(
                    "DELETE FROM probe_queue WHERE origin_id = ? AND video_id = ?",
                    (row["origin_id"], row["video_id"]),
                )
            else:
                delay_seconds = poll_interval_seconds
                if retryable:
                    delay_seconds = min(
                        poll_interval_seconds,
                        60 * (2 ** max(0, failure_count - 1)),
                    )
                next_attempt = (
                    _utc_now() + timedelta(seconds=delay_seconds)
                ).isoformat()
                self.conn.execute(
                    """
                    UPDATE probe_queue
                    SET failure_count = ?, next_attempt_at = ?, updated_at = ?,
                        last_error_kind = ?, suppress_notifications = ?
                    WHERE origin_id = ? AND video_id = ?
                    """,
                    (
                        failure_count,
                        next_attempt,
                        now,
                        outcome.error_kind,
                        (
                            int(row["suppress_notifications"])
                            if retryable
                            else 0
                        ),
                        row["origin_id"],
                        row["video_id"],
                    ),
                )

            if event_type:
                payload = {
                    "channel_name": str(row["channel_name"]),
                    "title": str(row["title"]),
                    "url": str(row["url"]),
                    "event_type": event_type,
                }
                cursor = self.conn.execute(
                    """
                    INSERT OR IGNORE INTO notification_outbox (
                      origin_id, video_id, event_type, destination, status,
                      available_at, created_at, updated_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["origin_id"],
                        row["video_id"],
                        event_type,
                        notification_destination,
                        "suppressed" if suppress_notification else "pending",
                        now,
                        now,
                        now,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    ),
                )
                notification_queued = cursor.rowcount > 0
        return event_type, notification_queued

    def claim_notification(self) -> sqlite3.Row | None:
        row = self.conn.execute(
            """
            SELECT * FROM notification_outbox
            WHERE status = 'pending' AND available_at <= ?
            ORDER BY created_at, id LIMIT 1
            """,
            (_iso_now(),),
        ).fetchone()
        if row is None:
            return None
        self.conn.execute(
            """
            UPDATE notification_outbox
            SET status = 'sending', attempts = attempts + 1, updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (_iso_now(), row["id"]),
        )
        self.conn.commit()
        return self.conn.execute(
            "SELECT * FROM notification_outbox WHERE id = ?",
            (row["id"],),
        ).fetchone()

    def finish_notification(
        self,
        notification_id: int,
        *,
        status: str,
        message_id: int | None = None,
        error: str | None = None,
    ) -> None:
        row = self.conn.execute(
            "SELECT attempts FROM notification_outbox WHERE id = ?",
            (notification_id,),
        ).fetchone()
        attempts = int(row["attempts"]) if row else MAX_PROBE_FAILURES
        final_status = status
        available_at = _iso_now()
        if status == "failed" and attempts < MAX_PROBE_FAILURES:
            final_status = "pending"
            available_at = (
                _utc_now() + timedelta(seconds=min(1800, 30 * (2 ** (attempts - 1))))
            ).isoformat()
        self.conn.execute(
            """
            UPDATE notification_outbox
            SET status = ?, available_at = ?, updated_at = ?,
                telegram_message_id = ?, last_error = ?
            WHERE id = ?
            """,
            (
                final_status,
                available_at,
                _iso_now(),
                message_id,
                (error or "")[:1000] or None,
                notification_id,
            ),
        )
        self.conn.commit()

    def get_state(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM dev_state WHERE key = ?",
            (key,),
        ).fetchone()
        return str(row["value"]) if row else None

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO dev_state (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    def delete_state(self, key: str) -> None:
        self.conn.execute("DELETE FROM dev_state WHERE key = ?", (key,))
        self.conn.commit()

    def status(self, *, limit: int) -> dict[str, object]:
        count_queries = {
            "cycles": "SELECT COUNT(*) FROM cycles",
            "items": "SELECT COUNT(*) FROM items",
            "member_confirmed": "SELECT COUNT(*) FROM items WHERE member_confirmed = 1",
            "pending_probes": "SELECT COUNT(*) FROM probe_queue",
            "pending_notifications": "SELECT COUNT(*) FROM notification_outbox WHERE status = 'pending'",
            "sent_notifications": "SELECT COUNT(*) FROM notification_outbox WHERE status = 'sent'",
            "suppressed_notifications": "SELECT COUNT(*) FROM notification_outbox WHERE status = 'suppressed'",
            "uncertain_notifications": "SELECT COUNT(*) FROM notification_outbox WHERE status = 'uncertain'",
        }
        counts = {
            key: int(self.conn.execute(query).fetchone()[0])
            for key, query in count_queries.items()
        }
        notifications = []
        for row in self.conn.execute(
            """
            SELECT origin_id, video_id, event_type, destination, status,
                   attempts, created_at, updated_at, telegram_message_id,
                   last_error, payload_json
            FROM notification_outbox ORDER BY id DESC LIMIT ?
            """,
            (max(0, limit),),
        ).fetchall():
            item = dict(row)
            item["payload"] = json.loads(str(item.pop("payload_json")))
            notifications.append(item)
        return {
            "counts": counts,
            "cooldown_until": self.get_state("cooldown_until"),
            "cooldown_reason": self.get_state("cooldown_reason"),
            "recent_notifications": notifications,
        }


class YoutubeMembershipDevRunner:
    """Run the default-off, anonymous membership notification experiment."""

    def __init__(
        self,
        config: Config,
        *,
        inspector: AnonymousYoutubeInspector | None = None,
        notifier: TelegramTextNotifier | None = None,
        feed_fetcher: Callable[[str], bytes] = fetch_feed,
        stop_event: threading.Event | None = None,
    ):
        self.config = config
        self.dev_config = config.dev.youtube_membership
        origins_by_id = {origin.id: origin for origin in config.origins}
        self.origins = [
            origins_by_id[origin_id]
            for origin_id in self.dev_config.origin_ids
            if origin_id in origins_by_id
        ]
        channel_ids = [origin.external_id for origin in self.origins]
        if len(channel_ids) != len(set(channel_ids)):
            raise ValueError(
                "dev.youtube_membership origin_ids must reference unique YouTube channels"
            )
        self.logger = logging.getLogger("asmr_tg_backup.dev.youtube_membership")
        self.data_dir = config.app.data_dir / "dev"
        self.db_path = self.data_dir / "youtube-membership.db"
        self.lock_path = self.data_dir / "youtube-membership.lock"
        self.store = _DevStore(self.db_path)
        self.inspector = inspector or AnonymousYoutubeInspector(config)
        self.notifier = notifier or TelegramTextNotifier(config.telegram)
        self.feed_fetcher = feed_fetcher
        self.stop_event = stop_event or threading.Event()
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self.store.close()
        self._closed = True

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            started_at = time.monotonic()
            failed = False
            try:
                self.run_once()
            except Exception:
                failed = True
                self.logger.exception("dev YouTube membership cycle failed")
            elapsed = time.monotonic() - started_at
            interval = 10 if failed else self.dev_config.poll_interval_seconds
            self.stop_event.wait(
                max(0.0, interval - elapsed)
            )

    def run_once(self) -> dict[str, object]:
        if not self.dev_config.enabled:
            raise RuntimeError("dev.youtube_membership is disabled")
        with self._exclusive_cycle_lock():
            return self._run_locked()

    def status(self, limit: int = 20) -> dict[str, object]:
        payload = self.store.status(limit=limit)
        return {
            "enabled": self.dev_config.enabled,
            "notify": self.dev_config.notify,
            "origin_ids": list(self.dev_config.origin_ids),
            "db_path": str(self.db_path),
            **payload,
        }

    def _run_locked(self) -> dict[str, object]:
        self.store.recover_sending_notifications()
        cycle_id = self.store.begin_cycle()
        stats: dict[str, object] = {
            "cycle_id": cycle_id,
            "origins": len(self.origins),
            "surface_items": 0,
            "surface_errors": 0,
            "probes": 0,
            "notifications_queued": 0,
            "notifications_sent": 0,
            "cooldown": False,
        }
        success = False
        try:
            if self._cooldown_active():
                stats["cooldown"] = True
                if not self.stop_event.is_set():
                    stats["notifications_sent"] = self._flush_notifications()
                success = True
                return stats
            for origin in self.origins:
                if self.stop_event.is_set() or self._cooldown_active():
                    break
                self._observe_origin(cycle_id, origin, stats)
            if not self.stop_event.is_set():
                stats["notifications_sent"] = self._flush_notifications()
            success = int(stats["surface_errors"]) == 0
            return stats
        finally:
            self.store.finish_cycle(cycle_id, stats, success=success)

    def _observe_origin(
        self,
        cycle_id: int,
        origin: Origin,
        stats: dict[str, object],
    ) -> None:
        surfaces: list[tuple[str, Callable[[], list[SurfaceItem]]]] = [
            (
                "members_playlist",
                lambda: self.inspector.list_members_playlist(origin.external_id),
            ),
            (
                "streams",
                lambda: self.inspector.list_tab(origin.external_id, "streams"),
            ),
            ("atom", lambda: self._atom_items(origin)),
        ]
        for surface, loader in surfaces:
            if self.stop_event.is_set() or self._cooldown_active():
                break
            seeded = self.store.surface_seeded(origin.id, surface)
            try:
                items = loader()
            except InspectionError as exc:
                self._record_surface_failure(origin, surface, exc.kind, exc.message, stats)
                if exc.kind in GLOBAL_CHALLENGE_ERRORS:
                    self._set_cooldown(exc.kind)
                continue
            except Exception as exc:
                kind = classify_probe_error(str(exc))
                self._record_surface_failure(origin, surface, kind, str(exc), stats)
                if kind in GLOBAL_CHALLENGE_ERRORS:
                    self._set_cooldown(kind)
                continue

            observed_at = _iso_now()
            baseline_probe_count = 0
            for item in items:
                member_evidence = (
                    surface == "members_playlist"
                    or item.availability == "subscriber_only"
                )
                new_to_surface, previous, surface_is_baseline = (
                    self.store.record_surface_item(
                        origin,
                        surface,
                        item,
                        observed_at,
                        member_evidence=member_evidence,
                        baseline=not seeded,
                    )
                )
                stats["surface_items"] = int(stats["surface_items"]) + 1
                should_probe = False
                suppress = surface_is_baseline
                if seeded and new_to_surface:
                    should_probe = (
                        member_evidence
                        or bool(MEMBERSHIP_TITLE_RE.search(item.title))
                        or item.live_status in {"is_upcoming", "is_live", "post_live"}
                    )
                elif not seeded and baseline_probe_count < MAX_PROBE_FAILURES:
                    should_probe = member_evidence or bool(
                        MEMBERSHIP_TITLE_RE.search(item.title)
                    )
                    if should_probe:
                        baseline_probe_count += 1
                if previous and (
                    previous["live_status"] in {"is_upcoming", "is_live"}
                ):
                    should_probe = True
                    # Baseline suppression applies only before a channel surface
                    # has ever completed successfully.
                    suppress = not seeded
                if item.live_status in {"is_upcoming", "is_live"} and member_evidence:
                    should_probe = True
                current = self.store.item(origin.id, item.video_id)
                if current and current["last_probe_at"] is not None:
                    suppress = False
                if (
                    not new_to_surface
                    and current
                    and current["last_probe_at"] is None
                    and (
                        member_evidence
                        or bool(MEMBERSHIP_TITLE_RE.search(item.title))
                        or item.live_status
                        in {"is_upcoming", "is_live", "post_live"}
                    )
                ):
                    # Reconcile a crash after persisting the discovery but
                    # before persisting its queue row.
                    should_probe = True
                if should_probe:
                    self.store.queue_probe(
                        origin.id,
                        item.video_id,
                        reason=f"surface:{surface}",
                        suppress_notifications=suppress,
                    )
            self.store.record_surface_success(origin.id, surface, observed_at)

        for row in self.store.due_probes(origin.id, limit=MAX_PROBE_FAILURES):
            if self.stop_event.is_set() or self._cooldown_active():
                break
            try:
                outcome = self.inspector.probe(str(row["url"]))
            except InspectionError as exc:
                outcome = ProbeOutcome(
                    ok=False,
                    access_class="probe_error",
                    error_kind=exc.kind,
                    error_message=exc.message,
                )
            except Exception as exc:
                kind = classify_probe_error(str(exc))
                outcome = ProbeOutcome(
                    ok=False,
                    access_class="probe_error",
                    error_kind=kind,
                    error_message=str(exc),
                )
            destination = self.dev_config.chat_id or self.config.telegram.chat_id
            event_type, queued = self.store.record_probe(
                cycle_id=cycle_id,
                row=row,
                outcome=outcome,
                poll_interval_seconds=self.dev_config.poll_interval_seconds,
                notification_destination=destination,
                suppress_notification=(
                    bool(row["suppress_notifications"])
                    or not self.dev_config.notify
                ),
            )
            stats["probes"] = int(stats["probes"]) + 1
            if event_type and queued:
                stats["notifications_queued"] = (
                    int(stats["notifications_queued"]) + 1
                )
            if outcome.error_kind in GLOBAL_CHALLENGE_ERRORS:
                self._set_cooldown(str(outcome.error_kind))
                break

    def _atom_items(self, origin: Origin) -> list[SurfaceItem]:
        url = (
            "https://www.youtube.com/feeds/videos.xml?channel_id="
            f"{origin.external_id}"
        )
        entries = parse_feed(self.feed_fetcher(url), origin.id, origin.name)
        return [
            SurfaceItem(
                video_id=entry.video_id,
                title=entry.title,
                url=entry.url,
                position=index,
                source_timestamp=entry.published_at,
            )
            for index, entry in enumerate(entries, start=1)
        ]

    def _record_surface_failure(
        self,
        origin: Origin,
        surface: str,
        kind: str,
        message: str,
        stats: dict[str, object],
    ) -> None:
        self.store.record_surface_error(origin.id, surface, kind, message)
        stats["surface_errors"] = int(stats["surface_errors"]) + 1
        self.logger.warning(
            "dev membership surface failed origin=%s surface=%s kind=%s",
            origin.id,
            surface,
            kind,
        )

    def _flush_notifications(self) -> int:
        if not self.dev_config.notify:
            return 0
        sent = 0
        while not self.stop_event.is_set():
            row = self.store.claim_notification()
            if row is None:
                break
            payload = json.loads(str(row["payload_json"]))
            text = self._notification_text(payload)
            try:
                message_id = self.notifier.send_text(
                    text,
                    chat_id=str(row["destination"]),
                    timeout_seconds=min(
                        60,
                        max(1, self.config.telegram.upload_timeout_seconds),
                    ),
                )
            except TelegramUploadError as exc:
                self.store.finish_notification(
                    int(row["id"]),
                    status="uncertain" if exc.uncertain else "failed",
                    error=str(exc),
                )
                continue
            except Exception as exc:
                self.store.finish_notification(
                    int(row["id"]),
                    status="failed",
                    error=str(exc),
                )
                continue
            self.store.finish_notification(
                int(row["id"]),
                status="sent",
                message_id=message_id,
            )
            sent += 1
        return sent

    @staticmethod
    def _notification_text(payload: dict[str, object]) -> str:
        labels = {
            "upcoming": "YouTube 会员预约",
            "live": "YouTube 会员直播开始",
            "ended": "YouTube 会员直播结束",
            "published": "发现 YouTube 会员内容",
        }
        event_type = str(payload.get("event_type") or "published")
        return (
            f"🧪 DEV · {labels.get(event_type, labels['published'])}\n"
            f"{payload.get('channel_name', '')}\n"
            f"{payload.get('title', '')}\n"
            f"{payload.get('url', '')}\n\n"
            "匿名元数据通知；未使用 cookies，也不会下载媒体。"
        )

    def _cooldown_active(self) -> bool:
        value = self.store.get_state("cooldown_until")
        until = _parse_datetime(value)
        if until is None:
            if value:
                self.store.delete_state("cooldown_until")
                self.store.delete_state("cooldown_reason")
            return False
        if _utc_now() >= until:
            self.store.delete_state("cooldown_until")
            self.store.delete_state("cooldown_reason")
            return False
        return True

    def _set_cooldown(self, reason: str) -> None:
        until = _utc_now() + timedelta(hours=CHALLENGE_COOLDOWN_HOURS)
        self.store.set_state("cooldown_until", until.isoformat())
        self.store.set_state("cooldown_reason", reason)
        self.logger.warning(
            "dev YouTube membership requests paused until=%s reason=%s",
            until.isoformat(),
            reason,
        )

    @contextmanager
    def _exclusive_cycle_lock(self) -> Iterator[None]:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            try:
                self.lock_path.chmod(0o600)
            except OSError:
                pass
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    "another dev YouTube membership cycle is already running"
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = [
    "AnonymousYoutubeInspector",
    "ProbeOutcome",
    "SurfaceItem",
    "YoutubeMembershipDevRunner",
]
