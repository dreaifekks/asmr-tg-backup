from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import uuid

from .feed import FeedEntry
from .models import ClaimedJob, MediaCandidate, Origin


StorageRoots = Path | Sequence[Path]


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

CREATE TABLE IF NOT EXISTS artifact_archive_moves (
  artifact_id INTEGER PRIMARY KEY REFERENCES artifacts(id) ON DELETE CASCADE,
  media_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
  operation_id TEXT NOT NULL UNIQUE,
  source_path TEXT NOT NULL UNIQUE,
  target_path TEXT NOT NULL UNIQUE,
  source_identity_json TEXT NOT NULL,
  state TEXT NOT NULL,
  owner_pid INTEGER NOT NULL,
  owner_start_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  error TEXT
);

CREATE INDEX IF NOT EXISTS idx_artifact_archive_moves_state
ON artifact_archive_moves(state, updated_at, artifact_id);

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

CREATE INDEX IF NOT EXISTS idx_deliveries_retention
ON deliveries(sink, delivered_at, media_id);

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

DELIVERY_RETENTION_MEDIA_CTE = """
media_groups AS (
  SELECT
    mi.id AS media_id,
    CASE
      WHEN mi.provider='twitch'
        AND mi.content_kind IN ('vod', 'live_stream')
        AND json_valid(mi.metadata_json)
        AND trim(
          COALESCE(
            CAST(json_extract(mi.metadata_json, '$.stream_id') AS TEXT),
            ''
          )
        )!=''
      THEN 'twitch:stream:' || trim(
        CAST(json_extract(mi.metadata_json, '$.stream_id') AS TEXT)
      )
      ELSE 'media:' || CAST(mi.id AS TEXT)
    END AS group_key
  FROM media_items mi
)
"""


DELIVERY_PROCESS_RETENTION_SOURCE = "delivery_process_retention"
DELIVERY_PROCESS_ARTIFACT_ROLES = frozenset(
    {"live_segment", "telegram_upload"}
)


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
            self._recover_incomplete_archive_moves()
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
            self._recover_incomplete_archive_moves()
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
        """Make interrupted disk purges visible and retryable after restart."""

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
            SELECT id, path, metadata_json
            FROM artifacts
            WHERE state='purging'
            """
        ).fetchall()
        process_operations = {
            str(purge_metadata.get("operation_id") or "")
            for row in rows
            for metadata in [_json_object(row["metadata_json"])]
            for purge_metadata in [_json_object(metadata.get("local_purge"))]
            if purge_metadata.get("source")
            == DELIVERY_PROCESS_RETENTION_SOURCE
            and purge_metadata.get("operation_id")
        }
        reserved_paths_by_operation: dict[str, set[str]] = {}
        for reservation in reservation_rows:
            operation_id = str(reservation["operation_id"])
            reserved_paths_by_operation.setdefault(operation_id, set()).add(
                str(reservation["storage_key"])
            )

        # A retention purge pins cancelled Twitch fallbacks in phase two, in
        # the same transaction that records the unlink result.  Filesystem
        # deletion cannot roll back, though, so a process death after unlink
        # and before commit would otherwise lose that pin.  Repair only the
        # provable crash window: this operation is dead, this exact path was
        # present when reserved, and its active reservation is now missing.
        interrupted_retention_groups: set[str] = set()
        for row in rows:
            metadata = _json_object(row["metadata_json"])
            purge_metadata = _json_object(metadata.get("local_purge"))
            operation_id = str(purge_metadata.get("operation_id") or "")
            if not operation_id or operation_id in live_operations:
                continue
            if purge_metadata.get("source") != "delivery_retention":
                continue
            if purge_metadata.get("path_existed") is not True:
                continue
            group_key = str(
                purge_metadata.get("retention_group_key") or ""
            )
            storage_key = _storage_key(Path(str(row["path"])))
            if (
                group_key
                and storage_key
                in reserved_paths_by_operation.get(operation_id, set())
                and not _path_entry_exists(Path(storage_key))
            ):
                interrupted_retention_groups.add(group_key)
        for group_key in sorted(interrupted_retention_groups):
            self._pin_retention_cancelled_downloads(
                group_key,
                recovered_at,
            )

        for row in rows:
            metadata = _json_object(row["metadata_json"])
            purge_metadata = _json_object(metadata.get("local_purge"))
            operation_id = str(purge_metadata.get("operation_id") or "")
            if operation_id and operation_id in live_operations:
                continue
            process_path_deleted = (
                purge_metadata.get("source")
                == DELIVERY_PROCESS_RETENTION_SOURCE
                and not _path_entry_exists(Path(str(row["path"])))
            )
            purge_metadata.update(
                {
                    "finished_at": recovered_at,
                    "result": (
                        "deleted" if process_path_deleted else "interrupted"
                    ),
                    "error": "service stopped before local deletion completed",
                }
            )
            metadata["local_purge"] = purge_metadata
            self.conn.execute(
                """
                UPDATE artifacts
                SET state=?, metadata_json=?, updated_at=?
                WHERE id=?
                """,
                (
                    "purged" if process_path_deleted else "purge_failed",
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
            if (
                operation_id in process_operations
                or _path_entry_exists(Path(storage_key))
            ):
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

    def _recover_incomplete_archive_moves(self) -> None:
        """Release archive copy claims left by a dead process."""

        recovered_at = now_iso()
        rows = self.conn.execute(
            """
            SELECT artifact_id, state, owner_pid, owner_start_id
            FROM artifact_archive_moves
            WHERE state IN ('copying', 'source_cleanup')
            """
        ).fetchall()
        for row in rows:
            if _process_instance_is_alive(
                int(row["owner_pid"]),
                str(row["owner_start_id"]),
            ):
                continue
            state = str(row["state"])
            self.conn.execute(
                """
                UPDATE artifact_archive_moves
                SET state=?, owner_pid=0, owner_start_id='', updated_at=?,
                  error=CASE
                    WHEN state='copying'
                    THEN 'archive copy was interrupted and will be retried'
                    ELSE error
                  END
                WHERE artifact_id=? AND state=?
                """,
                (
                    "retry" if state == "copying" else "source_cleanup",
                    recovered_at,
                    int(row["artifact_id"]),
                    state,
                ),
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
        commit: bool = True,
    ) -> None:
        now = now_iso()
        existing = self.conn.execute(
            "SELECT provider, kind, external_id, managed_by, bootstrap FROM origins WHERE id=?",
            (origin.id,),
        ).fetchone()
        control_retarget = False
        activate_backfill = False
        if existing is not None:
            # Provider-specific normalization is part of source identity.
            # Twitch logins are case-insensitive, while YouTube channel ids
            # remain case-sensitive. Recording mode is deliberately excluded:
            # changing VOD/live behavior does not retarget the source itself.
            old_identity = _normalized_origin_identity_values(
                str(existing["provider"]),
                str(existing["kind"]),
                str(existing["external_id"]),
            )[:3]
            new_identity = _normalized_origin_identity(origin)[:3]
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
        if commit:
            self.conn.commit()

    def reconcile_source_catalog(
        self,
        origins: Sequence[Origin],
        source_filter: str,
        *,
        max_failures: int = 5,
    ) -> None:
        """Replace mutable source state with one catalog snapshot.

        ``sources.toml`` is the authority. SQLite keeps the runtime projection,
        discovery state, and media relationships. Legacy origins are retained
        because old media rows can still refer to them, while every other
        origin missing from the catalog is removed.
        """

        from .source_filter import SOURCE_FILTER_STATE_KEY, compile_source_filter

        catalog_origins = list(origins)
        if max_failures <= 0:
            raise ValueError("max_failures must be positive")
        if not isinstance(source_filter, str):
            raise ValueError("source_filter must be a string")
        compile_source_filter(source_filter)

        ids: set[str] = set()
        identities: dict[tuple[str, str, str, str], str] = {}
        for origin in catalog_origins:
            if origin.id in ids:
                raise ValueError(f"duplicate origin id: {origin.id}")
            ids.add(origin.id)
            identity = _normalized_origin_identity(origin)
            other_id = identities.get(identity)
            if other_id is not None and other_id != origin.id:
                raise ValueError(
                    f"origins {other_id!r} and {origin.id!r} have the same source identity"
                )
            identities[identity] = origin.id

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # Refuse silent id changes for a source that already has durable
            # discovery state under another id. This comparison is normalized
            # for provider-specific case rules.
            for row in self.conn.execute(
                "SELECT id, provider, kind, external_id, options_json FROM origins"
            ).fetchall():
                existing_identity = _normalized_origin_identity_values(
                    str(row["provider"]),
                    str(row["kind"]),
                    str(row["external_id"]),
                    options_json=row["options_json"],
                )
                catalog_id = identities.get(existing_identity)
                if catalog_id is not None and catalog_id != str(row["id"]):
                    raise ValueError(
                        f"source identity already belongs to origin {row['id']!r}; "
                        f"cannot assign it to {catalog_id!r}"
                    )

            for origin in catalog_origins:
                self.upsert_origin(
                    origin,
                    managed_by="catalog",
                    max_failures=max_failures,
                    commit=False,
                )

            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(
                    f"DELETE FROM origins WHERE managed_by!='legacy' "
                    f"AND id NOT IN ({placeholders})",
                    tuple(sorted(ids)),
                )
            else:
                self.conn.execute("DELETE FROM origins WHERE managed_by!='legacy'")

            self.conn.execute(
                """
                INSERT INTO bot_state(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (SOURCE_FILTER_STATE_KEY, source_filter),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

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

    def list_delivery_retention_candidates(
        self,
        delivered_before: str,
        *,
        limit: int = 25,
    ) -> list[dict[str, object]]:
        """List tracked resources whose Telegram delivery group may be purged.

        Ordinary media items form one-item groups. Twitch VOD and live rows with
        the same non-empty ``stream_id`` form one group so a successfully
        delivered counterpart can make a suppressed duplicate eligible. The
        newest Telegram delivery in the group controls the retention clock, and
        every job in the group must already be terminal.
        """

        page_limit = max(1, min(100, int(limit)))
        rows = self.conn.execute(
            f"""
            {DISK_RESOURCE_CTE},
            {DELIVERY_RETENTION_MEDIA_CTE},
            blocked_groups AS (
              SELECT DISTINCT member.group_key
              FROM media_groups member
              JOIN jobs j ON j.media_id=member.media_id
              WHERE j.state NOT IN ('succeeded', 'cancelled')
            ),
            eligible_groups AS (
              SELECT
                member.group_key,
                MAX(d.delivered_at) AS delivered_at
              FROM media_groups member
              JOIN deliveries d
                ON d.media_id=member.media_id AND d.sink='telegram'
              WHERE NOT EXISTS (
                SELECT 1
                FROM blocked_groups blocked
                WHERE blocked.group_key=member.group_key
              )
              GROUP BY member.group_key
              HAVING MAX(d.delivered_at) <= ?
            )
            SELECT
              eligible.group_key,
              resources.artifact_id,
              resources.media_id,
              resources.master_state AS artifact_state,
              eligible.delivered_at
            FROM resources
            JOIN artifacts retention_artifact
              ON retention_artifact.id=resources.artifact_id
            JOIN media_groups member ON member.media_id=resources.media_id
            JOIN eligible_groups eligible
              ON eligible.group_key=member.group_key
            WHERE resources.master_state IN (
              'ready', 'suppressed', 'purge_failed'
            )
              AND (
                EXISTS (
                  SELECT 1
                  FROM deliveries own_delivery
                  WHERE own_delivery.media_id=resources.media_id
                    AND own_delivery.sink='telegram'
                )
                OR resources.master_state='suppressed'
                OR (
                  resources.master_state='purge_failed'
                  AND (
                    json_extract(
                      retention_artifact.metadata_json,
                      '$.local_purge.retention_fallback'
                    )=1
                    OR (
                      json_extract(
                        retention_artifact.metadata_json,
                        '$.local_purge.source'
                      )='delivery_retention'
                      AND json_extract(
                        retention_artifact.metadata_json,
                        '$.local_purge.previous_state'
                      )='suppressed'
                    )
                  )
                )
              )
            ORDER BY
              eligible.delivered_at,
              eligible.group_key,
              CASE resources.master_state
                WHEN 'suppressed' THEN 0
                WHEN 'purge_failed' THEN 1
                ELSE 2
              END,
              resources.artifact_id
            LIMIT ?
            """,
            (str(delivered_before), page_limit),
        ).fetchall()
        return [
            {
                "group_key": str(row["group_key"]),
                "artifact_id": int(row["artifact_id"]),
                "media_id": int(row["media_id"]),
                "artifact_state": str(row["artifact_state"]),
                "delivered_at": str(row["delivered_at"]),
            }
            for row in rows
        ]

    def list_process_retention_candidates(
        self,
        delivered_before: str,
        *,
        limit: int = 25,
    ) -> list[dict[str, object]]:
        """List delivered media with explicit process artifacts to remove.

        Unlike full-resource retention, process retention is deliberately
        media-local: a Twitch sibling's delivery never makes another media
        item's process files eligible. Thumbnails are eligible only when their
        metadata explicitly identifies them as Telegram delivery derivatives.
        """

        page_limit = max(1, min(100, int(limit)))
        rows = self.conn.execute(
            f"""
            {DISK_RESOURCE_CTE},
            own_delivery AS (
              SELECT media_id, MAX(delivered_at) AS delivered_at
              FROM deliveries
              WHERE sink='telegram'
              GROUP BY media_id
            )
            SELECT
              'media:' || CAST(resources.media_id AS TEXT) AS group_key,
              resources.artifact_id,
              resources.media_id,
              resources.master_state AS artifact_state,
              own_delivery.delivered_at
            FROM resources
            JOIN own_delivery ON own_delivery.media_id=resources.media_id
            WHERE own_delivery.delivered_at <= ?
              AND resources.anchor_role='master'
              AND resources.master_state='ready'
              AND NOT EXISTS (
                SELECT 1
                FROM jobs j
                WHERE j.media_id=resources.media_id
                  AND j.state NOT IN ('succeeded', 'cancelled')
              )
              AND EXISTS (
                SELECT 1
                FROM artifacts process_artifact
                WHERE process_artifact.media_id=resources.media_id
                  AND process_artifact.state IN ('ready', 'purge_failed')
                  AND (
                    process_artifact.role IN (
                      'live_segment', 'telegram_upload'
                    )
                    OR (
                      process_artifact.role='thumbnail'
                      AND json_valid(process_artifact.metadata_json)
                      AND json_type(
                        process_artifact.metadata_json,
                        '$.delivery_derivative'
                      )='true'
                    )
                  )
              )
            ORDER BY own_delivery.delivered_at, resources.artifact_id
            LIMIT ?
            """,
            (str(delivered_before), page_limit),
        ).fetchall()
        return [
            {
                "group_key": str(row["group_key"]),
                "artifact_id": int(row["artifact_id"]),
                "media_id": int(row["media_id"]),
                "artifact_state": str(row["artifact_state"]),
                "delivered_at": str(row["delivered_at"]),
            }
            for row in rows
        ]

    def list_master_archive_candidates(
        self,
        delivered_before: str,
        *,
        limit: int = 25,
    ) -> list[dict[str, object]]:
        """List delivered canonical masters ready for mounted-disk archival."""

        page_limit = max(1, min(100, int(limit)))
        rows = self.conn.execute(
            f"""
            {DISK_RESOURCE_CTE},
            own_delivery AS (
              SELECT media_id, MAX(delivered_at) AS delivered_at
              FROM deliveries
              WHERE sink='telegram'
              GROUP BY media_id
            )
            SELECT
              'media:' || CAST(resources.media_id AS TEXT) AS group_key,
              resources.artifact_id,
              resources.media_id,
              resources.master_state AS artifact_state,
              own_delivery.delivered_at,
              archive_move.state AS move_state
            FROM resources
            JOIN own_delivery ON own_delivery.media_id=resources.media_id
            JOIN artifacts master ON master.id=resources.artifact_id
            LEFT JOIN artifact_archive_moves archive_move
              ON archive_move.artifact_id=resources.artifact_id
            WHERE own_delivery.delivered_at <= ?
              AND resources.anchor_role='master'
              AND resources.master_state='ready'
              AND json_type(master.metadata_json, '$.archive.archived_at')
                IS NULL
              AND (
                archive_move.artifact_id IS NULL
                OR archive_move.state='retry'
              )
              AND NOT EXISTS (
                SELECT 1
                FROM jobs j
                WHERE j.media_id=resources.media_id
                  AND j.state NOT IN ('succeeded', 'cancelled')
              )
            ORDER BY own_delivery.delivered_at, resources.artifact_id
            LIMIT ?
            """,
            (str(delivered_before), page_limit),
        ).fetchall()
        return [
            {
                "group_key": str(row["group_key"]),
                "artifact_id": int(row["artifact_id"]),
                "media_id": int(row["media_id"]),
                "artifact_state": str(row["artifact_state"]),
                "delivered_at": str(row["delivered_at"]),
                "move_state": (
                    str(row["move_state"])
                    if row["move_state"] is not None
                    else None
                ),
            }
            for row in rows
        ]

    @staticmethod
    def validate_archive_destination(
        download_root: Path,
        archive_root: Path,
        *,
        require_mount: bool = True,
    ) -> None:
        """Fail closed when the configured archive filesystem is unavailable."""

        _validate_archive_root(
            download_root,
            archive_root,
            require_mount=require_mount,
        )

    def list_archive_source_cleanup_candidates(
        self,
        *,
        limit: int = 25,
    ) -> list[int]:
        page_limit = max(1, min(100, int(limit)))
        return [
            int(row["artifact_id"])
            for row in self.conn.execute(
                """
                SELECT artifact_id
                FROM artifact_archive_moves
                WHERE state='source_cleanup'
                ORDER BY updated_at, artifact_id
                LIMIT ?
                """,
                (page_limit,),
            ).fetchall()
        ]

    def list_disk_resources(
        self,
        download_root: StorageRoots,
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
        download_root: StorageRoots,
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

    def _delivery_retention_group_status(
        self,
        media_id: int,
        delivered_before: str,
    ) -> dict[str, object] | None:
        row = self.conn.execute(
            f"""
            WITH
            {DELIVERY_RETENTION_MEDIA_CTE},
            target_group AS (
              SELECT group_key
              FROM media_groups
              WHERE media_id=?
            ),
            group_delivery AS (
              SELECT
                target.group_key,
                MAX(d.delivered_at) AS delivered_at
              FROM target_group target
              JOIN media_groups member
                ON member.group_key=target.group_key
              JOIN deliveries d
                ON d.media_id=member.media_id AND d.sink='telegram'
              GROUP BY target.group_key
            )
            SELECT
              delivery.group_key,
              delivery.delivered_at,
              EXISTS (
                SELECT 1
                FROM deliveries own_delivery
                WHERE own_delivery.media_id=?
                  AND own_delivery.sink='telegram'
              ) AS media_delivered
            FROM group_delivery delivery
            WHERE delivery.delivered_at <= ?
              AND NOT EXISTS (
                SELECT 1
                FROM media_groups member
                JOIN jobs j ON j.media_id=member.media_id
                WHERE member.group_key=delivery.group_key
                  AND j.state NOT IN ('succeeded', 'cancelled')
              )
            """,
            (int(media_id), int(media_id), str(delivered_before)),
        ).fetchone()
        if row is None:
            return None
        return {
            "group_key": str(row["group_key"]),
            "delivered_at": str(row["delivered_at"]),
            "media_delivered": bool(row["media_delivered"]),
        }

    def _delivery_process_retention_status(
        self,
        media_id: int,
        delivered_before: str,
        *,
        include_purging: bool = False,
    ) -> dict[str, object] | None:
        eligible_states = (
            "'ready', 'purge_failed', 'purging'"
            if include_purging
            else "'ready', 'purge_failed'"
        )
        row = self.conn.execute(
            f"""
            SELECT
              MAX(d.delivered_at) AS delivered_at
            FROM deliveries d
            WHERE d.media_id=? AND d.sink='telegram'
            HAVING MAX(d.delivered_at) <= ?
              AND NOT EXISTS (
                SELECT 1
                FROM jobs j
                WHERE j.media_id=?
                  AND j.state NOT IN ('succeeded', 'cancelled')
              )
              AND EXISTS (
                SELECT 1
                FROM artifacts process_artifact
                WHERE process_artifact.media_id=?
                  AND process_artifact.state IN ({eligible_states})
                  AND (
                    process_artifact.role IN (
                      'live_segment', 'telegram_upload'
                    )
                    OR (
                      process_artifact.role='thumbnail'
                      AND json_valid(process_artifact.metadata_json)
                      AND json_type(
                        process_artifact.metadata_json,
                        '$.delivery_derivative'
                      )='true'
                    )
                  )
              )
            """,
            (
                int(media_id),
                str(delivered_before),
                int(media_id),
                int(media_id),
            ),
        ).fetchone()
        if row is None:
            return None
        return {
            "group_key": f"media:{int(media_id)}",
            "delivered_at": str(row["delivered_at"]),
        }

    def _delivery_archive_status(
        self,
        media_id: int,
        delivered_before: str,
    ) -> dict[str, object] | None:
        row = self.conn.execute(
            """
            SELECT MAX(d.delivered_at) AS delivered_at
            FROM deliveries d
            WHERE d.media_id=? AND d.sink='telegram'
            HAVING MAX(d.delivered_at) <= ?
              AND NOT EXISTS (
                SELECT 1
                FROM jobs j
                WHERE j.media_id=?
                  AND j.state NOT IN ('succeeded', 'cancelled')
              )
            """,
            (int(media_id), str(delivered_before), int(media_id)),
        ).fetchone()
        if row is None:
            return None
        return {
            "group_key": f"media:{int(media_id)}",
            "delivered_at": str(row["delivered_at"]),
        }

    @staticmethod
    def _is_delivery_process_artifact(artifact: sqlite3.Row) -> bool:
        role = str(artifact["role"])
        if role in DELIVERY_PROCESS_ARTIFACT_ROLES:
            return True
        if role != "thumbnail":
            return False
        metadata = _json_object(artifact["metadata_json"])
        return metadata.get("delivery_derivative") is True

    @staticmethod
    def _delivery_retention_artifact_is_eligible(
        artifact: sqlite3.Row,
        retention_status: dict[str, object],
    ) -> bool:
        if bool(retention_status["media_delivered"]):
            return True
        return Store._delivery_retention_fallback_artifact(artifact)

    @staticmethod
    def _delivery_retention_fallback_artifact(
        artifact: sqlite3.Row,
    ) -> bool:
        state = str(artifact["state"])
        if state == "suppressed":
            return True
        if state != "purge_failed":
            return False
        metadata = _json_object(artifact["metadata_json"])
        purge_metadata = _json_object(metadata.get("local_purge"))
        return (
            purge_metadata.get("retention_fallback") is True
            or (
                purge_metadata.get("source") == "delivery_retention"
                and purge_metadata.get("previous_state") == "suppressed"
            )
        )

    def _pin_retention_cancelled_downloads(
        self,
        group_key: str,
        changed_at: str,
    ) -> None:
        if not str(group_key).startswith("twitch:stream:"):
            return
        self.conn.execute(
            f"""
            WITH
            {DELIVERY_RETENTION_MEDIA_CTE}
            UPDATE jobs
            SET reason_code='resource_purged',
              last_error=(
                'local archive was deleted after Telegram delivery retention elapsed'
              ),
              finished_at=COALESCE(finished_at, ?),
              updated_at=?
            WHERE job_type='download'
              AND state='cancelled'
              AND reason_code='live_recording_exists'
              AND media_id IN (
                SELECT media_id
                FROM media_groups
                WHERE group_key=?
              )
            """,
            (changed_at, changed_at, str(group_key)),
        )

    @staticmethod
    def _retention_skip_result(
        *,
        artifact_id: int,
        media_id: int,
        title: str,
        updated_at: str,
        resource_revision: str,
        reason: str,
    ) -> dict[str, object]:
        return {
            "artifact_id": artifact_id,
            "media_id": media_id,
            "title": title,
            "completed": False,
            "skipped": True,
            "skip_reason": reason,
            "deleted_files": 0,
            "missing_files": 0,
            "failed_files": 0,
            "freed_bytes": 0,
            "errors": [],
            "updated_at": updated_at,
            "resource_revision": resource_revision,
        }

    def purge_disk_resource(
        self,
        artifact_id: int,
        download_root: StorageRoots,
        *,
        expected_revision: str,
        source: str = "telegram_panel",
        delivery_retention_before: str | None = None,
    ) -> dict[str, object]:
        return self._purge_disk_artifacts(
            artifact_id,
            download_root,
            expected_revision=expected_revision,
            source=source,
            delivery_retention_before=delivery_retention_before,
            process_retention_before=None,
        )

    def purge_process_artifacts(
        self,
        artifact_id: int,
        download_root: StorageRoots,
        *,
        expected_revision: str,
        delivered_before: str,
    ) -> dict[str, object]:
        """Purge only explicitly tracked delivery-process artifacts."""

        return self._purge_disk_artifacts(
            artifact_id,
            download_root,
            expected_revision=expected_revision,
            source=DELIVERY_PROCESS_RETENTION_SOURCE,
            delivery_retention_before=None,
            process_retention_before=delivered_before,
        )

    def archive_master(
        self,
        artifact_id: int,
        download_root: Path,
        archive_root: Path,
        *,
        expected_revision: str,
        delivered_before: str,
        require_mount: bool = True,
    ) -> dict[str, object]:
        """Move one delivered master to a user-mounted archive filesystem."""

        source_root, destination_root = _validate_archive_root(
            download_root,
            archive_root,
            require_mount=require_mount,
        )
        storage_roots = (source_root, destination_root)
        target_id = int(artifact_id)
        operation_id = uuid.uuid4().hex
        claimed_at = now_iso()
        source_path: Path
        target_path: Path
        source_inspection: dict[str, object]
        media_id: int
        title: str

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
            title = str(anchor["media_title"])
            archive_status = self._delivery_archive_status(
                media_id,
                delivered_before,
            )
            metadata = _json_object(anchor["metadata_json"])
            if (
                archive_status is None
                or str(anchor["role"]) != "master"
                or int(anchor["part_no"]) != 0
                or str(anchor["state"]) != "ready"
                or _json_object(metadata.get("archive")).get("archived_at")
            ):
                self.conn.commit()
                return {
                    "artifact_id": target_id,
                    "media_id": media_id,
                    "title": title,
                    "completed": False,
                    "archived": False,
                    "skipped": True,
                    "copied_bytes": 0,
                    "source_deleted": False,
                    "errors": ["archive eligibility changed before transfer"],
                }

            resource_rows = self.conn.execute(
                """
                SELECT id, role, part_no, path, size_bytes, state,
                  metadata_json, updated_at
                FROM artifacts
                WHERE media_id=? AND state!='purged'
                ORDER BY CASE role WHEN 'master' THEN 1 ELSE 0 END, id
                """,
                (media_id,),
            ).fetchall()
            inspected_by_id = {
                int(row["id"]): _inspect_storage_path(
                    Path(str(row["path"])),
                    storage_roots,
                )
                for row in resource_rows
            }
            if (
                _artifact_set_revision(resource_rows, inspected_by_id)
                != str(expected_revision)
            ):
                raise ValueError(
                    "resource changed after archive selection; review it again"
                )
            source_inspection = inspected_by_id[target_id]
            source_path = Path(str(anchor["path"]))
            if (
                not bool(source_inspection["safe"])
                or not bool(source_inspection["exists"])
                or source_inspection.get("storage_root") != str(source_root)
                or int(anchor["size_bytes"]) <= 0
                or int(source_inspection["actual_bytes"])
                != int(anchor["size_bytes"])
            ):
                raise ValueError(
                    "master is missing, incomplete, or already outside downloads"
                )
            relative = Path(str(source_inspection["relative_path"]))
            target_path = destination_root / relative
            existing_move = self.conn.execute(
                """
                SELECT state, owner_pid, owner_start_id, operation_id,
                  source_path, target_path
                FROM artifact_archive_moves
                WHERE artifact_id=?
                """,
                (target_id,),
            ).fetchone()
            if existing_move is not None and str(existing_move["state"]) in {
                "copying",
                "source_cleanup",
            }:
                if _process_instance_is_alive(
                    int(existing_move["owner_pid"]),
                    str(existing_move["owner_start_id"]),
                ):
                    self.conn.commit()
                    return {
                        "artifact_id": target_id,
                        "media_id": media_id,
                        "title": title,
                        "completed": False,
                        "archived": False,
                        "skipped": True,
                        "copied_bytes": 0,
                        "source_deleted": False,
                        "errors": ["another archive transfer owns this master"],
                    }
            if (
                existing_move is not None
                and str(existing_move["state"]) == "retry"
                and str(existing_move["source_path"])
                == str(source_inspection["normalized_path"])
                and str(existing_move["target_path"]) == str(target_path)
            ):
                operation_id = str(existing_move["operation_id"])
            self.conn.execute(
                """
                INSERT INTO artifact_archive_moves(
                  artifact_id, media_id, operation_id, source_path,
                  target_path, source_identity_json, state, owner_pid,
                  owner_start_id, created_at, updated_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, 'copying', ?, ?, ?, ?, NULL)
                ON CONFLICT(artifact_id) DO UPDATE SET
                  media_id=excluded.media_id,
                  operation_id=excluded.operation_id,
                  source_path=excluded.source_path,
                  target_path=excluded.target_path,
                  source_identity_json=excluded.source_identity_json,
                  state='copying',
                  owner_pid=excluded.owner_pid,
                  owner_start_id=excluded.owner_start_id,
                  updated_at=excluded.updated_at,
                  error=NULL
                """,
                (
                    target_id,
                    media_id,
                    operation_id,
                    str(source_inspection["normalized_path"]),
                    str(target_path),
                    json.dumps(
                        _storage_identity_payload(source_inspection),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    os.getpid(),
                    _process_start_id(os.getpid()),
                    claimed_at,
                    claimed_at,
                ),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        try:
            target_inspection, source_digest, _created = (
                _copy_file_to_archive(
                    source_path,
                    target_path,
                    source_inspection=source_inspection,
                    source_root=source_root,
                    archive_root=destination_root,
                    operation_id=operation_id,
                    require_mount=require_mount,
                )
            )
        except Exception as exc:
            self._mark_archive_move_retry(target_id, operation_id, str(exc))
            raise

        archived_at = now_iso()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            move = self.conn.execute(
                """
                SELECT state FROM artifact_archive_moves
                WHERE artifact_id=? AND operation_id=?
                """,
                (target_id, operation_id),
            ).fetchone()
            anchor = self.conn.execute(
                """
                SELECT a.*, mi.title AS media_title
                FROM artifacts a
                JOIN media_items mi ON mi.id=a.media_id
                WHERE a.id=?
                """,
                (target_id,),
            ).fetchone()
            if (
                move is None
                or str(move["state"]) != "copying"
                or anchor is None
                or str(anchor["path"]) != str(source_path)
                or str(anchor["state"]) != "ready"
                or self._delivery_archive_status(media_id, delivered_before)
                is None
            ):
                self.conn.rollback()
                self._mark_archive_move_retry(
                    target_id,
                    operation_id,
                    "archive eligibility changed after copy",
                )
                return {
                    "artifact_id": target_id,
                    "media_id": media_id,
                    "title": title,
                    "completed": False,
                    "archived": False,
                    "skipped": True,
                    "copied_bytes": int(source_inspection["actual_bytes"]),
                    "source_deleted": False,
                    "errors": ["archive eligibility changed after copy"],
                }
            resource_rows = self.conn.execute(
                """
                SELECT id, role, part_no, path, size_bytes, state,
                  metadata_json, updated_at
                FROM artifacts
                WHERE media_id=? AND state!='purged'
                ORDER BY CASE role WHEN 'master' THEN 1 ELSE 0 END, id
                """,
                (media_id,),
            ).fetchall()
            current_inspections = {
                int(row["id"]): _inspect_storage_path(
                    Path(str(row["path"])),
                    storage_roots,
                )
                for row in resource_rows
            }
            current_source = current_inspections.get(target_id)
            current_target = _inspect_storage_path(
                target_path,
                (destination_root,),
            )
            _validate_archive_root(
                source_root,
                destination_root,
                require_mount=require_mount,
            )
            if (
                current_source is None
                or not _storage_identity_matches(
                    current_source,
                    source_inspection,
                )
                or _artifact_set_revision(resource_rows, current_inspections)
                != str(expected_revision)
                or not _storage_identity_matches(
                    current_target,
                    target_inspection,
                )
            ):
                raise ValueError(
                    "master or archive target changed before path commit"
                )
            metadata = _json_object(anchor["metadata_json"])
            metadata["archive"] = {
                "backend": "mounted_filesystem",
                "root": str(destination_root),
                "relative_path": str(target_path.relative_to(destination_root)),
                "source_path": str(source_inspection["normalized_path"]),
                "archived_at": archived_at,
                "verified_at": archived_at,
                "sha256": source_digest,
                "require_mount": bool(require_mount),
                "source_cleanup_pending": True,
            }
            self.conn.execute(
                """
                UPDATE artifacts
                SET path=?, metadata_json=?, updated_at=?
                WHERE id=? AND path=? AND state='ready'
                """,
                (
                    str(target_path),
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    archived_at,
                    target_id,
                    str(source_path),
                ),
            )
            self.conn.execute(
                """
                UPDATE artifact_archive_moves
                SET state='source_cleanup', owner_pid=?, owner_start_id=?,
                  updated_at=?, error=NULL
                WHERE artifact_id=? AND operation_id=? AND state='copying'
                """,
                (
                    os.getpid(),
                    _process_start_id(os.getpid()),
                    archived_at,
                    target_id,
                    operation_id,
                ),
            )
            self.conn.commit()
        except Exception as exc:
            if self.conn.in_transaction:
                self.conn.rollback()
            self._mark_archive_move_retry(target_id, operation_id, str(exc))
            raise

        cleanup = self.cleanup_archived_master_source(
            target_id,
            storage_roots,
        )
        return {
            "artifact_id": target_id,
            "media_id": media_id,
            "title": title,
            "completed": bool(cleanup["completed"]),
            "archived": True,
            "skipped": False,
            "copied_bytes": int(source_inspection["actual_bytes"]),
            "source_deleted": bool(cleanup["source_deleted"]),
            "errors": list(cleanup["errors"]),
            "path": str(target_path),
        }

    def _mark_archive_move_retry(
        self,
        artifact_id: int,
        operation_id: str,
        error: str,
    ) -> None:
        changed_at = now_iso()
        if self.conn.in_transaction:
            self.conn.rollback()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                """
                UPDATE artifact_archive_moves
                SET state='retry', owner_pid=0, owner_start_id='',
                  updated_at=?, error=?
                WHERE artifact_id=? AND operation_id=? AND state='copying'
                """,
                (
                    changed_at,
                    str(error)[:500],
                    int(artifact_id),
                    str(operation_id),
                ),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def cleanup_archived_master_source(
        self,
        artifact_id: int,
        storage_roots: StorageRoots,
    ) -> dict[str, object]:
        """Remove the verified source duplicate after the DB points at archive."""

        target_id = int(artifact_id)
        roots = _normalized_storage_roots(storage_roots)
        move = self.conn.execute(
            """
            SELECT * FROM artifact_archive_moves
            WHERE artifact_id=? AND state='source_cleanup'
            """,
            (target_id,),
        ).fetchone()
        artifact = self.conn.execute(
            "SELECT * FROM artifacts WHERE id=?",
            (target_id,),
        ).fetchone()
        if move is None:
            return {
                "completed": True,
                "source_deleted": False,
                "errors": [],
            }
        if artifact is None:
            self.conn.execute(
                "DELETE FROM artifact_archive_moves WHERE artifact_id=?",
                (target_id,),
            )
            self.conn.commit()
            return {
                "completed": True,
                "source_deleted": False,
                "errors": [],
            }
        metadata = _json_object(artifact["metadata_json"])
        archive_metadata = _json_object(metadata.get("archive"))
        source_expected = _json_object(move["source_identity_json"])
        source_path = Path(str(move["source_path"]))
        target_path = Path(str(move["target_path"]))
        source_now = _inspect_storage_path(source_path, roots)
        target_now = _inspect_storage_path(target_path, roots)
        expected_digest = str(archive_metadata.get("sha256") or "")
        archive_root = Path(str(archive_metadata.get("root") or ""))
        source_root = Path(str(source_expected.get("storage_root") or ""))
        require_mount = archive_metadata.get("require_mount") is not False
        archive_root_error = ""
        try:
            _validated_source, validated_archive = _validate_archive_root(
                source_root,
                archive_root,
                require_mount=require_mount,
            )
        except (OSError, ValueError) as exc:
            archive_root_error = str(exc)
            validated_archive = archive_root
        if (
            str(artifact["path"]) != str(target_path)
            or bool(archive_root_error)
            or not bool(source_now["safe"])
            or not bool(target_now["safe"])
            or not bool(target_now["exists"])
            or target_now.get("storage_root") != str(validated_archive)
            or int(target_now["actual_bytes"]) != int(artifact["size_bytes"])
            or _storage_identities_alias(target_now, source_now)
            or not expected_digest
        ):
            detail = f": {archive_root_error}" if archive_root_error else ""
            error = (
                "archived master is unavailable; source duplicate was kept"
                f"{detail}"
            )
            self.conn.execute(
                """
                UPDATE artifact_archive_moves
                SET owner_pid=0, owner_start_id='', updated_at=?, error=?
                WHERE artifact_id=? AND state='source_cleanup'
                """,
                (now_iso(), error, target_id),
            )
            self.conn.commit()
            return {
                "completed": False,
                "source_deleted": False,
                "errors": [error],
            }
        try:
            target_digest = _hash_storage_file(target_path, target_now)
        except (OSError, ValueError) as exc:
            target_digest = ""
            verification_error = str(exc)
        else:
            verification_error = ""
        if target_digest != expected_digest:
            detail = f": {verification_error}" if verification_error else ""
            error = (
                "archived master checksum changed; source duplicate was kept"
                f"{detail}"
            )
            self.conn.execute(
                """
                UPDATE artifact_archive_moves
                SET owner_pid=0, owner_start_id='', updated_at=?, error=?
                WHERE artifact_id=? AND state='source_cleanup'
                """,
                (now_iso(), error, target_id),
            )
            self.conn.commit()
            return {
                "completed": False,
                "source_deleted": False,
                "errors": [error],
            }
        if bool(source_now["exists"]) and not _storage_identity_matches(
            source_now,
            source_expected,
        ):
            error = "source path was reused; unknown file was not deleted"
            self.conn.execute(
                """
                UPDATE artifact_archive_moves
                SET state='orphaned', owner_pid=0, owner_start_id='',
                  updated_at=?, error=?
                WHERE artifact_id=? AND state='source_cleanup'
                """,
                (now_iso(), error, target_id),
            )
            self.conn.commit()
            return {
                "completed": False,
                "source_deleted": False,
                "errors": [error],
            }

        deleted = False
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            current_move = self.conn.execute(
                """
                SELECT state FROM artifact_archive_moves
                WHERE artifact_id=?
                """,
                (target_id,),
            ).fetchone()
            current_artifact = self.conn.execute(
                "SELECT path, metadata_json FROM artifacts WHERE id=?",
                (target_id,),
            ).fetchone()
            _validate_archive_root(
                source_root,
                archive_root,
                require_mount=require_mount,
            )
            current_source = _inspect_storage_path(source_path, roots)
            current_target = _inspect_storage_path(target_path, roots)
            if (
                current_move is None
                or str(current_move["state"]) != "source_cleanup"
                or current_artifact is None
                or str(current_artifact["path"]) != str(target_path)
                or not _storage_identity_matches(current_target, target_now)
            ):
                raise ValueError("archive cleanup state changed before unlink")
            if bool(current_source["exists"]):
                if not _storage_identity_matches(current_source, source_expected):
                    raise ValueError("source path changed before archive cleanup")
                _unlink_tracked_file(source_path, roots, source_expected)
                deleted = True
            finished_at = now_iso()
            current_metadata = _json_object(current_artifact["metadata_json"])
            current_archive = _json_object(current_metadata.get("archive"))
            current_archive.update(
                {
                    "source_cleanup_pending": False,
                    "source_deleted_at": finished_at,
                }
            )
            current_metadata["archive"] = current_archive
            self.conn.execute(
                """
                UPDATE artifacts SET metadata_json=?, updated_at=? WHERE id=?
                """,
                (
                    json.dumps(
                        current_metadata,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    finished_at,
                    target_id,
                ),
            )
            self.conn.execute(
                "DELETE FROM artifact_archive_moves WHERE artifact_id=?",
                (target_id,),
            )
            self.conn.commit()
        except Exception as exc:
            self.conn.rollback()
            error = str(exc)
            self.conn.execute(
                """
                UPDATE artifact_archive_moves
                SET owner_pid=0, owner_start_id='', updated_at=?, error=?
                WHERE artifact_id=? AND state='source_cleanup'
                """,
                (now_iso(), error[:500], target_id),
            )
            self.conn.commit()
            return {
                "completed": False,
                "source_deleted": False,
                "errors": [error],
            }
        return {
            "completed": True,
            "source_deleted": deleted,
            "errors": [],
        }

    def _purge_disk_artifacts(
        self,
        artifact_id: int,
        download_root: StorageRoots,
        *,
        expected_revision: str,
        source: str,
        delivery_retention_before: str | None,
        process_retention_before: str | None,
    ) -> dict[str, object]:
        """Purge exact tracked files while retaining media and delivery history.

        A short ``purging`` tombstone phase keeps workers from treating an
        intentional deletion as an accidental missing artifact. Panel deletes
        cancel related pending jobs. Retention deletes instead fail closed if
        any job in the delivery group is no longer terminal.
        """

        target_id = int(artifact_id)
        storage_roots = _normalized_storage_roots(download_root)
        operation_id = uuid.uuid4().hex
        requested_at = now_iso()
        purge_source = str(source).strip() or "telegram_panel"
        if (
            delivery_retention_before is not None
            and process_retention_before is not None
        ):
            raise ValueError("full and process retention are mutually exclusive")
        process_cleanup = process_retention_before is not None
        retention_group_key: str | None = None
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
            archive_move = self.conn.execute(
                """
                SELECT state FROM artifact_archive_moves
                WHERE media_id=?
                  AND state IN ('copying', 'retry', 'source_cleanup')
                LIMIT 1
                """,
                (media_id,),
            ).fetchone()
            if archive_move is not None and process_retention_before is None:
                if delivery_retention_before is not None:
                    self.conn.commit()
                    return self._retention_skip_result(
                        artifact_id=target_id,
                        media_id=media_id,
                        title=str(anchor["media_title"]),
                        updated_at=str(anchor["updated_at"]),
                        resource_revision=str(expected_revision),
                        reason="master archive transfer is still being finalized",
                    )
                raise ValueError(
                    "resource is being transferred to the archive directory"
                )
            if delivery_retention_before is not None:
                retention_status = self._delivery_retention_group_status(
                    media_id,
                    delivery_retention_before,
                )
                if (
                    retention_status is None
                    or str(anchor["state"])
                    not in {"ready", "suppressed", "purge_failed"}
                    or not self._delivery_retention_artifact_is_eligible(
                        anchor,
                        retention_status,
                    )
                ):
                    self.conn.commit()
                    return self._retention_skip_result(
                        artifact_id=target_id,
                        media_id=media_id,
                        title=str(anchor["media_title"]),
                        updated_at=str(anchor["updated_at"]),
                        resource_revision=str(expected_revision),
                        reason=(
                            "delivery retention eligibility changed before "
                            "deletion was reserved"
                        ),
                    )
                retention_group_key = str(retention_status["group_key"])
            elif process_retention_before is not None:
                process_status = self._delivery_process_retention_status(
                    media_id,
                    process_retention_before,
                )
                if (
                    process_status is None
                    or str(anchor["role"]) != "master"
                    or int(anchor["part_no"]) != 0
                    or str(anchor["state"]) != "ready"
                ):
                    self.conn.commit()
                    return self._retention_skip_result(
                        artifact_id=target_id,
                        media_id=media_id,
                        title=str(anchor["media_title"]),
                        updated_at=str(anchor["updated_at"]),
                        resource_revision=str(expected_revision),
                        reason=(
                            "process retention eligibility changed before "
                            "deletion was reserved"
                        ),
                    )
                retention_group_key = str(process_status["group_key"])

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
            if (
                delivery_retention_before is None
                and process_retention_before is None
                and running is not None
            ):
                raise ValueError(
                    "resource is currently being downloaded or delivered; try again later"
                )

            resource_rows = self.conn.execute(
                """
                SELECT id, role, part_no, path, size_bytes, state,
                  metadata_json, updated_at
                FROM artifacts
                WHERE media_id=? AND state!='purged'
                ORDER BY CASE role WHEN 'master' THEN 1 ELSE 0 END, id
                """,
                (media_id,),
            ).fetchall()
            artifact_rows = (
                [
                    artifact
                    for artifact in resource_rows
                    if self._is_delivery_process_artifact(artifact)
                    and str(artifact["state"])
                    in {"ready", "purge_failed"}
                ]
                if process_cleanup
                else list(resource_rows)
            )
            if not artifact_rows:
                self.conn.commit()
                return self._retention_skip_result(
                    artifact_id=target_id,
                    media_id=media_id,
                    title=str(anchor["media_title"]),
                    updated_at=str(anchor["updated_at"]),
                    resource_revision=str(expected_revision),
                    reason="no eligible process artifacts remain",
                )
            inspected_by_id: dict[int, dict[str, object]] = {}
            normalized_paths: set[str] = set()
            selected_ids = {int(artifact["id"]) for artifact in artifact_rows}
            for artifact in resource_rows:
                inspected = _inspect_storage_path(
                    Path(str(artifact["path"])),
                    storage_roots,
                )
                artifact_row_id = int(artifact["id"])
                if (
                    artifact_row_id in selected_ids
                    and not bool(inspected["safe"])
                ):
                    raise ValueError(
                        "refusing to delete an unsafe tracked path: "
                        f"{inspected['unsafe_reason']}"
                    )
                inspected_by_id[artifact_row_id] = inspected
                if artifact_row_id in selected_ids:
                    normalized_paths.add(str(inspected["normalized_path"]))
            if process_cleanup:
                retained_master = next(
                    (
                        artifact
                        for artifact in resource_rows
                        if str(artifact["role"]) == "master"
                        and int(artifact["part_no"]) == 0
                        and str(artifact["state"]) == "ready"
                    ),
                    None,
                )
                master_inspection = (
                    inspected_by_id[int(retained_master["id"])]
                    if retained_master is not None
                    else None
                )
                master_recorded_bytes = (
                    int(retained_master["size_bytes"])
                    if retained_master is not None
                    else 0
                )
                if (
                    master_inspection is None
                    or not bool(master_inspection["safe"])
                    or not bool(master_inspection["exists"])
                    or master_recorded_bytes <= 0
                    or int(master_inspection["actual_bytes"])
                    != master_recorded_bytes
                ):
                    current_revision = _artifact_set_revision(
                        resource_rows,
                        inspected_by_id,
                    )
                    self.conn.commit()
                    return self._retention_skip_result(
                        artifact_id=target_id,
                        media_id=media_id,
                        title=str(anchor["media_title"]),
                        updated_at=str(anchor["updated_at"]),
                        resource_revision=current_revision,
                        reason=(
                            "retained master is missing, unsafe, or incomplete; "
                            "process artifacts were kept"
                        ),
                    )
            if (
                _artifact_set_revision(resource_rows, inspected_by_id)
                != str(expected_revision)
            ):
                raise ValueError(
                    "resource changed after confirmation; review it again"
                )

            if self._find_shared_resource_path(
                selected_ids,
                normalized_paths,
                storage_roots,
            ) is not None:
                raise ValueError(
                    "refusing to delete a file referenced by another media item "
                    "or retained artifact"
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

            retention_fallback = (
                delivery_retention_before is not None
                and self._delivery_retention_fallback_artifact(anchor)
            )
            for artifact in artifact_rows:
                metadata = _json_object(artifact["metadata_json"])
                purge_metadata: dict[str, object] = {
                    "operation_id": operation_id,
                    "requested_at": requested_at,
                    "previous_state": str(artifact["state"]),
                    "source": purge_source,
                }
                if delivery_retention_before is not None:
                    purge_metadata.update(
                        {
                            "delivery_retention_before": str(
                                delivery_retention_before
                            ),
                            "retention_group_key": retention_group_key,
                            "retention_fallback": retention_fallback,
                            "path_existed": bool(
                                inspected_by_id[int(artifact["id"])]["exists"]
                            ),
                        }
                    )
                elif process_retention_before is not None:
                    purge_metadata.update(
                        {
                            "delivery_retention_before": str(
                                process_retention_before
                            ),
                            "retention_group_key": retention_group_key,
                            "retention_scope": "process",
                            "path_existed": bool(
                                inspected_by_id[int(artifact["id"])]["exists"]
                            ),
                        }
                    )
                metadata["local_purge"] = purge_metadata
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
            if (
                delivery_retention_before is None
                and process_retention_before is None
            ):
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
            resource_rows=resource_rows,
            inspected_by_id=inspected_by_id,
            root=storage_roots,
            operation_id=operation_id,
            requested_at=requested_at,
            source=purge_source,
            delivery_retention_before=delivery_retention_before,
            process_retention_before=process_retention_before,
            retention_group_key=retention_group_key,
        )

    def _find_shared_resource_path(
        self,
        selected_artifact_ids: set[int],
        normalized_paths: set[str],
        root: StorageRoots,
    ) -> str | None:
        if not selected_artifact_ids:
            return None
        placeholders = ",".join("?" for _ in selected_artifact_ids)
        other_paths = self.conn.execute(
            f"""
            SELECT path
            FROM artifacts
            WHERE state!='purged'
              AND id NOT IN ({placeholders})
            """,
            tuple(sorted(selected_artifact_ids)),
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
        resource_rows: list[sqlite3.Row],
        inspected_by_id: dict[int, dict[str, object]],
        root: StorageRoots,
        operation_id: str,
        requested_at: str,
        source: str,
        delivery_retention_before: str | None,
        process_retention_before: str | None,
        retention_group_key: str | None,
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
                for row in resource_rows
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
            selected_ids = {int(row["id"]) for row in artifact_rows}
            original_by_id = {
                int(row["id"]): row for row in resource_rows
            }
            validation_error: str | None = None
            if current_shape != expected_shape or any(
                (
                    str(row["state"]) != "purging"
                    if int(row["id"]) in selected_ids
                    else (
                        int(row["id"]) not in original_by_id
                        or str(row["state"])
                        != str(original_by_id[int(row["id"])]["state"])
                    )
                )
                for row in current_rows
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

            retention_skip_reason: str | None = None
            if delivery_retention_before is not None:
                retention_status = self._delivery_retention_group_status(
                    media_id,
                    delivery_retention_before,
                )
                target_artifact = next(
                    (
                        row
                        for row in resource_rows
                        if int(row["id"]) == target_id
                    ),
                    None,
                )
                if (
                    retention_status is None
                    or str(retention_status["group_key"])
                    != str(retention_group_key)
                    or target_artifact is None
                    or not self._delivery_retention_artifact_is_eligible(
                        target_artifact,
                        retention_status,
                    )
                ):
                    retention_skip_reason = (
                        "delivery retention eligibility changed after deletion "
                        "was reserved"
                    )
            elif process_retention_before is not None:
                process_status = self._delivery_process_retention_status(
                    media_id,
                    process_retention_before,
                    include_purging=True,
                )
                if (
                    process_status is None
                    or str(process_status["group_key"])
                    != str(retention_group_key)
                ):
                    retention_skip_reason = (
                        "process retention eligibility changed after deletion "
                        "was reserved"
                    )

            if retention_skip_reason is not None:
                skipped_at = now_iso()
                original_by_id = {
                    int(row["id"]): row for row in artifact_rows
                }
                for current in current_rows:
                    artifact_row_id = int(current["id"])
                    original = original_by_id.get(artifact_row_id)
                    if original is None:
                        continue
                    metadata = _json_object(current["metadata_json"])
                    purge_metadata = _json_object(metadata.get("local_purge"))
                    purge_metadata.update(
                        {
                            "finished_at": skipped_at,
                            "result": "skipped",
                            "error": retention_skip_reason,
                        }
                    )
                    metadata["local_purge"] = purge_metadata
                    self.conn.execute(
                        """
                        UPDATE artifacts
                        SET state=?, metadata_json=?, updated_at=?
                        WHERE id=? AND state='purging'
                        """,
                        (
                            str(original["state"]),
                            json.dumps(
                                metadata,
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            skipped_at,
                            artifact_row_id,
                        ),
                    )
                self.conn.execute(
                    """
                    DELETE FROM purge_path_reservations
                    WHERE operation_id=? AND state='active'
                    """,
                    (operation_id,),
                )
                self.conn.commit()
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
                anchor_row = self.conn.execute(
                    "SELECT updated_at FROM artifacts WHERE id=?",
                    (target_id,),
                ).fetchone()
                return self._retention_skip_result(
                    artifact_id=target_id,
                    media_id=media_id,
                    title=title,
                    updated_at=(
                        str(anchor_row["updated_at"])
                        if anchor_row is not None
                        else skipped_at
                    ),
                    resource_revision=_artifact_set_revision(
                        remaining_rows,
                        remaining_inspections,
                    ),
                    reason=retention_skip_reason,
                )

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
                    if (
                        artifact_row_id in selected_ids
                        and not bool(current["safe"])
                    ):
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
                    selected_ids,
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
                if (
                    delivery_retention_before is None
                    and process_retention_before is None
                ):
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
                elif delivery_retention_before is not None:
                    if retention_group_key is None:
                        raise RuntimeError(
                            "delivery retention group was lost before deletion"
                        )
                    self._pin_retention_cancelled_downloads(
                        retention_group_key,
                        requested_at,
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
                    "source": source,
                    "result": str(outcome["status"]),
                }
                if delivery_retention_before is not None:
                    purge_metadata.update(
                        {
                            "delivery_retention_before": str(
                                delivery_retention_before
                            ),
                            "retention_group_key": retention_group_key,
                            "retention_fallback": (
                                self._delivery_retention_fallback_artifact(
                                    artifact
                                )
                            ),
                            "path_existed": bool(
                                inspected_by_id[artifact_row_id]["exists"]
                            ),
                        }
                    )
                elif process_retention_before is not None:
                    purge_metadata.update(
                        {
                            "delivery_retention_before": str(
                                process_retention_before
                            ),
                            "retention_group_key": retention_group_key,
                            "retention_scope": "process",
                            "path_existed": bool(
                                inspected_by_id[artifact_row_id]["exists"]
                            ),
                        }
                    )
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
                if process_retention_before is not None:
                    self.conn.execute(
                        """
                        DELETE FROM purge_path_reservations
                        WHERE storage_key=? AND operation_id=?
                          AND state='active'
                        """,
                        (normalized, operation_id),
                    )
                    continue
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
                    release_reservations=(
                        process_retention_before is not None
                    ),
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
            "skipped": False,
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
        *,
        release_reservations: bool = False,
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
                if release_reservations or _path_entry_exists(
                    Path(storage_key)
                ):
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
        recording_mode = str(recording_mode).strip()
        if not recording_mode:
            raise ValueError("recording_mode must not be empty")
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
        archive_row = self.conn.execute(
            """
            SELECT artifact_id
            FROM artifact_archive_moves
            WHERE state IN ('copying', 'source_cleanup')
              AND (source_path=? OR target_path=?)
            """,
            (storage_key, storage_key),
        ).fetchone()
        if archive_row is not None:
            raise RuntimeError(
                "artifact path is reserved for transfer to the archive directory"
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
            active_archive = self.conn.execute(
                """
                SELECT move.state
                FROM artifacts master
                JOIN artifact_archive_moves move
                  ON move.artifact_id=master.id
                WHERE master.media_id=? AND master.role='master'
                  AND master.part_no=0
                  AND move.state IN ('copying', 'source_cleanup')
                LIMIT 1
                """,
                (job.media_id,),
            ).fetchone()
            if active_archive is not None:
                raise RuntimeError(
                    "canonical master archive transfer is still being finalized"
                )
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
                  state=excluded.state,
                  metadata_json=json_remove(
                    artifacts.metadata_json,
                    '$.archive'
                  ),
                  updated_at=excluded.updated_at
                """,
                (job.media_id, str(path), size_bytes, artifact_state, now, now),
            )
            artifact_id = int(
                self.conn.execute(
                    "SELECT id FROM artifacts WHERE media_id=? AND role='master' AND part_no=0",
                    (job.media_id,),
                ).fetchone()[0]
            )
            self.conn.execute(
                """
                DELETE FROM artifact_archive_moves
                WHERE artifact_id=? AND state IN ('retry', 'orphaned')
                """,
                (artifact_id,),
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
                SELECT id, part_no, state FROM artifacts
                WHERE media_id=? AND role='live_segment' AND path=?
                """,
                (media_id, str(path)),
            ).fetchone()
            if existing is not None:
                if str(existing["state"]) in {"purged", "purge_failed"}:
                    self.conn.execute(
                        """
                        UPDATE artifacts
                        SET size_bytes=?, state='ready', metadata_json=?,
                          updated_at=?
                        WHERE id=? AND state IN ('purged', 'purge_failed')
                        """,
                        (
                            int(size_bytes),
                            json.dumps(metadata or {}, sort_keys=True),
                            now,
                            int(existing["id"]),
                        ),
                    )
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
        """Return whether a live backup already supersedes its Twitch VOD.

        A confirmed Telegram delivery is durable workflow state and must not
        regress merely because a local or mounted replica is temporarily
        unavailable. Before delivery, an existing ready master still protects
        the in-flight live recording from duplicate VOD work.
        """

        if not stream_id:
            return False
        delivered = self.conn.execute(
            """
            SELECT 1
            FROM media_items mi
            JOIN deliveries d
              ON d.media_id=mi.id AND d.sink='telegram'
            WHERE mi.provider='twitch'
              AND mi.content_kind='live_stream'
              AND json_extract(mi.metadata_json, '$.stream_id')=?
            LIMIT 1
            """,
            (stream_id,),
        ).fetchone()
        if delivered is not None:
            return True
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
            inspected.get("storage_root"),
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
    storage_roots: StorageRoots,
) -> dict[str, object]:
    inspected = _inspect_storage_path(
        Path(str(row["master_path"])),
        storage_roots,
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
        "master_storage_root": inspected["storage_root"],
        "relative_path": str(inspected["relative_path"]),
        "unsafe_reason": inspected["unsafe_reason"],
    }


def _normalized_storage_roots(storage_roots: StorageRoots) -> tuple[Path, ...]:
    if isinstance(storage_roots, (str, os.PathLike)):
        values = (Path(storage_roots),)
    else:
        values = tuple(Path(value) for value in storage_roots)
    normalized: list[Path] = []
    for value in values:
        root = value.expanduser().resolve(strict=False)
        if root not in normalized:
            normalized.append(root)
    if not normalized:
        raise ValueError("at least one managed storage root is required")
    return tuple(normalized)


def _inspect_storage_path(
    path: Path,
    storage_roots: StorageRoots,
) -> dict[str, object]:
    roots = _normalized_storage_roots(storage_roots)
    candidate = path.expanduser()
    if not candidate.is_absolute():
        return {
            "exists": False,
            "safe": False,
            "actual_bytes": 0,
            "relative_path": candidate.name,
            "normalized_path": candidate,
            "storage_root": None,
            "unsafe_reason": "relative paths are not managed",
            "device": None,
            "inode": None,
            "mtime_ns": None,
            "ctime_ns": None,
        }
    normalized = candidate.resolve(strict=False)
    matches: list[tuple[Path, Path]] = []
    for root in roots:
        try:
            matches.append((root, normalized.relative_to(root)))
        except ValueError:
            continue
    if not matches:
        return {
            "exists": candidate.exists() or candidate.is_symlink(),
            "safe": False,
            "actual_bytes": 0,
            "relative_path": candidate.name,
            "normalized_path": normalized,
            "storage_root": None,
            "unsafe_reason": "path is outside the configured storage roots",
            "device": None,
            "inode": None,
            "mtime_ns": None,
            "ctime_ns": None,
        }
    root, relative = max(matches, key=lambda item: len(item[0].parts))
    try:
        root_stat = root.lstat()
    except OSError as exc:
        return {
            "exists": candidate.exists() or candidate.is_symlink(),
            "safe": False,
            "actual_bytes": 0,
            "relative_path": str(relative),
            "normalized_path": normalized,
            "storage_root": str(root),
            "unsafe_reason": f"configured storage root is unavailable: {exc}",
            "device": None,
            "inode": None,
            "mtime_ns": None,
            "ctime_ns": None,
        }
    if not stat.S_ISDIR(root_stat.st_mode):
        return {
            "exists": candidate.exists() or candidate.is_symlink(),
            "safe": False,
            "actual_bytes": 0,
            "relative_path": str(relative),
            "normalized_path": normalized,
            "storage_root": str(root),
            "unsafe_reason": "configured storage root is not a directory",
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
            "storage_root": str(root),
            "unsafe_reason": "a configured storage root is not a resource file",
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
            "storage_root": str(root),
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
            "storage_root": str(root),
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
            "storage_root": str(root),
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
            "storage_root": str(root),
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
        "storage_root": str(root),
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
        or current.get("storage_root") != expected.get("storage_root")
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


def _storage_identities_alias(
    left: dict[str, object],
    right: dict[str, object],
) -> bool:
    return (
        bool(left.get("exists"))
        and bool(right.get("exists"))
        and left.get("device") is not None
        and left.get("device") == right.get("device")
        and left.get("inode") == right.get("inode")
    )


def _storage_identity_payload(
    inspection: dict[str, object],
) -> dict[str, object]:
    return {
        key: inspection.get(key)
        for key in (
            "exists",
            "safe",
            "actual_bytes",
            "normalized_path",
            "storage_root",
            "device",
            "inode",
            "mtime_ns",
            "ctime_ns",
        )
    } | {
        "normalized_path": str(inspection["normalized_path"]),
    }


def _decode_mountinfo_path(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _is_on_nonroot_mount(path: Path) -> bool:
    resolved = path.resolve(strict=True)
    mountinfo = Path("/proc/self/mountinfo")
    try:
        lines = mountinfo.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        fields = line.split()
        if len(fields) < 6:
            continue
        mount_point = Path(_decode_mountinfo_path(fields[4])).resolve(
            strict=False
        )
        if mount_point == Path("/"):
            continue
        if resolved == mount_point or mount_point in resolved.parents:
            return True
    return resolved != Path("/") and resolved.is_mount()


def _validate_archive_root(
    download_root: Path,
    archive_root: Path,
    *,
    require_mount: bool,
) -> tuple[Path, Path]:
    source_root = Path(download_root).expanduser().resolve(strict=False)
    configured_archive = Path(archive_root).expanduser()
    if not configured_archive.is_absolute():
        raise ValueError("archive directory must be an absolute path")
    try:
        configured_stat = configured_archive.lstat()
    except OSError as exc:
        raise ValueError(
            f"archive directory is unavailable; source was kept: {exc}"
        ) from exc
    if stat.S_ISLNK(configured_stat.st_mode):
        raise ValueError("archive directory must not be a symbolic link")
    if not stat.S_ISDIR(configured_stat.st_mode):
        raise ValueError("archive directory is not a directory")
    resolved_archive = configured_archive.resolve(strict=True)
    if (
        resolved_archive == source_root
        or resolved_archive in source_root.parents
        or source_root in resolved_archive.parents
    ):
        raise ValueError(
            "archive directory must be separate from the downloads directory"
        )
    if require_mount and not _is_on_nonroot_mount(resolved_archive):
        raise ValueError(
            "archive directory is not on a non-root mount; source was kept"
        )
    return source_root, resolved_archive


def _hash_storage_file(
    path: Path,
    expected: dict[str, object],
) -> str:
    normalized = Path(path).expanduser().resolve(strict=False)
    if str(normalized) != str(expected["normalized_path"]):
        raise ValueError("tracked file path changed before it could be hashed")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, os.O_RDONLY | nofollow | cloexec)
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or int(opened.st_dev) != expected["device"]
            or int(opened.st_ino) != expected["inode"]
            or int(opened.st_size) != expected["actual_bytes"]
            or int(opened.st_mtime_ns) != expected["mtime_ns"]
            or int(opened.st_ctime_ns) != expected["ctime_ns"]
        ):
            raise ValueError("tracked file changed before it could be hashed")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
        if (
            int(after.st_size) != expected["actual_bytes"]
            or int(after.st_mtime_ns) != expected["mtime_ns"]
            or int(after.st_ctime_ns) != expected["ctime_ns"]
        ):
            raise ValueError("tracked file changed while it was being hashed")
        return digest.hexdigest()
    finally:
        os.close(fd)


def _ensure_archive_parent(archive_root: Path, relative: Path) -> Path:
    current = archive_root
    for component in relative.parts:
        if component in {"", ".", ".."}:
            raise ValueError("archive path contains an unsafe component")
        current = current / component
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            current_stat = current.lstat()
        if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(
            current_stat.st_mode
        ):
            raise ValueError("archive parent is not a safe directory")
    return current


def _fsync_directory(path: Path) -> None:
    """Persist a directory mutation when the backing filesystem supports it."""

    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, os.O_RDONLY | directory | cloexec)
    except OSError as exc:
        if exc.errno in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
            return
        raise
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in {
                errno.EINVAL,
                errno.ENOTSUP,
                errno.EOPNOTSUPP,
            }:
                raise
    finally:
        os.close(fd)


def _archive_target_matches(
    target: Path,
    archive_root: Path,
    *,
    size_bytes: int,
    sha256: str,
) -> dict[str, object] | None:
    inspected = _inspect_storage_path(target, (archive_root,))
    if not bool(inspected["exists"]):
        if not bool(inspected["safe"]):
            raise ValueError(
                f"archive target path is unsafe: {inspected['unsafe_reason']}"
            )
        return None
    if (
        not bool(inspected["safe"])
        or int(inspected["actual_bytes"]) != int(size_bytes)
        or _hash_storage_file(target, inspected) != sha256
    ):
        raise ValueError("archive target already exists with different content")
    return inspected


def _publish_archive_temp(
    temp_path: Path,
    target: Path,
    archive_root: Path,
    *,
    size_bytes: int,
    sha256: str,
) -> bool:
    """Publish a verified temp file without replacing an existing target."""

    existing = _archive_target_matches(
        target,
        archive_root,
        size_bytes=size_bytes,
        sha256=sha256,
    )
    if existing is not None:
        temp_path.unlink()
        _fsync_directory(target.parent)
        return False

    try:
        # Temp and target share a directory, so a hard link gives us atomic
        # no-clobber publication on filesystems that implement it.
        os.link(temp_path, target, follow_symlinks=False)
    except FileExistsError:
        _archive_target_matches(
            target,
            archive_root,
            size_bytes=size_bytes,
            sha256=sha256,
        )
        temp_path.unlink()
        _fsync_directory(target.parent)
        return False
    except OSError as exc:
        if exc.errno not in {
            errno.EPERM,
            errno.EXDEV,
            errno.ENOSYS,
            errno.ENOTSUP,
            errno.EOPNOTSUPP,
        }:
            raise
        # Some FUSE/object-storage mounts support atomic rename but not hard
        # links. Recheck immediately before rename; this root is expected to be
        # dedicated to the service, and phase two verifies the published inode.
        if _archive_target_matches(
            target,
            archive_root,
            size_bytes=size_bytes,
            sha256=sha256,
        ) is not None:
            temp_path.unlink()
            _fsync_directory(target.parent)
            return False
        os.replace(temp_path, target)
    else:
        temp_path.unlink()
    _fsync_directory(target.parent)
    return True


def _copy_file_to_archive(
    source: Path,
    target: Path,
    *,
    source_inspection: dict[str, object],
    source_root: Path,
    archive_root: Path,
    operation_id: str,
    require_mount: bool,
) -> tuple[dict[str, object], str, bool]:
    if _storage_key(source) != str(source_inspection["normalized_path"]):
        raise ValueError("master path changed before archive copy started")
    _validate_archive_root(
        source_root,
        archive_root,
        require_mount=require_mount,
    )
    relative_target = target.relative_to(archive_root)
    parent = _ensure_archive_parent(archive_root, relative_target.parent)
    temp_path = parent / f".{target.name}.archive-{operation_id}.part"
    if _path_entry_exists(temp_path):
        temp_stat = temp_path.lstat()
        if not stat.S_ISREG(temp_stat.st_mode):
            raise ValueError("archive temporary path is not a regular file")
        temp_path.unlink()

    existing_target = _inspect_storage_path(target, (archive_root,))
    if bool(existing_target["exists"]):
        source_digest = _hash_storage_file(source, source_inspection)
        matched_target = _archive_target_matches(
            target,
            archive_root,
            size_bytes=int(source_inspection["actual_bytes"]),
            sha256=source_digest,
        )
        _validate_archive_root(
            source_root,
            archive_root,
            require_mount=require_mount,
        )
        if matched_target is None:
            raise RuntimeError("archive target disappeared during verification")
        if _storage_identities_alias(matched_target, source_inspection):
            raise ValueError("archive target aliases the source master")
        return matched_target, source_digest, False
    if not bool(existing_target["safe"]):
        raise ValueError(
            f"archive target path is unsafe: {existing_target['unsafe_reason']}"
        )

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    source_fd = os.open(source, os.O_RDONLY | nofollow | cloexec)
    temp_fd: int | None = None
    created = False
    try:
        opened = os.fstat(source_fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or int(opened.st_dev) != source_inspection["device"]
            or int(opened.st_ino) != source_inspection["inode"]
            or int(opened.st_size) != source_inspection["actual_bytes"]
            or int(opened.st_mtime_ns) != source_inspection["mtime_ns"]
            or int(opened.st_ctime_ns) != source_inspection["ctime_ns"]
        ):
            raise ValueError("master changed before archive copy started")
        digest = hashlib.sha256()
        temp_fd = os.open(
            temp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | cloexec,
            0o600,
        )
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            offset = 0
            while offset < len(chunk):
                written = os.write(temp_fd, chunk[offset:])
                if written <= 0:
                    raise OSError("archive copy stopped before the chunk was written")
                offset += written
        os.fsync(temp_fd)
        copied_stat = os.fstat(temp_fd)
        source_after = os.fstat(source_fd)
        if int(copied_stat.st_size) != source_inspection["actual_bytes"]:
            raise OSError("archive copy size does not match the master")
        if (
            int(source_after.st_size) != source_inspection["actual_bytes"]
            or int(source_after.st_mtime_ns) != source_inspection["mtime_ns"]
            or int(source_after.st_ctime_ns) != source_inspection["ctime_ns"]
        ):
            raise ValueError("master changed while it was copied")
        source_digest = digest.hexdigest()
    except Exception:
        if temp_fd is not None:
            os.close(temp_fd)
            temp_fd = None
        if _path_entry_exists(temp_path):
            temp_path.unlink()
        raise
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        os.close(source_fd)

    _validate_archive_root(
        source_root,
        archive_root,
        require_mount=require_mount,
    )
    try:
        created = _publish_archive_temp(
            temp_path,
            target,
            archive_root,
            size_bytes=int(source_inspection["actual_bytes"]),
            sha256=source_digest,
        )
    except Exception:
        if _path_entry_exists(temp_path):
            temp_path.unlink()
        raise
    _validate_archive_root(
        source_root,
        archive_root,
        require_mount=require_mount,
    )
    target_inspection = _inspect_storage_path(target, (archive_root,))
    if (
        not bool(target_inspection["safe"])
        or not bool(target_inspection["exists"])
        or int(target_inspection["actual_bytes"])
        != int(source_inspection["actual_bytes"])
        or _hash_storage_file(target, target_inspection) != source_digest
    ):
        raise OSError("archive target could not be verified after copy")
    if _storage_identities_alias(target_inspection, source_inspection):
        raise ValueError("archive target aliases the source master")
    return target_inspection, source_digest, created


def _unlink_tracked_file(
    path: Path,
    storage_roots: StorageRoots,
    expected: dict[str, object],
) -> int:
    """Unlink a confirmed regular file without following swapped path links."""

    expected_root = str(expected.get("storage_root") or "")
    roots = _normalized_storage_roots(storage_roots)
    configured_root = next(
        (root for root in roots if str(root) == expected_root),
        None,
    )
    if configured_root is None:
        raise ValueError("tracked file storage root is no longer configured")
    root = configured_root.resolve(strict=True)
    normalized = Path(str(expected["normalized_path"]))
    try:
        relative = normalized.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "tracked file moved outside its configured storage root"
        ) from exc
    if relative == Path(".") or not relative.parts:
        raise ValueError("a configured storage root cannot be deleted")
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


def _normalized_origin_identity(origin: Origin) -> tuple[str, str, str, str]:
    return _normalized_origin_identity_values(
        origin.provider,
        origin.kind,
        origin.external_id,
        options_json=origin.options,
    )


def _normalized_origin_identity_values(
    provider: str,
    kind: str,
    external_id: str,
    *,
    options_json: object = None,
) -> tuple[str, str, str, str]:
    normalized_provider = provider.strip().casefold()
    normalized_kind = kind.strip().casefold()
    normalized_external_id = external_id.strip()
    if normalized_provider == "twitch":
        normalized_external_id = normalized_external_id.casefold()
    variant = ""
    if normalized_provider == "twitch" and normalized_kind == "vods":
        if isinstance(options_json, dict):
            options = options_json
        else:
            options = _json_object(options_json)
        variant = str(options.get("recording_mode") or "vod").strip().casefold()
    return normalized_provider, normalized_kind, normalized_external_id, variant
