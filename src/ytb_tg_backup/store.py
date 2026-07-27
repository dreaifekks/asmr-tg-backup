from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import uuid

from .feed import FeedEntry
from .models import ClaimedJob, MediaCandidate, Origin


LEGACY_SCHEMA = """
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

# Kept as a compatibility alias for migration fixtures and external imports.
SCHEMA = LEGACY_SCHEMA

V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS origins (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  kind TEXT NOT NULL,
  external_id TEXT NOT NULL,
  name TEXT NOT NULL,
  managed_by TEXT NOT NULL,
  options_json TEXT NOT NULL DEFAULT '{}',
  enabled INTEGER NOT NULL DEFAULT 1,
  bootstrap TEXT NOT NULL DEFAULT 'latest',
  credential_ref TEXT,
  created_at TEXT NOT NULL,
  created_by TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS origin_poll_state (
  origin_id TEXT PRIMARY KEY REFERENCES origins(id) ON DELETE CASCADE,
  cursor TEXT,
  etag TEXT,
  last_modified TEXT,
  last_polled_at TEXT,
  last_success_at TEXT,
  last_error_code TEXT,
  last_error TEXT,
  next_poll_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL,
  content_kind TEXT NOT NULL,
  external_id TEXT NOT NULL,
  title TEXT NOT NULL,
  canonical_url TEXT NOT NULL,
  published_at TEXT,
  source_updated_at TEXT,
  live_status TEXT,
  visibility TEXT NOT NULL DEFAULT 'public',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  UNIQUE(provider, content_kind, external_id)
);

CREATE TABLE IF NOT EXISTS origin_items (
  origin_id TEXT NOT NULL REFERENCES origins(id) ON DELETE CASCADE,
  media_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
  disposition TEXT NOT NULL DEFAULT 'eligible',
  decision_code TEXT,
  decision_reason TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(origin_id, media_id)
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  media_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
  job_type TEXT NOT NULL,
  target_key TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL,
  failure_count INTEGER NOT NULL DEFAULT 0,
  max_failures INTEGER NOT NULL,
  available_at TEXT NOT NULL,
  lease_owner TEXT,
  lease_token TEXT,
  lease_until TEXT,
  reason_code TEXT,
  last_error TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  UNIQUE(media_id, job_type, target_key)
);

CREATE INDEX IF NOT EXISTS idx_jobs_claim
ON jobs(job_type, state, available_at, lease_until, id);

CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  media_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  part_no INTEGER NOT NULL DEFAULT 0,
  path TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'ready',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(media_id, role, part_no)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_resource_library
ON artifacts(role, state, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_origin_items_media_resource
ON origin_items(media_id, disposition, first_seen_at, origin_id);

CREATE TABLE IF NOT EXISTS purge_path_reservations (
  storage_key TEXT PRIMARY KEY,
  media_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
  operation_id TEXT NOT NULL,
  owner_pid INTEGER NOT NULL,
  owner_start_id TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_purge_path_reservations_operation
ON purge_path_reservations(operation_id);

CREATE INDEX IF NOT EXISTS idx_purge_path_reservations_state
ON purge_path_reservations(state, operation_id);

CREATE TABLE IF NOT EXISTS deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  media_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
  artifact_id INTEGER REFERENCES artifacts(id),
  sink TEXT NOT NULL,
  destination_key TEXT NOT NULL,
  remote_id TEXT NOT NULL,
  delivered_at TEXT NOT NULL,
  UNIQUE(media_id, sink, destination_key)
);

CREATE TABLE IF NOT EXISTS bot_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS panel_snapshots (
  cache_key TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  dirty INTEGER NOT NULL DEFAULT 1,
  generated_at TEXT NOT NULL
);
"""

DISK_RESOURCE_CTE = """
WITH anchor_ids AS (
  SELECT
    media_id,
    COALESCE(
      MIN(CASE WHEN role='master' AND part_no=0 THEN id END),
      MIN(CASE WHEN role='live_segment' THEN id END)
    ) AS artifact_id
  FROM artifacts
  WHERE state IN ('ready', 'staged', 'suppressed', 'purging', 'purge_failed')
  GROUP BY media_id
),
resources AS (
  SELECT
    a.id AS artifact_id,
    a.media_id,
    a.role AS anchor_role,
    a.path AS master_path,
    a.size_bytes AS master_recorded_bytes,
    a.state AS master_state,
    a.created_at AS artifact_created_at,
    a.updated_at AS artifact_updated_at,
    mi.provider,
    mi.content_kind,
    mi.external_id,
    mi.title,
    mi.canonical_url,
    mi.published_at,
    mi.metadata_json AS media_metadata_json,
    COALESCE(
      (
        SELECT o.name
        FROM origin_items oi
        JOIN origins o ON o.id=oi.origin_id
        WHERE oi.media_id=mi.id
        ORDER BY
          CASE oi.disposition WHEN 'eligible' THEN 0 ELSE 1 END,
          oi.first_seen_at,
          oi.origin_id
        LIMIT 1
      ),
      mi.provider
    ) AS origin_name,
    EXISTS(
      SELECT 1
      FROM deliveries d
      WHERE d.media_id=mi.id AND d.sink='telegram'
    ) AS delivered,
    (
      SELECT j.state
      FROM jobs j
      WHERE j.media_id=mi.id AND j.job_type='telegram_delivery'
      ORDER BY
        CASE j.state
          WHEN 'running' THEN 0
          WHEN 'queued' THEN 1
          WHEN 'retry' THEN 2
          WHEN 'uncertain' THEN 3
          WHEN 'blocked' THEN 4
          ELSE 5
        END,
        j.updated_at DESC,
        j.id DESC
      LIMIT 1
    ) AS delivery_job_state,
    EXISTS(
      SELECT 1
      FROM jobs j
      WHERE j.media_id=mi.id AND j.state='running'
    ) AS running,
    CASE
      WHEN mi.content_kind='live_stream'
        OR json_extract(mi.metadata_json, '$.recording_mode')='live'
      THEN 1 ELSE 0
    END AS irreplaceable_live,
    (
      SELECT COUNT(*)
      FROM artifacts related
      WHERE related.media_id=mi.id AND related.state!='purged'
    ) AS artifact_count,
    (
      SELECT COALESCE(SUM(related.size_bytes), 0)
      FROM artifacts related
      WHERE related.media_id=mi.id AND related.state!='purged'
    ) AS recorded_bytes,
    (
      mi.title || ' ' || mi.external_id || ' ' || mi.provider || ' ' ||
      mi.content_kind || ' ' ||
      COALESCE(
        (
          SELECT o.name
          FROM origin_items oi
          JOIN origins o ON o.id=oi.origin_id
          WHERE oi.media_id=mi.id
          ORDER BY
            CASE oi.disposition WHEN 'eligible' THEN 0 ELSE 1 END,
            oi.first_seen_at,
            oi.origin_id
          LIMIT 1
        ),
        ''
      )
    ) AS search_text
  FROM anchor_ids anchor
  JOIN artifacts a ON a.id=anchor.artifact_id
  JOIN media_items mi ON mi.id=a.media_id
  WHERE anchor.artifact_id IS NOT NULL
)
"""


@dataclass(frozen=True)
class Subscription:
    id: str
    name: str
    channel_id: str
    routes: list[str]
    enabled: bool


class Store:
    CURRENT_SCHEMA_VERSION = 2

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self.conn = sqlite3.connect(self.path, timeout=30)
        self._closed = False
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        self._harden_sqlite_permissions()

    def close(self) -> None:
        if self._closed:
            return
        self.conn.close()
        self._closed = True
        self._harden_sqlite_permissions()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def initialize(self) -> None:
        has_migrations = self._table_exists("schema_migrations")
        current = (
            self.conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
            if has_migrations
            else 0
        )
        if int(current) >= self.CURRENT_SCHEMA_VERSION:
            self.conn.executescript(V2_SCHEMA)
            self._ensure_panel_snapshot_support()
            self._recover_incomplete_disk_purges()
            self._ensure_compatibility_views()
            self.conn.commit()
            return

        has_v1 = self._table_exists("videos") or self._table_exists("subscriptions")
        if has_v1:
            self._backup_v1()
        self.conn.executescript(V2_SCHEMA)
        self._ensure_panel_snapshot_support()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._recover_incomplete_disk_purges()
            if has_v1:
                self._migrate_v1_rows()
                if self._table_exists("videos"):
                    self.conn.execute("ALTER TABLE videos RENAME TO videos_v1")
                if self._table_exists("subscriptions"):
                    self.conn.execute("ALTER TABLE subscriptions RENAME TO subscriptions_v1")
            self.conn.execute(
                "INSERT OR REPLACE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (self.CURRENT_SCHEMA_VERSION, now_iso()),
            )
            self._ensure_compatibility_views()
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _recover_incomplete_disk_purges(self) -> None:
        """Make interrupted panel purges visible and retryable after restart."""

        recovered_at = now_iso()
        reservation_rows = self.conn.execute(
            """
            SELECT storage_key, operation_id, owner_pid, owner_start_id
            FROM purge_path_reservations
            WHERE state='active'
            """
        ).fetchall()
        live_operations = {
            str(row["operation_id"])
            for row in reservation_rows
            if _process_instance_is_alive(
                int(row["owner_pid"]),
                str(row["owner_start_id"]),
            )
        }
        rows = self.conn.execute(
            """
            SELECT id, metadata_json
            FROM artifacts
            WHERE state='purging'
            """
        ).fetchall()
        for row in rows:
            metadata = _json_object(row["metadata_json"])
            purge_metadata = _json_object(metadata.get("local_purge"))
            operation_id = str(purge_metadata.get("operation_id") or "")
            if operation_id and operation_id in live_operations:
                continue
            purge_metadata.update(
                {
                    "finished_at": recovered_at,
                    "result": "interrupted",
                    "error": "service stopped before local deletion completed",
                }
            )
            metadata["local_purge"] = purge_metadata
            self.conn.execute(
                """
                UPDATE artifacts
                SET state='purge_failed', metadata_json=?, updated_at=?
                WHERE id=?
                """,
                (
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    recovered_at,
                    int(row["id"]),
                ),
            )
        for reservation in reservation_rows:
            operation_id = str(reservation["operation_id"])
            if operation_id in live_operations:
                continue
            storage_key = str(reservation["storage_key"])
            if _path_entry_exists(Path(storage_key)):
                self.conn.execute(
                    """
                    DELETE FROM purge_path_reservations
                    WHERE storage_key=? AND state='active'
                    """,
                    (storage_key,),
                )
            else:
                self.conn.execute(
                    """
                    UPDATE purge_path_reservations
                    SET state='purged', owner_pid=0, owner_start_id='',
                      finished_at=?
                    WHERE storage_key=? AND state='active'
                    """,
                    (recovered_at, storage_key),
                )

    def _ensure_panel_snapshot_support(self) -> None:
        """Keep the materialized panel row dirty when its source data changes."""
        trigger_statements: list[str] = []
        for table in (
            "origins",
            "origin_poll_state",
            "media_items",
            "origin_items",
            "jobs",
            "artifacts",
            "deliveries",
        ):
            for operation in ("INSERT", "UPDATE", "DELETE"):
                trigger_statements.append(
                    f"""
                    CREATE TRIGGER IF NOT EXISTS panel_snapshot_dirty_{table}_{operation.lower()}
                    AFTER {operation} ON {table}
                    BEGIN
                      INSERT INTO panel_snapshots(
                        cache_key, payload_json, dirty, generated_at
                      ) VALUES ('global', '{{}}', 1, '')
                      ON CONFLICT(cache_key) DO UPDATE SET dirty=1;
                    END;
                    """
                )

        # Panel navigation state and the Telegram update offset do not affect
        # displayed metrics. Only the global filter is part of the snapshot.
        for operation, condition in (
            ("INSERT", "NEW.key='source_filter_pattern'"),
            ("UPDATE", "NEW.key='source_filter_pattern' OR OLD.key='source_filter_pattern'"),
            ("DELETE", "OLD.key='source_filter_pattern'"),
        ):
            trigger_statements.append(
                f"""
                CREATE TRIGGER IF NOT EXISTS panel_snapshot_dirty_bot_state_{operation.lower()}
                AFTER {operation} ON bot_state
                WHEN {condition}
                BEGIN
                  INSERT INTO panel_snapshots(
                    cache_key, payload_json, dirty, generated_at
                  ) VALUES ('global', '{{}}', 1, '')
                  ON CONFLICT(cache_key) DO UPDATE SET dirty=1;
                END;
                """
            )
        self.conn.executescript("\n".join(trigger_statements))

    # ------------------------------------------------------------------
    # Provider-neutral origin and discovery API

    def upsert_origin(
        self,
        origin: Origin,
        *,
        managed_by: str = "config",
        created_by: str | None = None,
        max_failures: int = 5,
    ) -> None:
        now = now_iso()
        existing = self.conn.execute(
            "SELECT provider, kind, external_id, managed_by, bootstrap FROM origins WHERE id=?",
            (origin.id,),
        ).fetchone()
        control_retarget = False
        activate_backfill = False
        if existing is not None:
            old_identity = (str(existing["provider"]), str(existing["kind"]), str(existing["external_id"]))
            new_identity = (origin.provider, origin.kind, origin.external_id)
            if old_identity != new_identity:
                old_manager = str(existing["managed_by"])
                control_retarget = old_manager == "control" and managed_by == "control"
                if old_manager != "legacy" and not control_retarget:
                    raise ValueError(
                        f"origin {origin.id!r} source identity is immutable; use a new origin id"
                    )
            activate_backfill = str(existing["bootstrap"]) != "all" and origin.bootstrap == "all"
        if control_retarget:
            # `/sub add` historically updates an existing dynamic subscription.
            # Drop source-bound associations and polling watermarks so the new
            # channel cannot inherit the old channel's discovery state.
            self.conn.execute("DELETE FROM origin_items WHERE origin_id=?", (origin.id,))
            self.conn.execute("DELETE FROM origin_poll_state WHERE origin_id=?", (origin.id,))
        self.conn.execute(
            """
            INSERT INTO origins(
              id, provider, kind, external_id, name, managed_by, options_json,
              enabled, bootstrap, credential_ref, created_at, created_by, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              provider=excluded.provider,
              kind=excluded.kind,
              external_id=excluded.external_id,
              name=excluded.name,
              managed_by=excluded.managed_by,
              options_json=excluded.options_json,
              enabled=excluded.enabled,
              bootstrap=excluded.bootstrap,
              credential_ref=excluded.credential_ref,
              updated_at=excluded.updated_at
            """,
            (
                origin.id,
                origin.provider,
                origin.kind,
                origin.external_id,
                origin.name,
                managed_by,
                json.dumps(origin.options, ensure_ascii=False, sort_keys=True),
                int(origin.enabled),
                origin.bootstrap,
                origin.credential_ref,
                now,
                created_by,
                now,
            ),
        )
        if activate_backfill:
            media_ids = [
                int(row["media_id"])
                for row in self.conn.execute(
                    """
                    SELECT media_id FROM origin_items
                    WHERE origin_id=? AND decision_code='initial_seed'
                    """,
                    (origin.id,),
                ).fetchall()
            ]
            self.conn.execute(
                """
                UPDATE origin_items SET disposition='eligible',
                  decision_code='bootstrap_all',
                  decision_reason='origin bootstrap changed to all',
                  last_seen_at=?
                WHERE origin_id=? AND decision_code='initial_seed'
                """,
                (now, origin.id),
            )
            self.conn.execute("DELETE FROM origin_poll_state WHERE origin_id=?", (origin.id,))
            for media_id in media_ids:
                self._ensure_job(media_id, "download", "", max_failures=max_failures)
        self.conn.commit()

    def list_origins(self, *, managed_by: str | None = None) -> list[Origin]:
        sql = "SELECT * FROM origins"
        params: tuple[object, ...] = ()
        if managed_by is not None:
            sql += " WHERE managed_by = ?"
            params = (managed_by,)
        sql += " ORDER BY id"
        return [self._origin_from_row(row) for row in self.conn.execute(sql, params).fetchall()]

    def list_origin_statuses(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT o.*, COUNT(oi.media_id) AS item_count,
                  ps.last_success_at, ps.last_error_code, ps.last_error,
                  ps.next_poll_at
                FROM origins o
                LEFT JOIN origin_items oi ON oi.origin_id=o.id
                LEFT JOIN origin_poll_state ps ON ps.origin_id=o.id
                GROUP BY o.id
                ORDER BY o.provider, o.name, o.id
                """
            )
        )

    def upsert_control_origin(
        self,
        origin: Origin,
        *,
        created_by: str | None,
        max_failures: int = 5,
    ) -> bool:
        existing = self.conn.execute(
            "SELECT managed_by FROM origins WHERE id=?",
            (origin.id,),
        ).fetchone()
        if existing is not None and str(existing["managed_by"]) != "control":
            raise ValueError(f"origin {origin.id!r} is managed by config and cannot be changed from Telegram")
        self.upsert_origin(
            origin,
            managed_by="control",
            created_by=created_by,
            max_failures=max_failures,
        )
        return existing is None

    def set_control_origin_enabled(self, origin_id: str, enabled: bool) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE origins SET enabled=?, updated_at=?
            WHERE id=? AND managed_by='control'
            """,
            (int(enabled), now_iso(), origin_id),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def set_control_twitch_recording_mode(
        self,
        origin_id: str,
        recording_mode: str,
    ) -> bool:
        mode = recording_mode.lower().strip()
        if mode not in {"vod", "live"}:
            raise ValueError("recording_mode must be 'vod' or 'live'")
        row = self.conn.execute(
            """
            SELECT options_json
            FROM origins
            WHERE id=? AND managed_by='control'
              AND provider='twitch' AND kind='vods'
            """,
            (origin_id,),
        ).fetchone()
        if row is None:
            return False
        try:
            options = json.loads(str(row["options_json"] or "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("origin options_json is invalid") from exc
        if not isinstance(options, dict):
            raise ValueError("origin options_json must be an object")
        options["recording_mode"] = mode
        now = now_iso()
        self.conn.execute(
            """
            UPDATE origins SET options_json=?, updated_at=?
            WHERE id=? AND managed_by='control'
              AND provider='twitch' AND kind='vods'
            """,
            (
                json.dumps(options, ensure_ascii=False, sort_keys=True),
                now,
                origin_id,
            ),
        )
        # Make the new mode eligible for polling as soon as its worker wakes.
        # The service's mode reconciliation also updates its durable marker.
        self.conn.execute(
            "DELETE FROM origin_poll_state WHERE origin_id=?",
            (origin_id,),
        )
        self.conn.commit()
        return True

    def delete_control_origin(self, origin_id: str) -> bool:
        cursor = self.conn.execute(
            "DELETE FROM origins WHERE id=? AND managed_by='control'",
            (origin_id,),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def counts_by_provider(self) -> dict[str, int]:
        return {
            str(row["provider"]): int(row["count"])
            for row in self.conn.execute(
                "SELECT provider, COUNT(*) AS count FROM media_items GROUP BY provider ORDER BY provider"
            )
        }

    def job_counts(self) -> dict[str, int]:
        return {
            f"{row['job_type']}:{row['state']}": int(row["count"])
            for row in self.conn.execute(
                """
                SELECT job_type, state, COUNT(*) AS count
                FROM jobs GROUP BY job_type, state ORDER BY job_type, state
                """
            )
        }

    def list_disk_resources(
        self,
        download_root: Path,
        *,
        limit: int = 6,
        offset: int = 0,
        query: str = "",
    ) -> dict[str, object]:
        """Return one page of tracked disk resources for the control panel.

        The database remains the resource index. Filesystem inspection is
        limited to the selected page so opening the Telegram panel never turns
        into a recursive scan of the whole download tree.
        """

        page_limit = max(1, min(50, int(limit)))
        page_offset = max(0, int(offset))
        search = query.strip()
        where = ""
        params: list[object] = []
        if search:
            terms = search.split()
            where = "WHERE " + " AND ".join(
                "lower(search_text) LIKE ? ESCAPE '\\'"
                for _ in terms
            )
            params.extend(_like_pattern(term) for term in terms)

        summary = self.conn.execute(
            f"""
            {DISK_RESOURCE_CTE}
            SELECT COUNT(*) AS count, COALESCE(SUM(recorded_bytes), 0) AS bytes
            FROM resources
            {where}
            """,
            params,
        ).fetchone()
        rows = self.conn.execute(
            f"""
            {DISK_RESOURCE_CTE}
            SELECT *
            FROM resources
            {where}
            ORDER BY
              COALESCE(published_at, artifact_created_at) DESC,
              artifact_id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, page_limit, page_offset),
        ).fetchall()
        return {
            "items": [
                _disk_resource_from_row(row, download_root)
                for row in rows
            ],
            "total": int(summary["count"]),
            "recorded_bytes": int(summary["bytes"]),
        }

    def get_disk_resource(
        self,
        artifact_id: int,
        download_root: Path,
    ) -> dict[str, object] | None:
        row = self.conn.execute(
            f"""
            {DISK_RESOURCE_CTE}
            SELECT * FROM resources WHERE artifact_id=?
            """,
            (int(artifact_id),),
        ).fetchone()
        if row is None:
            return None

        resource = _disk_resource_from_row(row, download_root)
        file_rows = self.conn.execute(
            """
            SELECT id, role, part_no, path, size_bytes, state, metadata_json,
              created_at, updated_at
            FROM artifacts
            WHERE media_id=? AND state!='purged'
            ORDER BY
              CASE role
                WHEN 'master' THEN 0
                WHEN 'telegram_upload' THEN 1
                WHEN 'thumbnail' THEN 2
                WHEN 'live_segment' THEN 3
                ELSE 4
              END,
              part_no,
              id
            """,
            (int(row["media_id"]),),
        ).fetchall()
        files: list[dict[str, object]] = []
        inspected_by_id: dict[int, dict[str, object]] = {}
        seen_paths: set[str] = set()
        actual_bytes = 0
        existing_file_count = 0
        missing_file_count = 0
        unsafe_file_count = 0
        for file_row in file_rows:
            inspected = _inspect_storage_path(
                Path(str(file_row["path"])),
                download_root,
            )
            inspected_by_id[int(file_row["id"])] = inspected
            normalized = str(inspected["normalized_path"])
            duplicate = normalized in seen_paths
            seen_paths.add(normalized)
            item: dict[str, object] = {
                "artifact_id": int(file_row["id"]),
                "role": str(file_row["role"]),
                "part_no": int(file_row["part_no"]),
                "path": str(file_row["path"]),
                "recorded_bytes": int(file_row["size_bytes"]),
                "state": str(file_row["state"]),
                "created_at": str(file_row["created_at"]),
                "updated_at": str(file_row["updated_at"]),
                "duplicate_path": duplicate,
                **inspected,
            }
            files.append(item)
            if duplicate:
                continue
            if not bool(inspected["safe"]):
                unsafe_file_count += 1
            elif bool(inspected["exists"]):
                existing_file_count += 1
                actual_bytes += int(inspected["actual_bytes"])
            else:
                missing_file_count += 1

        resource.update(
            {
                "files": files,
                "actual_bytes": actual_bytes,
                "existing_file_count": existing_file_count,
                "missing_file_count": missing_file_count,
                "unsafe_file_count": unsafe_file_count,
                "resource_revision": _artifact_set_revision(
                    file_rows,
                    inspected_by_id,
                ),
            }
        )
        return resource

    def purge_disk_resource(
        self,
        artifact_id: int,
        download_root: Path,
        *,
        expected_revision: str,
    ) -> dict[str, object]:
        """Purge exact tracked files while retaining media and delivery history.

        A short ``purging`` tombstone phase keeps workers from treating an
        intentional deletion as an accidental missing artifact. Related
        queued/retry/blocked jobs are cancelled before unlinking starts.
        """

        target_id = int(artifact_id)
        root = Path(download_root).expanduser()
        operation_id = uuid.uuid4().hex
        requested_at = now_iso()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            anchor = self.conn.execute(
                f"""
                {DISK_RESOURCE_CTE}
                SELECT a.*, resources.title AS media_title
                FROM resources
                JOIN artifacts a ON a.id=resources.artifact_id
                WHERE resources.artifact_id=?
                """,
                (target_id,),
            ).fetchone()
            if anchor is None:
                raise ValueError("resource no longer exists in the local library")

            media_id = int(anchor["media_id"])
            running = self.conn.execute(
                """
                SELECT job_type
                FROM jobs
                WHERE media_id=? AND state='running'
                ORDER BY id
                LIMIT 1
                """,
                (media_id,),
            ).fetchone()
            if running is not None:
                raise ValueError(
                    "resource is currently being downloaded or delivered; try again later"
                )

            artifact_rows = self.conn.execute(
                """
                SELECT id, role, part_no, path, size_bytes, state,
                  metadata_json, updated_at
                FROM artifacts
                WHERE media_id=? AND state!='purged'
                ORDER BY CASE role WHEN 'master' THEN 1 ELSE 0 END, id
                """,
                (media_id,),
            ).fetchall()
            inspected_by_id: dict[int, dict[str, object]] = {}
            normalized_paths: set[str] = set()
            for artifact in artifact_rows:
                inspected = _inspect_storage_path(
                    Path(str(artifact["path"])),
                    root,
                )
                if not bool(inspected["safe"]):
                    raise ValueError(
                        "refusing to delete an unsafe tracked path: "
                        f"{inspected['unsafe_reason']}"
                    )
                artifact_row_id = int(artifact["id"])
                inspected_by_id[artifact_row_id] = inspected
                normalized_paths.add(str(inspected["normalized_path"]))
            if (
                _artifact_set_revision(artifact_rows, inspected_by_id)
                != str(expected_revision)
            ):
                raise ValueError(
                    "resource changed after confirmation; review it again"
                )

            if self._find_shared_resource_path(
                media_id,
                normalized_paths,
                root,
            ) is not None:
                raise ValueError(
                    "refusing to delete a file referenced by another media item"
                )

            for storage_key in sorted(normalized_paths):
                try:
                    self.conn.execute(
                        """
                        INSERT INTO purge_path_reservations(
                          storage_key, media_id, operation_id, owner_pid,
                          owner_start_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            storage_key,
                            media_id,
                            operation_id,
                            os.getpid(),
                            _process_start_id(os.getpid()),
                            requested_at,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    existing_reservation = self.conn.execute(
                        """
                        SELECT media_id, state
                        FROM purge_path_reservations
                        WHERE storage_key=?
                        """,
                        (storage_key,),
                    ).fetchone()
                    storage_is_missing = not any(
                        bool(inspection["exists"])
                        for inspection in inspected_by_id.values()
                        if str(inspection["normalized_path"])
                        == storage_key
                    )
                    storage_is_failed_retry = any(
                        str(artifact["state"]) == "purge_failed"
                        and str(
                            inspected_by_id[int(artifact["id"])][
                                "normalized_path"
                            ]
                        )
                        == storage_key
                        for artifact in artifact_rows
                    )
                    if (
                        existing_reservation is not None
                        and int(existing_reservation["media_id"]) == media_id
                        and str(existing_reservation["state"]) == "purged"
                        and (
                            storage_is_missing
                            or storage_is_failed_retry
                        )
                    ):
                        cursor = self.conn.execute(
                            """
                            UPDATE purge_path_reservations
                            SET operation_id=?, owner_pid=?,
                              owner_start_id=?, state='active',
                              created_at=?, finished_at=NULL
                            WHERE storage_key=? AND media_id=?
                              AND state='purged'
                            """,
                            (
                                operation_id,
                                os.getpid(),
                                _process_start_id(os.getpid()),
                                requested_at,
                                storage_key,
                                media_id,
                            ),
                        )
                        if cursor.rowcount == 1:
                            continue
                    raise ValueError(
                        "one of the tracked files is already reserved for deletion"
                    ) from exc

            for artifact in artifact_rows:
                metadata = _json_object(artifact["metadata_json"])
                metadata["local_purge"] = {
                    "operation_id": operation_id,
                    "requested_at": requested_at,
                    "previous_state": str(artifact["state"]),
                    "source": "telegram_panel",
                }
                self.conn.execute(
                    """
                    UPDATE artifacts
                    SET state='purging', metadata_json=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                        requested_at,
                        int(artifact["id"]),
                    ),
                )
            self.conn.execute(
                """
                UPDATE jobs
                SET state='cancelled',
                  reason_code='resource_purged',
                  last_error='local archive was deleted from the Telegram panel',
                  lease_owner=NULL,
                  lease_token=NULL,
                  lease_until=NULL,
                  available_at=?,
                  finished_at=?,
                  updated_at=?
                WHERE media_id=? AND state IN ('queued', 'retry', 'blocked')
                """,
                (requested_at, requested_at, requested_at, media_id),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        return self._purge_reserved_files(
            media_id=media_id,
            target_id=target_id,
            title=str(anchor["media_title"]),
            artifact_rows=artifact_rows,
            inspected_by_id=inspected_by_id,
            root=root,
            operation_id=operation_id,
            requested_at=requested_at,
        )

    def _find_shared_resource_path(
        self,
        media_id: int,
        normalized_paths: set[str],
        root: Path,
    ) -> str | None:
        other_paths = self.conn.execute(
            """
            SELECT path
            FROM artifacts
            WHERE media_id!=? AND state!='purged'
            """,
            (media_id,),
        ).fetchall()
        for other in other_paths:
            inspected = _inspect_storage_path(
                Path(str(other["path"])),
                root,
            )
            normalized = str(inspected["normalized_path"])
            if normalized in normalized_paths:
                return normalized
        return None

    def _purge_reserved_files(
        self,
        *,
        media_id: int,
        target_id: int,
        title: str,
        artifact_rows: list[sqlite3.Row],
        inspected_by_id: dict[int, dict[str, object]],
        root: Path,
        operation_id: str,
        requested_at: str,
    ) -> dict[str, object]:
        path_groups: dict[str, dict[str, object]] = {}
        for artifact in artifact_rows:
            artifact_row_id = int(artifact["id"])
            inspected = inspected_by_id[artifact_row_id]
            normalized = str(inspected["normalized_path"])
            group = path_groups.setdefault(
                normalized,
                {
                    "path": Path(str(artifact["path"])),
                    "artifact_ids": [],
                    "roles": [],
                    "inspection": inspected,
                },
            )
            group["artifact_ids"].append(artifact_row_id)  # type: ignore[union-attr]
            group["roles"].append(str(artifact["role"]))  # type: ignore[union-attr]

        ordered_groups = sorted(
            path_groups.values(),
            key=lambda group: int(
                target_id
                in [int(value) for value in group["artifact_ids"]]
            ),
        )
        result_by_id: dict[int, dict[str, object]] = {}
        deleted_files = 0
        missing_files = 0
        freed_bytes = 0
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            current_rows = self.conn.execute(
                """
                SELECT id, role, part_no, path, size_bytes, state,
                  metadata_json, updated_at
                FROM artifacts
                WHERE media_id=? AND state!='purged'
                ORDER BY CASE role WHEN 'master' THEN 1 ELSE 0 END, id
                """,
                (media_id,),
            ).fetchall()
            expected_shape = [
                (
                    int(row["id"]),
                    str(row["role"]),
                    int(row["part_no"]),
                    str(row["path"]),
                    int(row["size_bytes"]),
                )
                for row in artifact_rows
            ]
            current_shape = [
                (
                    int(row["id"]),
                    str(row["role"]),
                    int(row["part_no"]),
                    str(row["path"]),
                    int(row["size_bytes"]),
                )
                for row in current_rows
            ]
            validation_error: str | None = None
            if current_shape != expected_shape or any(
                str(row["state"]) != "purging" for row in current_rows
            ):
                validation_error = (
                    "resource changed after deletion was reserved"
                )

            reserved_paths = {
                str(row["storage_key"])
                for row in self.conn.execute(
                    """
                    SELECT storage_key
                    FROM purge_path_reservations
                    WHERE operation_id=? AND media_id=? AND state='active'
                    """,
                    (operation_id, media_id),
                )
            }
            expected_paths = set(path_groups)
            if validation_error is None and reserved_paths != expected_paths:
                validation_error = "local deletion reservation was lost"

            running = self.conn.execute(
                """
                SELECT 1
                FROM jobs
                WHERE media_id=? AND state='running'
                LIMIT 1
                """,
                (media_id,),
            ).fetchone()
            if validation_error is None and running is not None:
                validation_error = (
                    "resource became active after deletion was reserved"
                )

            if validation_error is None:
                for artifact in current_rows:
                    artifact_row_id = int(artifact["id"])
                    current = _inspect_storage_path(
                        Path(str(artifact["path"])),
                        root,
                    )
                    initial = inspected_by_id[artifact_row_id]
                    if not bool(current["safe"]):
                        validation_error = (
                            "refusing to delete an unsafe tracked path: "
                            f"{current['unsafe_reason']}"
                        )
                        break
                    if not _storage_identity_matches(current, initial):
                        validation_error = (
                            "tracked file changed after deletion was reserved"
                        )
                        break

            if (
                validation_error is None
                and self._find_shared_resource_path(
                    media_id,
                    expected_paths,
                    root,
                )
                is not None
            ):
                validation_error = (
                    "tracked file became referenced by another media item"
                )

            if validation_error is not None:
                for group in ordered_groups:
                    result = {
                        "status": "failed",
                        "error": validation_error,
                        "bytes": 0,
                    }
                    for artifact_row_id in group["artifact_ids"]:
                        result_by_id[int(artifact_row_id)] = result
            else:
                self.conn.execute(
                    """
                    UPDATE jobs
                    SET state='cancelled',
                      reason_code='resource_purged',
                      last_error=(
                        'local archive was deleted from the Telegram panel'
                      ),
                      lease_owner=NULL,
                      lease_token=NULL,
                      lease_until=NULL,
                      available_at=?,
                      finished_at=?,
                      updated_at=?
                    WHERE media_id=?
                      AND state IN ('queued', 'retry', 'blocked')
                    """,
                    (
                        requested_at,
                        requested_at,
                        requested_at,
                        media_id,
                    ),
                )
                failure_seen = False
                for group in ordered_groups:
                    artifact_ids = [
                        int(value) for value in group["artifact_ids"]
                    ]
                    initial = group["inspection"]
                    if failure_seen:
                        result = {
                            "status": "failed",
                            "error": (
                                "not attempted after another tracked file "
                                "failed"
                            ),
                            "bytes": 0,
                        }
                    elif not bool(initial["exists"]):
                        result = {
                            "status": "missing",
                            "error": None,
                            "bytes": 0,
                        }
                        missing_files += 1
                    else:
                        try:
                            size = _unlink_tracked_file(
                                Path(str(initial["normalized_path"])),
                                root,
                                initial,
                            )
                        except FileNotFoundError:
                            result = {
                                "status": "missing",
                                "error": None,
                                "bytes": 0,
                            }
                            missing_files += 1
                        except (OSError, ValueError) as exc:
                            result = {
                                "status": "failed",
                                "error": str(exc),
                                "bytes": 0,
                            }
                            failure_seen = True
                        else:
                            result = {
                                "status": "deleted",
                                "error": None,
                                "bytes": size,
                            }
                            deleted_files += 1
                            freed_bytes += size
                    for artifact_row_id in artifact_ids:
                        result_by_id[artifact_row_id] = result

            finished_at = now_iso()
            failed_paths: set[str] = set()
            failure_messages: list[str] = []
            for artifact in artifact_rows:
                artifact_row_id = int(artifact["id"])
                outcome = result_by_id[artifact_row_id]
                metadata = _json_object(artifact["metadata_json"])
                purge_metadata: dict[str, object] = {
                    "requested_at": requested_at,
                    "finished_at": finished_at,
                    "previous_state": str(artifact["state"]),
                    "source": "telegram_panel",
                    "result": str(outcome["status"]),
                }
                if outcome["error"]:
                    purge_metadata["error"] = str(outcome["error"])[:500]
                metadata["local_purge"] = purge_metadata
                state = (
                    "purged"
                    if outcome["status"] in {"deleted", "missing"}
                    else "purge_failed"
                )
                if state == "purge_failed":
                    failed_paths.add(
                        str(
                            inspected_by_id[artifact_row_id][
                                "normalized_path"
                            ]
                        )
                    )
                    failure_messages.append(str(outcome["error"]))
                self.conn.execute(
                    """
                    UPDATE artifacts
                    SET state=?, metadata_json=?, updated_at=?
                    WHERE id=? AND state='purging'
                    """,
                    (
                        state,
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                        finished_at,
                        artifact_row_id,
                    ),
                )
            for normalized, group in path_groups.items():
                first_artifact_id = int(group["artifact_ids"][0])
                outcome = result_by_id[first_artifact_id]
                if (
                    outcome["status"] in {"deleted", "missing"}
                    or not _path_entry_exists(Path(normalized))
                ):
                    self.conn.execute(
                        """
                        UPDATE purge_path_reservations
                        SET state='purged', owner_pid=0, owner_start_id='',
                          finished_at=?
                        WHERE storage_key=? AND operation_id=?
                          AND state='active'
                        """,
                        (finished_at, normalized, operation_id),
                    )
                else:
                    self.conn.execute(
                        """
                        DELETE FROM purge_path_reservations
                        WHERE storage_key=? AND operation_id=?
                          AND state='active'
                        """,
                        (normalized, operation_id),
                    )
            self.conn.commit()
        except Exception:
            if self.conn.in_transaction:
                self.conn.rollback()
            try:
                self._abort_reserved_disk_purge(
                    operation_id,
                    artifact_rows,
                )
            except Exception:
                # Startup recovery keeps the durable reservation retryable.
                pass
            raise

        anchor_row = self.conn.execute(
            "SELECT updated_at FROM artifacts WHERE id=?",
            (target_id,),
        ).fetchone()
        remaining_rows = self.conn.execute(
            """
            SELECT id, role, part_no, path, size_bytes, state,
              metadata_json, updated_at
            FROM artifacts
            WHERE media_id=? AND state!='purged'
            ORDER BY CASE role WHEN 'master' THEN 1 ELSE 0 END, id
            """,
            (media_id,),
        ).fetchall()
        remaining_inspections = {
            int(row["id"]): _inspect_storage_path(
                Path(str(row["path"])),
                root,
            )
            for row in remaining_rows
        }
        return {
            "artifact_id": target_id,
            "media_id": media_id,
            "title": title,
            "completed": not failed_paths,
            "deleted_files": deleted_files,
            "missing_files": missing_files,
            "failed_files": len(failed_paths),
            "freed_bytes": freed_bytes,
            "errors": list(dict.fromkeys(failure_messages)),
            "updated_at": (
                str(anchor_row["updated_at"])
                if anchor_row is not None
                else finished_at
            ),
            "resource_revision": _artifact_set_revision(
                remaining_rows,
                remaining_inspections,
            ),
        }

    def _abort_reserved_disk_purge(
        self,
        operation_id: str,
        artifact_rows: list[sqlite3.Row],
    ) -> None:
        aborted_at = now_iso()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for artifact in artifact_rows:
                metadata = _json_object(artifact["metadata_json"])
                purge_metadata = _json_object(metadata.get("local_purge"))
                purge_metadata.update(
                    {
                        "finished_at": aborted_at,
                        "result": "failed",
                        "error": "local deletion stopped unexpectedly",
                    }
                )
                metadata["local_purge"] = purge_metadata
                self.conn.execute(
                    """
                    UPDATE artifacts
                    SET state='purge_failed', metadata_json=?, updated_at=?
                    WHERE id=? AND state='purging'
                    """,
                    (
                        json.dumps(
                            metadata,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        aborted_at,
                        int(artifact["id"]),
                    ),
                )
            reservation_rows = self.conn.execute(
                """
                SELECT storage_key
                FROM purge_path_reservations
                WHERE operation_id=? AND state='active'
                """,
                (operation_id,),
            ).fetchall()
            for reservation in reservation_rows:
                storage_key = str(reservation["storage_key"])
                if _path_entry_exists(Path(storage_key)):
                    self.conn.execute(
                        """
                        DELETE FROM purge_path_reservations
                        WHERE storage_key=? AND operation_id=?
                          AND state='active'
                        """,
                        (storage_key, operation_id),
                    )
                else:
                    self.conn.execute(
                        """
                        UPDATE purge_path_reservations
                        SET state='purged', owner_pid=0, owner_start_id='',
                          finished_at=?
                        WHERE storage_key=? AND operation_id=?
                          AND state='active'
                        """,
                        (aborted_at, storage_key, operation_id),
                    )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def get_panel_snapshot(
        self,
        source_filter_pattern: str | None,
        *,
        max_age_seconds: int = 30,
        force: bool = False,
    ) -> dict[str, object]:
        row = self.conn.execute(
            "SELECT payload_json, dirty, generated_at FROM panel_snapshots WHERE cache_key='global'"
        ).fetchone()
        if row is not None and not force and not bool(row["dirty"]):
            try:
                generated_at = datetime.fromisoformat(str(row["generated_at"]))
                if generated_at.tzinfo is None:
                    generated_at = generated_at.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - generated_at.astimezone(timezone.utc)).total_seconds()
                payload = json.loads(str(row["payload_json"]))
                if (
                    age <= max_age_seconds
                    and isinstance(payload, dict)
                    and payload.get("version") == 2
                    and payload.get("source_filter_pattern") == source_filter_pattern
                ):
                    return payload
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        return self._rebuild_panel_snapshot(source_filter_pattern)

    def _rebuild_panel_snapshot(self, source_filter_pattern: str | None) -> dict[str, object]:
        generated_at = now_iso()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            origins = [
                {
                    "id": str(row["id"]),
                    "provider": str(row["provider"]),
                    "kind": str(row["kind"]),
                    "external_id": str(row["external_id"]),
                    "name": str(row["name"]),
                    "managed_by": str(row["managed_by"]),
                    "enabled": bool(row["enabled"]),
                    "item_count": int(row["item_count"]),
                    "last_success_at": str(row["last_success_at"]) if row["last_success_at"] else None,
                    "last_error_code": str(row["last_error_code"]) if row["last_error_code"] else None,
                    "next_poll_at": str(row["next_poll_at"]) if row["next_poll_at"] else None,
                    "recording_mode": _origin_recording_mode_override(
                        row["options_json"]
                    ),
                }
                for row in self.list_origin_statuses()
            ]
            payload: dict[str, object] = {
                "version": 2,
                "generated_at": generated_at,
                "source_filter_pattern": source_filter_pattern,
                "origins": origins,
                "summary": self.backup_summary(),
                "providers": self.counts_by_provider(),
                "jobs": self.job_counts(),
            }
            self.conn.execute(
                """
                INSERT INTO panel_snapshots(cache_key, payload_json, dirty, generated_at)
                VALUES ('global', ?, 0, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                  payload_json=excluded.payload_json,
                  dirty=0,
                  generated_at=excluded.generated_at
                """,
                (json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True), generated_at),
            )
            self.conn.commit()
            return payload
        except Exception:
            self.conn.rollback()
            raise

    def disable_missing_config_origins(self, configured_ids: set[str]) -> int:
        if configured_ids:
            placeholders = ",".join("?" for _ in configured_ids)
            cursor = self.conn.execute(
                f"UPDATE origins SET enabled=0, updated_at=? WHERE managed_by='config' AND id NOT IN ({placeholders})",
                (now_iso(), *sorted(configured_ids)),
            )
        else:
            cursor = self.conn.execute(
                "UPDATE origins SET enabled=0, updated_at=? WHERE managed_by='config' AND enabled!=0",
                (now_iso(),),
            )
        self.conn.commit()
        return cursor.rowcount

    def delete_origin(self, origin_id: str) -> bool:
        cursor = self.conn.execute("DELETE FROM origins WHERE id = ?", (origin_id,))
        self.conn.commit()
        return cursor.rowcount > 0

    def origin_has_items(
        self,
        origin_id: str,
        *,
        content_kind: str | None = None,
    ) -> bool:
        if content_kind is None:
            row = self.conn.execute(
                "SELECT 1 FROM origin_items WHERE origin_id = ? LIMIT 1",
                (origin_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                """
                SELECT 1
                FROM origin_items oi
                JOIN media_items mi ON mi.id=oi.media_id
                WHERE oi.origin_id=? AND mi.content_kind=?
                LIMIT 1
                """,
                (origin_id, content_kind),
            ).fetchone()
        return row is not None

    def upsert_discovered(
        self,
        origin_id: str,
        candidate: MediaCandidate,
        *,
        disposition: str = "eligible",
        decision_code: str | None = None,
        decision_reason: str | None = None,
        max_failures: int = 5,
        job_payload: dict[str, object] | None = None,
    ) -> tuple[int, bool]:
        now = now_iso()
        existing = self.conn.execute(
            "SELECT id FROM media_items WHERE provider = ? AND content_kind = ? AND external_id = ?",
            (candidate.provider, candidate.content_kind, candidate.external_id),
        ).fetchone()
        created = existing is None
        self.conn.execute(
            """
            INSERT INTO media_items(
              provider, content_kind, external_id, title, canonical_url,
              published_at, source_updated_at, live_status, visibility,
              metadata_json, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, content_kind, external_id) DO UPDATE SET
              title=excluded.title,
              canonical_url=excluded.canonical_url,
              published_at=COALESCE(excluded.published_at, media_items.published_at),
              source_updated_at=COALESCE(excluded.source_updated_at, media_items.source_updated_at),
              live_status=excluded.live_status,
              visibility=excluded.visibility,
              metadata_json=excluded.metadata_json,
              last_seen_at=excluded.last_seen_at
            """,
            (
                candidate.provider,
                candidate.content_kind,
                candidate.external_id,
                candidate.title,
                candidate.url,
                candidate.published_at,
                candidate.updated_at,
                candidate.live_status,
                candidate.visibility,
                json.dumps(candidate.metadata, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        media_id = int(
            self.conn.execute(
                "SELECT id FROM media_items WHERE provider = ? AND content_kind = ? AND external_id = ?",
                (candidate.provider, candidate.content_kind, candidate.external_id),
            ).fetchone()[0]
        )
        self.conn.execute(
            """
            INSERT INTO origin_items(
              origin_id, media_id, disposition, decision_code, decision_reason,
              first_seen_at, last_seen_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}')
            ON CONFLICT(origin_id, media_id) DO UPDATE SET
              disposition=CASE
                WHEN origin_items.decision_code='legacy_ignored'
                  OR (origin_items.decision_code='initial_seed'
                      AND COALESCE(excluded.decision_code, '')!='bootstrap_all')
                THEN origin_items.disposition ELSE excluded.disposition END,
              decision_code=CASE
                WHEN origin_items.decision_code='legacy_ignored'
                  OR (origin_items.decision_code='initial_seed'
                      AND COALESCE(excluded.decision_code, '')!='bootstrap_all')
                THEN origin_items.decision_code ELSE excluded.decision_code END,
              decision_reason=CASE
                WHEN origin_items.decision_code='legacy_ignored'
                  OR (origin_items.decision_code='initial_seed'
                      AND COALESCE(excluded.decision_code, '')!='bootstrap_all')
                THEN origin_items.decision_reason ELSE excluded.decision_reason END,
              last_seen_at=excluded.last_seen_at
            """,
            (origin_id, media_id, disposition, decision_code, decision_reason, now, now),
        )
        effective = self.conn.execute(
            "SELECT disposition FROM origin_items WHERE origin_id=? AND media_id=?",
            (origin_id, media_id),
        ).fetchone()
        if effective is not None and effective["disposition"] == "eligible":
            job_id = self._ensure_job(
                media_id,
                "download",
                "",
                max_failures=max_failures,
                payload=job_payload,
            )
            retriable_reasons = ["source_filter"]
            candidate_stream_id = str(candidate.metadata.get("stream_id") or "")
            if (
                candidate.provider == "twitch"
                and candidate.content_kind == "vod"
                and candidate_stream_id
                and not self.has_ready_twitch_live_recording(candidate_stream_id)
            ):
                retriable_reasons.append("live_recording_exists")
            if (
                job_payload
                and job_payload.get("download_lane") == "live"
                and job_payload.get("recording_mode") == "live"
            ):
                retriable_reasons.append("live_origin_disabled")
            placeholders = ",".join("?" for _ in retriable_reasons)
            self.conn.execute(
                f"""
                UPDATE jobs SET state='queued', failure_count=0, available_at=?,
                  reason_code=NULL, last_error=NULL, finished_at=NULL, updated_at=?
                WHERE id=? AND state='cancelled'
                  AND reason_code IN ({placeholders})
                """,
                (now, now, job_id, *retriable_reasons),
            )
        self.conn.commit()
        return media_id, created

    def record_origin_poll_success(
        self,
        origin_id: str,
        *,
        cursor: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        now = now_iso()
        self.conn.execute(
            """
            INSERT INTO origin_poll_state(
              origin_id, cursor, etag, last_modified, last_polled_at,
              last_success_at, last_error_code, last_error, next_poll_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
            ON CONFLICT(origin_id) DO UPDATE SET
              cursor=excluded.cursor,
              etag=COALESCE(excluded.etag, origin_poll_state.etag),
              last_modified=COALESCE(excluded.last_modified, origin_poll_state.last_modified),
              last_polled_at=excluded.last_polled_at,
              last_success_at=excluded.last_success_at,
              last_error_code=NULL,
              last_error=NULL,
              next_poll_at=NULL,
              updated_at=excluded.updated_at
            """,
            (origin_id, cursor, etag, last_modified, now, now, now),
        )
        self.conn.commit()

    def record_origin_poll_failure(
        self,
        origin_id: str,
        *,
        error_code: str,
        error: str,
        retry_seconds: int,
    ) -> None:
        now = now_iso()
        self.conn.execute(
            """
            INSERT INTO origin_poll_state(
              origin_id, last_polled_at, last_error_code, last_error, next_poll_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(origin_id) DO UPDATE SET
              last_polled_at=excluded.last_polled_at,
              last_error_code=excluded.last_error_code,
              last_error=excluded.last_error,
              next_poll_at=excluded.next_poll_at,
              updated_at=excluded.updated_at
            """,
            (origin_id, now, error_code, error, future_iso(retry_seconds), now),
        )
        self.conn.commit()

    def origin_poll_due(self, origin_id: str) -> bool:
        row = self.conn.execute("SELECT next_poll_at FROM origin_poll_state WHERE origin_id = ?", (origin_id,)).fetchone()
        return row is None or row["next_poll_at"] is None or str(row["next_poll_at"]) <= now_iso()

    def reconcile_origin_poll_mode(self, origin_id: str, recording_mode: str) -> bool:
        if recording_mode not in {"vod", "live"}:
            raise ValueError("recording_mode must be 'vod' or 'live'")
        key = f"_origin_poll_mode:{origin_id}"
        row = self.conn.execute("SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
        previous_mode = str(row["value"]) if row is not None else None
        reset_poll_state = (
            previous_mode is not None and previous_mode != recording_mode
        ) or (
            previous_mode is None and recording_mode == "live"
        )
        self.conn.execute(
            """
            INSERT INTO bot_state(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, recording_mode),
        )
        if reset_poll_state:
            self.conn.execute("DELETE FROM origin_poll_state WHERE origin_id=?", (origin_id,))
        self.conn.commit()
        return reset_poll_state

    def get_origin_checkpoint(self, origin_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT cursor FROM origin_poll_state WHERE origin_id = ?",
            (origin_id,),
        ).fetchone()
        return str(row["cursor"]) if row and row["cursor"] is not None else None

    # ------------------------------------------------------------------
    # Durable job, artifact and delivery API

    def _assert_artifact_path_not_reserved(self, path: Path) -> None:
        storage_key = _storage_key(path)
        row = self.conn.execute(
            """
            SELECT media_id
            FROM purge_path_reservations
            WHERE storage_key=?
            """,
            (storage_key,),
        ).fetchone()
        if row is not None:
            raise RuntimeError(
                "artifact path is reserved for deletion from the local library"
            )

    def ensure_delivery_job(self, media_id: int, destination_key: str, *, max_failures: int = 5) -> int:
        job_id = self._ensure_job(
            media_id,
            "telegram_delivery",
            destination_key,
            max_failures=max_failures,
            payload={"destination_key": destination_key},
        )
        self.conn.commit()
        return job_id

    def ensure_delivery_jobs_for_ready_artifacts(self, destination_key: str, *, max_failures: int = 5) -> int:
        media_rows = self.conn.execute(
            """
            SELECT DISTINCT a.media_id
            FROM artifacts a
            WHERE a.role='master' AND a.state='ready'
              AND NOT EXISTS (
                SELECT 1 FROM deliveries d
                WHERE d.media_id=a.media_id AND d.sink='telegram'
              )
              AND NOT EXISTS (
                SELECT 1 FROM jobs j
                WHERE j.media_id=a.media_id AND j.job_type='telegram_delivery'
                  AND j.state IN ('succeeded', 'uncertain')
              )
            """,
        ).fetchall()
        for row in media_rows:
            self._ensure_job(
                int(row["media_id"]),
                "telegram_delivery",
                destination_key,
                max_failures=max_failures,
                payload={"destination_key": destination_key},
            )
        self.conn.commit()
        return len(media_rows)

    def requeue_download(self, media_id: int, *, max_failures: int = 5, reason: str = "artifact missing") -> None:
        self._ensure_job(media_id, "download", "", max_failures=max_failures)
        self.conn.execute(
            """
            UPDATE jobs SET state='retry', available_at=?, reason_code='artifact_missing',
              last_error=?, lease_owner=NULL, lease_token=NULL, lease_until=NULL,
              failure_count=CASE WHEN failure_count >= max_failures THEN 0 ELSE failure_count END,
              finished_at=NULL, updated_at=?
            WHERE media_id=? AND job_type='download' AND target_key=''
            """,
            (now_iso(), reason, now_iso(), media_id),
        )
        self.conn.commit()

    def claim_next_job(
        self,
        job_types: tuple[str, ...],
        *,
        owner: str,
        lease_seconds: int,
        download_lane: str | None = None,
    ) -> ClaimedJob | None:
        if not job_types:
            return None
        if download_lane not in {None, "standard", "live"}:
            raise ValueError("download_lane must be 'standard', 'live', or None")
        self.recover_stale_jobs(commit=True)
        now = now_iso()
        placeholders = ",".join("?" for _ in job_types)
        lane_clause = ""
        if download_lane == "live":
            lane_clause = (
                "AND job_type='download' "
                "AND COALESCE(json_extract(payload_json, '$.download_lane'), 'standard')='live'"
            )
        elif download_lane == "standard":
            lane_clause = (
                "AND (job_type!='download' "
                "OR COALESCE(json_extract(payload_json, '$.download_lane'), 'standard')!='live')"
            )
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                f"""
                SELECT * FROM jobs
                WHERE job_type IN ({placeholders})
                  AND state IN ('queued', 'retry')
                  AND failure_count < max_failures
                  AND available_at <= ?
                  {lane_clause}
                ORDER BY CASE job_type WHEN 'telegram_delivery' THEN 0 ELSE 1 END,
                         available_at ASC, id ASC
                LIMIT 1
                """,
                (*job_types, now),
            ).fetchone()
            if row is None:
                self.conn.commit()
                return None
            token = uuid.uuid4().hex
            lease_until = future_iso(lease_seconds)
            payload = json.loads(str(row["payload_json"] or "{}"))
            if str(row["job_type"]) == "telegram_delivery":
                payload["phase"] = "preparing"
            cursor = self.conn.execute(
                """
                UPDATE jobs
                SET state='running', lease_owner=?, lease_token=?, lease_until=?,
                    payload_json=?, started_at=COALESCE(started_at, ?), updated_at=?
                WHERE id=? AND state IN ('queued', 'retry')
                """,
                (owner, token, lease_until, json.dumps(payload, sort_keys=True), now, now, row["id"]),
            )
            if cursor.rowcount != 1:
                self.conn.rollback()
                return None
            self.conn.commit()
            return ClaimedJob(
                id=int(row["id"]),
                media_id=int(row["media_id"]),
                job_type=str(row["job_type"]),
                target_key=str(row["target_key"]),
                attempts=int(row["failure_count"]),
                max_attempts=int(row["max_failures"]),
                reason_code=str(row["reason_code"]) if row["reason_code"] else None,
                payload=payload,
                lease_owner=owner,
                lease_token=token,
            )
        except Exception:
            self.conn.rollback()
            raise

    def renew_lease(self, job: ClaimedJob, lease_seconds: int) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE jobs SET lease_until=?, updated_at=?
            WHERE id=? AND state='running' AND lease_token=?
            """,
            (future_iso(lease_seconds), now_iso(), job.id, job.lease_token),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def defer_job(self, job: ClaimedJob, *, reason_code: str, error: str, retry_seconds: int) -> None:
        self._finish_running_job(
            job,
            state="retry",
            reason_code=reason_code,
            error=error,
            available_at=future_iso(retry_seconds),
            increment_failure=False,
        )

    def fail_job(self, job: ClaimedJob, *, reason_code: str, error: str, retry_seconds: int) -> None:
        row = self.conn.execute(
            "SELECT failure_count, max_failures FROM jobs WHERE id=? AND state='running' AND lease_token=?",
            (job.id, job.lease_token),
        ).fetchone()
        if row is None:
            raise RuntimeError("job lease is no longer owned by this worker")
        state = "blocked" if int(row["failure_count"]) + 1 >= int(row["max_failures"]) else "retry"
        self._finish_running_job(
            job,
            state=state,
            reason_code=reason_code,
            error=error,
            available_at=future_iso(retry_seconds),
            increment_failure=True,
        )

    def block_job(self, job: ClaimedJob, *, reason_code: str, error: str) -> None:
        self._finish_running_job(
            job,
            state="blocked",
            reason_code=reason_code,
            error=error,
            available_at=now_iso(),
            increment_failure=False,
        )

    def mark_job_uncertain(self, job: ClaimedJob, *, error: str) -> None:
        self._finish_running_job(
            job,
            state="uncertain",
            reason_code="delivery_uncertain",
            error=error,
            available_at=now_iso(),
            increment_failure=False,
        )

    def mark_delivery_sending(self, job: ClaimedJob) -> None:
        if job.job_type != "telegram_delivery":
            raise ValueError("only delivery jobs can enter the sending phase")
        cursor = self.conn.execute(
            """
            UPDATE jobs SET payload_json=json_set(payload_json, '$.phase', 'sending'), updated_at=?
            WHERE id=? AND state='running' AND lease_token=?
            """,
            (now_iso(), job.id, job.lease_token),
        )
        if cursor.rowcount != 1:
            self.conn.rollback()
            raise RuntimeError("job lease is no longer owned by this worker")
        self.conn.commit()

    def cancel_job(self, job: ClaimedJob, *, reason_code: str, error: str) -> None:
        self._finish_running_job(
            job,
            state="cancelled",
            reason_code=reason_code,
            error=error,
            available_at=now_iso(),
            increment_failure=False,
        )
        if job.job_type == "download" and reason_code == "source_filter":
            self.conn.execute(
                """
                UPDATE origin_items SET disposition='ignored', decision_code='source_filter',
                  decision_reason=?, last_seen_at=?
                WHERE media_id=? AND disposition='eligible'
                """,
                (error, now_iso(), job.media_id),
            )
            self.conn.commit()

    def complete_download(
        self,
        job: ClaimedJob,
        *,
        path: Path,
        size_bytes: int,
        delivery_targets: tuple[str, ...] = (),
        delivery_max_failures: int | None = None,
        live_retry_seconds: int = 15,
    ) -> int:
        now = now_iso()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._assert_lease(job)
            self._assert_artifact_path_not_reserved(path)
            media = self.conn.execute(
                "SELECT provider, content_kind, metadata_json FROM media_items WHERE id=?",
                (job.media_id,),
            ).fetchone()
            metadata: dict[str, object] = {}
            if media is not None:
                decoded = json.loads(str(media["metadata_json"] or "{}"))
                if isinstance(decoded, dict):
                    metadata = decoded
            twitch_vod_stream_id = ""
            if (
                media is not None
                and str(media["provider"]) == "twitch"
                and str(media["content_kind"]) == "vod"
            ):
                twitch_vod_stream_id = str(metadata.get("stream_id") or "")
            linked_live_state = (
                self.twitch_live_recording_state(twitch_vod_stream_id)
                if twitch_vod_stream_id
                else None
            )
            artifact_state = {
                "ready": "suppressed",
                "pending": "staged",
            }.get(linked_live_state, "ready")
            self.conn.execute(
                """
                INSERT INTO artifacts(media_id, role, part_no, path, size_bytes, state, created_at, updated_at)
                VALUES (?, 'master', 0, ?, ?, ?, ?, ?)
                ON CONFLICT(media_id, role, part_no) DO UPDATE SET
                  path=excluded.path, size_bytes=excluded.size_bytes,
                  state=excluded.state, updated_at=excluded.updated_at
                """,
                (job.media_id, str(path), size_bytes, artifact_state, now, now),
            )
            artifact_id = int(
                self.conn.execute(
                    "SELECT id FROM artifacts WHERE media_id=? AND role='master' AND part_no=0",
                    (job.media_id,),
                ).fetchone()[0]
            )
            if linked_live_state in {"ready", "pending"}:
                state = "cancelled" if linked_live_state == "ready" else "retry"
                reason_code = (
                    "live_recording_exists"
                    if linked_live_state == "ready"
                    else "live_recording_pending"
                )
                error = (
                    "matching Twitch live stream was already archived"
                    if linked_live_state == "ready"
                    else "matching Twitch live recording is still in progress"
                )
                available_at = (
                    now
                    if linked_live_state == "ready"
                    else future_iso(max(1, live_retry_seconds))
                )
                cursor = self.conn.execute(
                    """
                    UPDATE jobs SET state=?, reason_code=?, last_error=?,
                      available_at=?, lease_owner=NULL, lease_token=NULL,
                      lease_until=NULL, finished_at=?, updated_at=?
                    WHERE id=? AND state='running' AND lease_token=?
                    """,
                    (
                        state,
                        reason_code,
                        error,
                        available_at,
                        now if state == "cancelled" else None,
                        now,
                        job.id,
                        job.lease_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("job lease is no longer owned by this worker")
                self.conn.commit()
                return artifact_id
            suppress_current_delivery = False
            if (
                media is not None
                and str(media["provider"]) == "twitch"
                and str(media["content_kind"]) == "live_stream"
            ):
                stream_id = str(metadata.get("stream_id") or "")
                if stream_id:
                    matching_vod_media = """
                        SELECT id FROM media_items
                        WHERE provider='twitch'
                          AND content_kind='vod'
                          AND json_extract(metadata_json, '$.stream_id')=?
                    """
                    vod_delivery_started = bool(
                        self.conn.execute(
                            f"""
                            SELECT
                              EXISTS(
                                SELECT 1 FROM deliveries
                                WHERE media_id IN ({matching_vod_media})
                              )
                              OR EXISTS(
                                SELECT 1 FROM jobs
                                WHERE job_type='telegram_delivery'
                                  AND media_id IN ({matching_vod_media})
                                  AND state IN ('running', 'succeeded', 'uncertain')
                              )
                            """,
                            (stream_id, stream_id),
                        ).fetchone()[0]
                    )
                    if vod_delivery_started:
                        suppress_current_delivery = True
                        self.conn.execute(
                            """
                            UPDATE artifacts SET state='suppressed', updated_at=?
                            WHERE id=?
                            """,
                            (now, artifact_id),
                        )
                    else:
                        self.conn.execute(
                            f"""
                            UPDATE jobs SET
                              state='cancelled',
                              reason_code='live_recording_exists',
                              last_error='matching Twitch live stream was already archived',
                              lease_owner=NULL,
                              lease_token=NULL,
                              lease_until=NULL,
                              finished_at=?,
                              updated_at=?
                            WHERE job_type='download'
                              AND media_id IN ({matching_vod_media})
                              AND state IN ('queued', 'retry', 'blocked', 'succeeded')
                            """,
                            (now, now, stream_id),
                        )
                        self.conn.execute(
                            f"""
                            UPDATE jobs SET
                              state='cancelled',
                              reason_code='live_recording_exists',
                              last_error='matching Twitch live stream was already archived',
                              lease_owner=NULL,
                              lease_token=NULL,
                              lease_until=NULL,
                              finished_at=?,
                              updated_at=?
                            WHERE job_type='telegram_delivery'
                              AND media_id IN ({matching_vod_media})
                              AND state IN ('queued', 'retry', 'blocked')
                            """,
                            (now, now, stream_id),
                        )
                        self.conn.execute(
                            f"""
                            UPDATE artifacts SET state='suppressed', updated_at=?
                            WHERE role='master'
                              AND media_id IN ({matching_vod_media})
                              AND state IN ('ready', 'staged', 'suppressed')
                            """,
                            (now, stream_id),
                        )
            max_failures = delivery_max_failures or job.max_attempts
            for destination_key in (
                () if suppress_current_delivery else dict.fromkeys(delivery_targets)
            ):
                self._ensure_job(
                    job.media_id,
                    "telegram_delivery",
                    destination_key,
                    max_failures=max_failures,
                    payload={"destination_key": destination_key},
                )
            self._set_job_succeeded(job, now)
            self.conn.commit()
            return artifact_id
        except Exception:
            self.conn.rollback()
            raise

    def record_artifact(
        self,
        media_id: int,
        *,
        role: str,
        path: Path,
        size_bytes: int,
        part_no: int = 0,
        metadata: dict[str, object] | None = None,
    ) -> int:
        now = now_iso()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._assert_artifact_path_not_reserved(path)
            self.conn.execute(
                """
                INSERT INTO artifacts(
                  media_id, role, part_no, path, size_bytes, state, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'ready', ?, ?, ?)
                ON CONFLICT(media_id, role, part_no) DO UPDATE SET
                  path=excluded.path, size_bytes=excluded.size_bytes, state='ready',
                  metadata_json=excluded.metadata_json, updated_at=excluded.updated_at
                """,
                (
                    media_id,
                    role,
                    part_no,
                    str(path),
                    size_bytes,
                    json.dumps(metadata or {}, sort_keys=True),
                    now,
                    now,
                ),
            )
            artifact_id = int(
                self.conn.execute(
                    "SELECT id FROM artifacts WHERE media_id=? AND role=? AND part_no=?",
                    (media_id, role, part_no),
                ).fetchone()[0]
            )
            self.conn.commit()
            return artifact_id
        except Exception:
            self.conn.rollback()
            raise

    def record_live_segment(
        self,
        media_id: int,
        *,
        path: Path,
        size_bytes: int,
        metadata: dict[str, object] | None = None,
    ) -> int:
        now = now_iso()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._assert_artifact_path_not_reserved(path)
            existing = self.conn.execute(
                """
                SELECT part_no FROM artifacts
                WHERE media_id=? AND role='live_segment' AND path=?
                """,
                (media_id, str(path)),
            ).fetchone()
            if existing is not None:
                self.conn.commit()
                return int(existing["part_no"])
            part_no = int(
                self.conn.execute(
                    """
                    SELECT COALESCE(MAX(part_no), -1) + 1
                    FROM artifacts
                    WHERE media_id=? AND role='live_segment'
                    """,
                    (media_id,),
                ).fetchone()[0]
            )
            self.conn.execute(
                """
                INSERT INTO artifacts(
                  media_id, role, part_no, path, size_bytes, state,
                  metadata_json, created_at, updated_at
                ) VALUES (?, 'live_segment', ?, ?, ?, 'ready', ?, ?, ?)
                """,
                (
                    media_id,
                    part_no,
                    str(path),
                    size_bytes,
                    json.dumps(metadata or {}, sort_keys=True),
                    now,
                    now,
                ),
            )
            self.conn.commit()
            return part_no
        except Exception:
            self.conn.rollback()
            raise

    def live_segment_paths(self, media_id: int) -> list[Path]:
        return [
            Path(str(row["path"]))
            for row in self.conn.execute(
                """
                SELECT path FROM artifacts
                WHERE media_id=? AND role='live_segment' AND state='ready'
                ORDER BY
                  CASE
                    WHEN json_type(metadata_json, '$.attempt_order')='integer'
                    THEN 1 ELSE 0
                  END,
                  CASE
                    WHEN json_type(metadata_json, '$.attempt_order')='integer'
                    THEN CAST(json_extract(metadata_json, '$.attempt_order') AS INTEGER)
                    ELSE part_no
                  END,
                  part_no
                """,
                (media_id,),
            ).fetchall()
            if Path(str(row["path"])).is_file()
        ]

    def complete_delivery(
        self,
        job: ClaimedJob,
        *,
        artifact_id: int,
        destination_key: str,
        remote_id: str,
    ) -> None:
        now = now_iso()
        if destination_key != job.target_key:
            raise RuntimeError("delivery destination does not match the claimed job")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._assert_lease(job)
            self.conn.execute(
                """
                INSERT INTO deliveries(media_id, artifact_id, sink, destination_key, remote_id, delivered_at)
                VALUES (?, ?, 'telegram', ?, ?, ?)
                ON CONFLICT(media_id, sink, destination_key) DO UPDATE SET
                  artifact_id=excluded.artifact_id,
                  remote_id=excluded.remote_id,
                  delivered_at=excluded.delivered_at
                """,
                (job.media_id, artifact_id, destination_key, remote_id, now),
            )
            self._set_job_succeeded(job, now)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def get_media(self, media_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM media_items WHERE id=?", (media_id,)).fetchone()

    def update_media_title(self, media_id: int, title: str | None) -> None:
        if not title:
            return
        self.conn.execute("UPDATE media_items SET title=?, last_seen_at=? WHERE id=?", (title, now_iso(), media_id))
        self.conn.commit()

    def primary_origin_name(self, media_id: int) -> str:
        row = self.conn.execute(
            """
            SELECT o.name FROM origin_items oi
            JOIN origins o ON o.id=oi.origin_id
            WHERE oi.media_id=? AND oi.disposition='eligible'
            ORDER BY oi.first_seen_at, oi.origin_id LIMIT 1
            """,
            (media_id,),
        ).fetchone()
        if row:
            return str(row["name"])
        media = self.get_media(media_id)
        return str(media["provider"]) if media else "media"

    def media_origins(self, media_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT o.id, o.name, oi.disposition
                FROM origin_items oi JOIN origins o ON o.id=oi.origin_id
                WHERE oi.media_id=? ORDER BY oi.first_seen_at, o.id
                """,
                (media_id,),
            )
        )

    def get_artifact(self, media_id: int, role: str = "master", part_no: int = 0) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM artifacts WHERE media_id=? AND role=? AND part_no=?",
            (media_id, role, part_no),
        ).fetchone()

    def has_ready_twitch_live_recording(self, stream_id: str) -> bool:
        if not stream_id:
            return False
        rows = self.conn.execute(
            """
            SELECT a.path
            FROM media_items mi
            JOIN artifacts a ON a.media_id=mi.id
            WHERE mi.provider='twitch'
              AND mi.content_kind='live_stream'
              AND json_extract(mi.metadata_json, '$.stream_id')=?
              AND a.role='master'
              AND a.part_no=0
              AND a.state='ready'
            """,
            (stream_id,),
        ).fetchall()
        return any(Path(str(row["path"])).is_file() for row in rows)

    def twitch_live_recording_state(self, stream_id: str) -> str | None:
        if not stream_id:
            return None
        if self.has_ready_twitch_live_recording(stream_id):
            return "ready"
        row = self.conn.execute(
            """
            SELECT 1
            FROM media_items mi
            JOIN jobs j ON j.media_id=mi.id AND j.job_type='download'
            WHERE mi.provider='twitch'
              AND mi.content_kind='live_stream'
              AND json_extract(mi.metadata_json, '$.stream_id')=?
              AND j.state IN ('queued', 'retry', 'running')
            LIMIT 1
            """,
            (stream_id,),
        ).fetchone()
        return "pending" if row is not None else None

    def recover_stale_jobs(self, *, commit: bool = True) -> None:
        now = now_iso()
        self.conn.execute(
            """
            UPDATE jobs SET
              state='uncertain', reason_code='delivery_uncertain',
              last_error='worker lease expired while delivery result was unknown',
              lease_owner=NULL, lease_token=NULL, lease_until=NULL, updated_at=?
            WHERE job_type='telegram_delivery' AND state='running' AND lease_until <= ?
              AND json_extract(payload_json, '$.phase')='sending'
            """,
            (now, now),
        )
        self.conn.execute(
            """
            UPDATE jobs SET
              state='retry', reason_code='worker_recovered',
              last_error='worker lease expired before Telegram sending began',
              available_at=?, lease_owner=NULL, lease_token=NULL, lease_until=NULL, updated_at=?
            WHERE job_type='telegram_delivery' AND state='running' AND lease_until <= ?
              AND COALESCE(json_extract(payload_json, '$.phase'), 'preparing')!='sending'
            """,
            (now, now, now),
        )
        self.conn.execute(
            """
            UPDATE jobs SET
              state='retry', reason_code='worker_recovered',
              last_error='worker lease expired; job returned to queue',
              available_at=?, lease_owner=NULL, lease_token=NULL, lease_until=NULL, updated_at=?
            WHERE job_type='download' AND state='running' AND lease_until <= ?
            """,
            (now, now, now),
        )
        if commit:
            self.conn.commit()

    def adopt_legacy_delivery_destination(self, destination_key: str) -> None:
        self.conn.execute(
            "UPDATE jobs SET target_key=?, payload_json=? WHERE job_type='telegram_delivery' AND target_key='telegram:legacy'",
            (destination_key, json.dumps({"destination_key": destination_key})),
        )
        self.conn.execute(
            "UPDATE deliveries SET destination_key=? WHERE sink='telegram' AND destination_key='telegram:legacy'",
            (destination_key,),
        )
        self.conn.commit()

    def reconcile_delivery_destination(self, destination_key: str) -> tuple[int, int]:
        migrated = 0
        cancelled = 0
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute(
                """
                SELECT id, media_id FROM jobs
                WHERE job_type='telegram_delivery' AND target_key!=?
                  AND state IN ('queued', 'retry', 'blocked')
                ORDER BY id
                """,
                (destination_key,),
            ).fetchall()
            for row in rows:
                conflict = self.conn.execute(
                    """
                    SELECT 1 FROM jobs
                    WHERE media_id=? AND job_type='telegram_delivery' AND target_key=?
                    """,
                    (row["media_id"], destination_key),
                ).fetchone()
                if conflict:
                    self.conn.execute(
                        """
                        UPDATE jobs SET state='cancelled', reason_code='destination_changed',
                          last_error='superseded by a delivery job for the current destination',
                          finished_at=?, updated_at=? WHERE id=?
                        """,
                        (now_iso(), now_iso(), row["id"]),
                    )
                    cancelled += 1
                else:
                    self.conn.execute(
                        """
                        UPDATE jobs SET target_key=?, payload_json=json_set(
                          payload_json, '$.destination_key', ?
                        ), updated_at=? WHERE id=?
                        """,
                        (destination_key, destination_key, now_iso(), row["id"]),
                    )
                    migrated += 1
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return migrated, cancelled

    # ------------------------------------------------------------------
    # Compatibility API for the existing CLI/control surface

    def has_entries_for_feed(self, feed_id: str) -> bool:
        return self.origin_has_items(feed_id)

    def upsert_entry(self, entry: FeedEntry, *, status: str = "seen", last_error: str | None = None) -> bool:
        self._ensure_legacy_origin(entry.feed_id, entry.feed_name)
        _, created = self.upsert_discovered(
            entry.feed_id,
            MediaCandidate(
                provider="youtube",
                content_kind="video",
                external_id=entry.video_id,
                title=entry.title,
                url=entry.url,
                published_at=entry.published_at,
                updated_at=entry.updated_at,
            ),
            disposition="ignored" if status == "ignored" else "eligible",
            decision_code="legacy_status" if status == "ignored" else None,
            decision_reason=last_error,
        )
        return created

    def enqueue_manual(self, url: str, feed_id: str = "manual", feed_name: str = "Manual", title: str | None = None) -> str:
        from .feed import extract_video_id

        video_id = extract_video_id(url)
        if not video_id:
            raise ValueError(f"Could not extract YouTube video id from {url}")
        self._ensure_legacy_origin(feed_id, feed_name)
        self.upsert_discovered(
            feed_id,
            MediaCandidate(
                provider="youtube",
                content_kind="video",
                external_id=video_id,
                title=title or video_id,
                url=url,
                published_at=None,
            ),
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
        origin_id = f"db:{sub_id}"
        created = self.conn.execute("SELECT 1 FROM origins WHERE id=?", (origin_id,)).fetchone() is None
        clean_routes = [route.strip("/") for route in routes if route.strip("/")] or ["live"]
        self.upsert_origin(
            Origin(
                id=origin_id,
                provider="youtube",
                kind="uploads",
                external_id=channel_id,
                name=name,
                enabled=True,
                options={"routes": clean_routes, "subscription_id": sub_id},
            ),
            managed_by="control",
            created_by=created_by,
        )
        return created

    def delete_subscription(self, sub_id: str) -> bool:
        return self.delete_origin(f"db:{sub_id}")

    def list_subscriptions(self) -> list[Subscription]:
        rows = self.conn.execute(
            "SELECT * FROM origins WHERE managed_by='control' AND provider='youtube' ORDER BY id"
        ).fetchall()
        result = []
        for row in rows:
            options = json.loads(str(row["options_json"] or "{}"))
            result.append(
                Subscription(
                    id=str(options.get("subscription_id") or str(row["id"]).removeprefix("db:")),
                    name=str(row["name"]),
                    channel_id=str(row["external_id"]),
                    routes=[str(route) for route in options.get("routes", ["live"])],
                    enabled=bool(row["enabled"]),
                )
            )
        return result

    def list_pending(self, limit: int, max_attempts: int, include_downloaded: bool) -> list[sqlite3.Row]:
        statuses = ["seen", "waiting_ready", "failed"]
        if include_downloaded:
            statuses.append("downloaded")
        placeholders = ",".join("?" for _ in statuses)
        return list(
            self.conn.execute(
                f"SELECT * FROM ({self._legacy_status_query()}) WHERE status IN ({placeholders}) LIMIT ?",
                (*statuses, limit),
            )
        )

    def begin_download(self, video_id: str) -> None:
        media_id = self._youtube_media_id(video_id)
        if media_id is None:
            return
        now = now_iso()
        self._ensure_job(media_id, "download", "", max_failures=5)
        self.conn.execute(
            "UPDATE jobs SET state='running', failure_count=failure_count+1, lease_owner='legacy', lease_token='legacy', lease_until=?, updated_at=? WHERE media_id=? AND job_type='download'",
            (future_iso(900), now, media_id),
        )
        self.conn.commit()

    def mark_waiting(self, video_id: str, reason: str, retry_seconds: int) -> None:
        self._legacy_set_job(video_id, "download", "retry", reason, retry_seconds, "not_ready")

    def update_title(self, video_id: str, title: str | None) -> None:
        if title:
            self.conn.execute(
                "UPDATE media_items SET title=? WHERE provider='youtube' AND external_id=?",
                (title, video_id),
            )
            self.conn.commit()

    def mark_failed(self, video_id: str, error: str, retry_seconds: int) -> None:
        media_id = self._youtube_media_id(video_id)
        if media_id is None:
            return
        if self.get_artifact(media_id) is not None:
            self._legacy_set_job(video_id, "telegram_delivery", "retry", error, retry_seconds, "delivery_failed")
        else:
            self._legacy_set_job(video_id, "download", "retry", error, retry_seconds, "download_failed")

    def mark_blocked(self, video_id: str, error: str) -> None:
        media_id = self._youtube_media_id(video_id)
        job_type = "telegram_delivery" if media_id and self.get_artifact(media_id) is not None else "download"
        self._legacy_set_job(video_id, job_type, "blocked", error, 0, "blocked")

    def mark_ignored(self, video_id: str, reason: str) -> None:
        media_id = self._youtube_media_id(video_id)
        if media_id is None:
            return
        self.conn.execute(
            "UPDATE origin_items SET disposition='ignored', decision_code='source_filter', decision_reason=? WHERE media_id=?",
            (reason, media_id),
        )
        self.conn.execute("DELETE FROM jobs WHERE media_id=? AND job_type='download' AND state!='succeeded'", (media_id,))
        self.conn.commit()

    def mark_downloaded(self, video_id: str, file_path: Path, file_size: int) -> None:
        media_id = self._youtube_media_id(video_id)
        if media_id is None:
            return
        self.record_artifact(media_id, role="master", path=file_path, size_bytes=file_size)
        self.conn.execute(
            "UPDATE jobs SET state='succeeded', last_error=NULL, reason_code=NULL, lease_owner=NULL, lease_token=NULL, lease_until=NULL, finished_at=?, updated_at=? WHERE media_id=? AND job_type='download'",
            (now_iso(), now_iso(), media_id),
        )
        self.conn.commit()

    def mark_uploaded(self, video_id: str, message_id: int) -> None:
        media_id = self._youtube_media_id(video_id)
        if media_id is None:
            return
        artifact = self.get_artifact(media_id)
        if artifact is None:
            return
        now = now_iso()
        self.conn.execute(
            "INSERT OR REPLACE INTO deliveries(media_id, artifact_id, sink, destination_key, remote_id, delivered_at) VALUES (?, ?, 'telegram', 'telegram:legacy', ?, ?)",
            (media_id, int(artifact["id"]), str(message_id), now),
        )
        self.conn.execute(
            "UPDATE jobs SET state='succeeded', finished_at=?, updated_at=?, last_error=NULL WHERE media_id=? AND job_type='telegram_delivery'",
            (now, now, media_id),
        )
        self.conn.commit()

    def list_recent(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(self.conn.execute(f"SELECT * FROM ({self._legacy_status_query()}) LIMIT ?", (limit,)))

    def counts_by_status(self) -> dict[str, int]:
        rows = self.conn.execute(
            f"SELECT status, COUNT(*) AS count FROM ({self._legacy_status_query()}) GROUP BY status"
        ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def backup_summary(self) -> dict[str, int]:
        counts = self.counts_by_status()
        file_row = self.conn.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(size_bytes), 0) AS bytes FROM artifacts WHERE role='master' AND state='ready'"
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
            "uncertain": counts.get("uncertain", 0),
            "file_count": int(file_row["count"]),
            "file_bytes": int(file_row["bytes"]),
        }

    def get_bot_offset(self) -> int:
        value = self.get_bot_state("last_update_id")
        return int(value) if value is not None else 0

    def set_bot_offset(self, update_id: int) -> None:
        self.set_bot_state("last_update_id", str(update_id))

    def get_bot_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_bot_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO bot_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def list_bot_states(self, key_prefix: str) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            """
            SELECT key, value
            FROM bot_state
            WHERE substr(key, 1, ?) = ?
            ORDER BY key
            """,
            (len(key_prefix), key_prefix),
        ).fetchall()
        return [(str(row["key"]), str(row["value"])) for row in rows]

    def delete_bot_state(self, key: str) -> bool:
        cursor = self.conn.execute(
            "DELETE FROM bot_state WHERE key=?",
            (key,),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    # ------------------------------------------------------------------
    # Internal helpers

    def _ensure_job(
        self,
        media_id: int,
        job_type: str,
        target_key: str,
        *,
        max_failures: int,
        payload: dict[str, object] | None = None,
        state: str = "queued",
        failure_count: int = 0,
        reason_code: str | None = None,
        last_error: str | None = None,
        available_at: str | None = None,
    ) -> int:
        now = now_iso()
        self.conn.execute(
            """
            INSERT INTO jobs(
              media_id, job_type, target_key, state, failure_count, max_failures,
              available_at, reason_code, last_error, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(media_id, job_type, target_key) DO UPDATE SET
              max_failures=MAX(jobs.max_failures, excluded.max_failures),
              payload_json=CASE WHEN excluded.payload_json='{}' THEN jobs.payload_json ELSE excluded.payload_json END,
              updated_at=excluded.updated_at
            """,
            (
                media_id,
                job_type,
                target_key,
                state,
                failure_count,
                max_failures,
                available_at or now,
                reason_code,
                last_error,
                json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        return int(
            self.conn.execute(
                "SELECT id FROM jobs WHERE media_id=? AND job_type=? AND target_key=?",
                (media_id, job_type, target_key),
            ).fetchone()[0]
        )

    def _finish_running_job(
        self,
        job: ClaimedJob,
        *,
        state: str,
        reason_code: str,
        error: str,
        available_at: str,
        increment_failure: bool,
    ) -> None:
        increment = 1 if increment_failure else 0
        finished_at = now_iso() if state in {"blocked", "uncertain"} else None
        cursor = self.conn.execute(
            """
            UPDATE jobs SET
              state=?, failure_count=failure_count+?, available_at=?,
              reason_code=?, last_error=?, lease_owner=NULL, lease_token=NULL,
              lease_until=NULL, updated_at=?, finished_at=?
            WHERE id=? AND state='running' AND lease_token=?
            """,
            (
                state,
                increment,
                available_at,
                reason_code,
                error,
                now_iso(),
                finished_at,
                job.id,
                job.lease_token,
            ),
        )
        if cursor.rowcount != 1:
            self.conn.rollback()
            raise RuntimeError("job lease is no longer owned by this worker")
        self.conn.commit()

    def _assert_lease(self, job: ClaimedJob) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM jobs WHERE id=? AND state='running' AND lease_token=?",
            (job.id, job.lease_token),
        ).fetchone()
        if row is None:
            raise RuntimeError("job lease is no longer owned by this worker")

    def _set_job_succeeded(self, job: ClaimedJob, now: str) -> None:
        cursor = self.conn.execute(
            """
            UPDATE jobs SET state='succeeded', reason_code=NULL, last_error=NULL,
              lease_owner=NULL, lease_token=NULL, lease_until=NULL,
              updated_at=?, finished_at=?
            WHERE id=? AND state='running' AND lease_token=?
            """,
            (now, now, job.id, job.lease_token),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("job lease is no longer owned by this worker")

    def _origin_from_row(self, row: sqlite3.Row) -> Origin:
        return Origin(
            id=str(row["id"]),
            provider=str(row["provider"]),
            kind=str(row["kind"]),
            external_id=str(row["external_id"]),
            name=str(row["name"]),
            enabled=bool(row["enabled"]),
            bootstrap=str(row["bootstrap"]),
            credential_ref=str(row["credential_ref"]) if row["credential_ref"] else None,
            options=json.loads(str(row["options_json"] or "{}")),
        )

    def _ensure_legacy_origin(self, feed_id: str, feed_name: str) -> None:
        if self.conn.execute("SELECT 1 FROM origins WHERE id=?", (feed_id,)).fetchone():
            return
        self.upsert_origin(
            Origin(
                id=feed_id,
                provider="youtube",
                kind="legacy_feed",
                external_id=feed_id,
                name=feed_name,
                enabled=False,
            ),
            managed_by="legacy",
        )

    def _youtube_media_id(self, external_id: str) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM media_items WHERE provider='youtube' AND external_id=? ORDER BY id LIMIT 1",
            (external_id,),
        ).fetchone()
        return int(row["id"]) if row else None

    def _legacy_set_job(
        self,
        video_id: str,
        job_type: str,
        state: str,
        error: str,
        retry_seconds: int,
        reason_code: str,
    ) -> None:
        media_id = self._youtube_media_id(video_id)
        if media_id is None:
            return
        target = "telegram:legacy" if job_type == "telegram_delivery" else ""
        self._ensure_job(media_id, job_type, target, max_failures=5)
        self.conn.execute(
            """
            UPDATE jobs SET state=?, available_at=?, reason_code=?, last_error=?,
              lease_owner=NULL, lease_token=NULL, lease_until=NULL, updated_at=?
            WHERE media_id=? AND job_type=? AND target_key=?
            """,
            (state, future_iso(retry_seconds), reason_code, error, now_iso(), media_id, job_type, target),
        )
        self.conn.commit()

    def _legacy_status_query(self) -> str:
        return """
        SELECT
          mi.external_id AS video_id,
          COALESCE((SELECT oi.origin_id FROM origin_items oi WHERE oi.media_id=mi.id ORDER BY oi.origin_id LIMIT 1), 'manual') AS feed_id,
          COALESCE((SELECT o.name FROM origin_items oi JOIN origins o ON o.id=oi.origin_id WHERE oi.media_id=mi.id ORDER BY oi.origin_id LIMIT 1), mi.provider) AS feed_name,
          mi.title,
          mi.canonical_url AS url,
          mi.published_at,
          mi.source_updated_at AS updated_at,
          mi.first_seen_at,
          mi.last_seen_at,
          CASE
            WHEN EXISTS(SELECT 1 FROM deliveries d WHERE d.media_id=mi.id) THEN 'uploaded'
            WHEN EXISTS(SELECT 1 FROM jobs j WHERE j.media_id=mi.id AND j.job_type='telegram_delivery' AND j.state='uncertain') THEN 'uncertain'
            WHEN EXISTS(SELECT 1 FROM jobs j WHERE j.media_id=mi.id AND j.state='blocked') THEN 'blocked'
            WHEN EXISTS(SELECT 1 FROM artifacts a WHERE a.media_id=mi.id AND a.role='master' AND a.state='ready') THEN 'downloaded'
            WHEN EXISTS(SELECT 1 FROM jobs j WHERE j.media_id=mi.id AND j.job_type='download' AND j.state='running') THEN 'downloading'
            WHEN EXISTS(SELECT 1 FROM jobs j WHERE j.media_id=mi.id AND j.job_type='download' AND j.state='retry' AND j.reason_code='not_ready') THEN 'waiting_ready'
            WHEN EXISTS(SELECT 1 FROM jobs j WHERE j.media_id=mi.id AND j.job_type='download' AND j.state='retry') THEN 'failed'
            WHEN EXISTS(SELECT 1 FROM jobs j WHERE j.media_id=mi.id AND j.job_type='download' AND j.state='queued') THEN 'seen'
            ELSE 'ignored'
          END AS status,
          COALESCE((SELECT j.failure_count FROM jobs j WHERE j.media_id=mi.id AND j.job_type='download' LIMIT 1), 0) AS attempts,
          (SELECT j.available_at FROM jobs j WHERE j.media_id=mi.id AND j.state IN ('queued','retry') ORDER BY j.id LIMIT 1) AS next_retry_at,
          (SELECT a.path FROM artifacts a WHERE a.media_id=mi.id AND a.role='master' LIMIT 1) AS file_path,
          (SELECT a.size_bytes FROM artifacts a WHERE a.media_id=mi.id AND a.role='master' LIMIT 1) AS file_size,
          (SELECT CAST(d.remote_id AS INTEGER) FROM deliveries d WHERE d.media_id=mi.id AND d.sink='telegram' LIMIT 1) AS telegram_message_id,
          COALESCE(
            (SELECT j.last_error FROM jobs j WHERE j.media_id=mi.id AND j.last_error IS NOT NULL ORDER BY j.updated_at DESC LIMIT 1),
            (SELECT oi.decision_reason FROM origin_items oi WHERE oi.media_id=mi.id AND oi.decision_reason IS NOT NULL ORDER BY oi.last_seen_at DESC LIMIT 1)
          ) AS last_error
        FROM media_items mi
        ORDER BY mi.first_seen_at DESC
        """

    def _table_exists(self, name: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone() is not None

    def _harden_sqlite_permissions(self) -> None:
        for path in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            if not path.exists():
                continue
            try:
                path.chmod(0o600)
            except OSError:
                pass

    def _ensure_compatibility_views(self) -> None:
        if not self._table_exists("videos"):
            self.conn.execute(f"CREATE VIEW IF NOT EXISTS videos AS {self._legacy_status_query()}")
        if not self._table_exists("subscriptions"):
            self.conn.execute(
                """
                CREATE VIEW IF NOT EXISTS subscriptions AS
                SELECT
                  COALESCE(json_extract(options_json, '$.subscription_id'), replace(id, 'db:', '')) AS id,
                  name,
                  external_id AS channel_id,
                  COALESCE(json_extract(options_json, '$.routes'), '[\"live\"]') AS routes_json,
                  enabled,
                  created_at,
                  created_by,
                  updated_at
                FROM origins
                WHERE managed_by='control' AND provider='youtube'
                """
            )

    def _backup_v1(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = self.path.with_name(f"{self.path.name}.bak-v1-{stamp}")
        if backup_path.exists():
            return
        destination = sqlite3.connect(backup_path)
        try:
            self.conn.backup(destination)
        finally:
            destination.close()
        try:
            backup_path.chmod(0o600)
        except OSError:
            pass

    def _migrate_v1_rows(self) -> None:
        now = now_iso()
        if self._table_exists("subscriptions"):
            for row in self.conn.execute("SELECT * FROM subscriptions").fetchall():
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO origins(
                      id, provider, kind, external_id, name, managed_by, options_json,
                      enabled, bootstrap, created_at, created_by, updated_at
                    ) VALUES (?, 'youtube', 'uploads', ?, ?, 'control', ?, ?, 'latest', ?, ?, ?)
                    """,
                    (
                        f"db:{row['id']}",
                        row["channel_id"],
                        row["name"],
                        json.dumps({"routes": json.loads(str(row["routes_json"])), "subscription_id": row["id"]}),
                        row["enabled"],
                        row["created_at"],
                        row["created_by"],
                        row["updated_at"],
                    ),
                )

        if not self._table_exists("videos"):
            return
        for row in self.conn.execute("SELECT * FROM videos ORDER BY first_seen_at").fetchall():
            origin_id = str(row["feed_id"] or "legacy:unknown")
            if self.conn.execute("SELECT 1 FROM origins WHERE id=?", (origin_id,)).fetchone() is None:
                self.conn.execute(
                    """
                    INSERT INTO origins(
                      id, provider, kind, external_id, name, managed_by,
                      options_json, enabled, bootstrap, created_at, updated_at
                    ) VALUES (?, 'youtube', 'legacy_feed', ?, ?, 'legacy', '{}', 0, 'latest', ?, ?)
                    """,
                    (origin_id, origin_id, row["feed_name"], now, now),
                )
            self.conn.execute(
                """
                INSERT OR IGNORE INTO media_items(
                  provider, content_kind, external_id, title, canonical_url,
                  published_at, source_updated_at, visibility, metadata_json,
                  first_seen_at, last_seen_at
                ) VALUES ('youtube', 'video', ?, ?, ?, ?, ?, 'public', '{}', ?, ?)
                """,
                (
                    row["video_id"],
                    row["title"],
                    row["url"],
                    row["published_at"],
                    row["updated_at"],
                    row["first_seen_at"],
                    row["last_seen_at"],
                ),
            )
            media_id = int(
                self.conn.execute(
                    "SELECT id FROM media_items WHERE provider='youtube' AND content_kind='video' AND external_id=?",
                    (row["video_id"],),
                ).fetchone()[0]
            )
            disposition = "ignored" if row["status"] == "ignored" else "eligible"
            legacy_error = str(row["last_error"] or "")
            if disposition == "ignored" and "initial" in legacy_error.lower() and "seed ignored" in legacy_error.lower():
                decision_code = "initial_seed"
            elif disposition == "ignored" and "source filter" in legacy_error.lower():
                decision_code = "source_filter"
            elif disposition == "ignored":
                decision_code = "legacy_ignored"
            else:
                decision_code = None
            self.conn.execute(
                """
                INSERT OR IGNORE INTO origin_items(
                  origin_id, media_id, disposition, decision_code, decision_reason,
                  first_seen_at, last_seen_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}')
                """,
                (
                    origin_id,
                    media_id,
                    disposition,
                    decision_code,
                    row["last_error"] if disposition == "ignored" else None,
                    row["first_seen_at"],
                    row["last_seen_at"],
                ),
            )
            file_path = Path(str(row["file_path"])) if row["file_path"] else None
            has_artifact_record = file_path is not None
            artifact_ready = file_path is not None and file_path.exists()
            if has_artifact_record:
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO artifacts(
                      media_id, role, part_no, path, size_bytes, state, metadata_json, created_at, updated_at
                    ) VALUES (?, 'master', 0, ?, ?, ?, '{}', ?, ?)
                    """,
                    (
                        media_id,
                        str(file_path),
                        int(row["file_size"] or 0),
                        "ready" if artifact_ready else "missing",
                        now,
                        now,
                    ),
                )

            status = str(row["status"])
            if disposition != "ignored":
                if artifact_ready and status in {"downloaded", "uploaded", "failed", "blocked"}:
                    download_state = "succeeded"
                    reason = None
                elif status == "blocked":
                    download_state = "blocked"
                    reason = "legacy_blocked"
                else:
                    download_state = "retry" if status in {"waiting_ready", "failed", "downloading"} else "queued"
                    reason = "not_ready" if status == "waiting_ready" else "worker_recovered" if status == "downloading" else "legacy_failure" if status == "failed" else None
                self._ensure_job(
                    media_id,
                    "download",
                    "",
                    max_failures=max(5, int(row["attempts"] or 0) + 1),
                    state=download_state,
                    failure_count=int(row["attempts"] or 0),
                    reason_code=reason,
                    last_error=row["last_error"],
                    available_at=row["next_retry_at"] or now,
                )
                if download_state == "succeeded":
                    self.conn.execute(
                        "UPDATE jobs SET finished_at=? WHERE media_id=? AND job_type='download'",
                        (now, media_id),
                    )

            if status == "uploaded" or (status in {"failed", "blocked"} and artifact_ready):
                delivery_state = "succeeded" if status == "uploaded" else "blocked" if status == "blocked" else "retry"
                self._ensure_job(
                    media_id,
                    "telegram_delivery",
                    "telegram:legacy",
                    max_failures=5,
                    state=delivery_state,
                    reason_code=None if status == "uploaded" else "legacy_delivery_failure",
                    last_error=None if status == "uploaded" else row["last_error"],
                )
                if status == "uploaded" and row["telegram_message_id"] is not None:
                    artifact_row = self.conn.execute(
                        "SELECT id FROM artifacts WHERE media_id=? AND role='master'",
                        (media_id,),
                    ).fetchone()
                    artifact_id = int(artifact_row["id"]) if artifact_row else None
                    self.conn.execute(
                        "INSERT OR IGNORE INTO deliveries(media_id, artifact_id, sink, destination_key, remote_id, delivered_at) VALUES (?, ?, 'telegram', 'telegram:legacy', ?, ?)",
                        (media_id, artifact_id, str(row["telegram_message_id"]), now),
                    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def future_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def row_dict(row: sqlite3.Row) -> dict[str, object]:
    return dict(row)


def _artifact_set_revision(
    rows: list[sqlite3.Row],
    inspections: dict[int, dict[str, object]],
) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: int(item["id"])):
        inspected = inspections[int(row["id"])]
        payload = (
            int(row["id"]),
            str(row["role"]),
            int(row["part_no"]),
            str(row["path"]),
            int(row["size_bytes"]),
            str(row["state"]),
            str(row["metadata_json"]),
            str(row["updated_at"]),
            bool(inspected["exists"]),
            bool(inspected["safe"]),
            int(inspected["actual_bytes"]),
            str(inspected["normalized_path"]),
            inspected["device"],
            inspected["inode"],
            inspected["mtime_ns"],
            inspected["ctime_ns"],
        )
        digest.update(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()[:32]


def _disk_resource_from_row(
    row: sqlite3.Row,
    download_root: Path,
) -> dict[str, object]:
    inspected = _inspect_storage_path(
        Path(str(row["master_path"])),
        download_root,
    )
    return {
        "artifact_id": int(row["artifact_id"]),
        "media_id": int(row["media_id"]),
        "anchor_role": str(row["anchor_role"]),
        "master_path": str(row["master_path"]),
        "master_recorded_bytes": int(row["master_recorded_bytes"]),
        "master_state": str(row["master_state"]),
        "artifact_created_at": str(row["artifact_created_at"]),
        "artifact_updated_at": str(row["artifact_updated_at"]),
        "provider": str(row["provider"]),
        "content_kind": str(row["content_kind"]),
        "external_id": str(row["external_id"]),
        "title": str(row["title"]),
        "canonical_url": str(row["canonical_url"]),
        "published_at": (
            str(row["published_at"]) if row["published_at"] else None
        ),
        "origin_name": str(row["origin_name"]),
        "delivered": bool(row["delivered"]),
        "delivery_job_state": (
            str(row["delivery_job_state"])
            if row["delivery_job_state"]
            else None
        ),
        "running": bool(row["running"]),
        "irreplaceable_live": bool(row["irreplaceable_live"]),
        "artifact_count": int(row["artifact_count"]),
        "recorded_bytes": int(row["recorded_bytes"]),
        "master_exists": bool(inspected["exists"]),
        "master_safe": bool(inspected["safe"]),
        "master_actual_bytes": int(inspected["actual_bytes"]),
        "relative_path": str(inspected["relative_path"]),
        "unsafe_reason": inspected["unsafe_reason"],
    }


def _inspect_storage_path(
    path: Path,
    download_root: Path,
) -> dict[str, object]:
    root = Path(download_root).expanduser().resolve(strict=False)
    candidate = path.expanduser()
    if not candidate.is_absolute():
        return {
            "exists": False,
            "safe": False,
            "actual_bytes": 0,
            "relative_path": candidate.name,
            "normalized_path": candidate,
            "unsafe_reason": "relative paths are not managed",
            "device": None,
            "inode": None,
            "mtime_ns": None,
            "ctime_ns": None,
        }
    normalized = candidate.resolve(strict=False)
    try:
        relative = normalized.relative_to(root)
    except ValueError:
        return {
            "exists": candidate.exists() or candidate.is_symlink(),
            "safe": False,
            "actual_bytes": 0,
            "relative_path": candidate.name,
            "normalized_path": normalized,
            "unsafe_reason": "path is outside the configured downloads directory",
            "device": None,
            "inode": None,
            "mtime_ns": None,
            "ctime_ns": None,
        }
    if relative == Path("."):
        return {
            "exists": True,
            "safe": False,
            "actual_bytes": 0,
            "relative_path": ".",
            "normalized_path": normalized,
            "unsafe_reason": "the downloads directory itself is not a resource file",
            "device": None,
            "inode": None,
            "mtime_ns": None,
            "ctime_ns": None,
        }
    try:
        file_stat = candidate.lstat()
    except FileNotFoundError:
        return {
            "exists": False,
            "safe": True,
            "actual_bytes": 0,
            "relative_path": str(relative),
            "normalized_path": normalized,
            "unsafe_reason": None,
            "device": None,
            "inode": None,
            "mtime_ns": None,
            "ctime_ns": None,
        }
    except OSError as exc:
        return {
            "exists": True,
            "safe": False,
            "actual_bytes": 0,
            "relative_path": str(relative),
            "normalized_path": normalized,
            "unsafe_reason": f"cannot inspect tracked path: {exc}",
            "device": None,
            "inode": None,
            "mtime_ns": None,
            "ctime_ns": None,
        }
    if stat.S_ISLNK(file_stat.st_mode):
        return {
            "exists": True,
            "safe": False,
            "actual_bytes": 0,
            "relative_path": str(relative),
            "normalized_path": normalized,
            "unsafe_reason": "symbolic links are not deleted from the panel",
            "device": int(file_stat.st_dev),
            "inode": int(file_stat.st_ino),
            "mtime_ns": int(file_stat.st_mtime_ns),
            "ctime_ns": int(file_stat.st_ctime_ns),
        }
    if not stat.S_ISREG(file_stat.st_mode):
        return {
            "exists": True,
            "safe": False,
            "actual_bytes": 0,
            "relative_path": str(relative),
            "normalized_path": normalized,
            "unsafe_reason": "tracked path is not a regular file",
            "device": int(file_stat.st_dev),
            "inode": int(file_stat.st_ino),
            "mtime_ns": int(file_stat.st_mtime_ns),
            "ctime_ns": int(file_stat.st_ctime_ns),
        }
    return {
        "exists": True,
        "safe": True,
        "actual_bytes": int(file_stat.st_size),
        "relative_path": str(relative),
        "normalized_path": normalized,
        "unsafe_reason": None,
        "device": int(file_stat.st_dev),
        "inode": int(file_stat.st_ino),
        "mtime_ns": int(file_stat.st_mtime_ns),
        "ctime_ns": int(file_stat.st_ctime_ns),
    }


def _storage_key(path: Path) -> str:
    return str(Path(path).expanduser().resolve(strict=False))


def _path_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_start_id(pid: int) -> str:
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields_after_name = stat_text.rsplit(") ", 1)[1].split()
        return fields_after_name[19]
    except (IndexError, OSError):
        return ""


def _process_instance_is_alive(pid: int, expected_start_id: str) -> bool:
    if not _process_is_alive(pid):
        return False
    actual_start_id = _process_start_id(pid)
    if expected_start_id and actual_start_id:
        return expected_start_id == actual_start_id
    return True


def _storage_identity_matches(
    current: dict[str, object],
    expected: dict[str, object],
) -> bool:
    if (
        bool(current["safe"]) != bool(expected["safe"])
        or bool(current["exists"]) != bool(expected["exists"])
        or str(current["normalized_path"]) != str(expected["normalized_path"])
    ):
        return False
    if not bool(expected["exists"]):
        return True
    return all(
        current[field] == expected[field]
        for field in (
            "device",
            "inode",
            "actual_bytes",
            "mtime_ns",
            "ctime_ns",
        )
    )


def _unlink_tracked_file(
    path: Path,
    download_root: Path,
    expected: dict[str, object],
) -> int:
    """Unlink a confirmed regular file without following swapped path links."""

    root = Path(download_root).expanduser().resolve(strict=True)
    normalized = Path(str(expected["normalized_path"]))
    try:
        relative = normalized.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "tracked file moved outside the configured downloads directory"
        ) from exc
    if relative == Path(".") or not relative.parts:
        raise ValueError("the downloads directory itself cannot be deleted")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("tracked file path contains an unsafe component")

    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise OSError("safe no-follow deletion is not supported on this platform")
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | directory | nofollow | cloexec
    leaf_flags = getattr(os, "O_PATH", os.O_RDONLY) | nofollow | cloexec
    directory_fds: list[int] = []
    leaf_fd: int | None = None
    try:
        directory_fds.append(os.open(root, directory_flags))
        for component in relative.parts[:-1]:
            directory_fds.append(
                os.open(
                    component,
                    directory_flags,
                    dir_fd=directory_fds[-1],
                )
            )
        parent_fd = directory_fds[-1]
        leaf = relative.parts[-1]
        leaf_fd = os.open(leaf, leaf_flags, dir_fd=parent_fd)
        opened_stat = os.fstat(leaf_fd)
        named_stat = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(opened_stat.st_mode) or not stat.S_ISREG(
            named_stat.st_mode
        ):
            raise ValueError("tracked path is no longer a regular file")
        for actual_stat in (opened_stat, named_stat):
            if (
                int(actual_stat.st_dev) != expected["device"]
                or int(actual_stat.st_ino) != expected["inode"]
                or int(actual_stat.st_size) != expected["actual_bytes"]
                or int(actual_stat.st_mtime_ns) != expected["mtime_ns"]
                or int(actual_stat.st_ctime_ns) != expected["ctime_ns"]
            ):
                raise ValueError(
                    "tracked file changed after deletion was reserved"
                )
        os.unlink(leaf, dir_fd=parent_fd)
        return int(opened_stat.st_size)
    finally:
        if leaf_fd is not None:
            os.close(leaf_fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def _like_pattern(value: str) -> str:
    escaped = (
        value.lower()
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def _origin_recording_mode_override(options_json: object) -> str | None:
    try:
        options = json.loads(str(options_json or "{}"))
    except json.JSONDecodeError:
        return None
    if not isinstance(options, dict):
        return None
    mode = str(options.get("recording_mode") or "").lower().strip()
    return mode if mode in {"vod", "live"} else None
