from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

import ytb_tg_backup.store as store_module
from ytb_tg_backup.models import MediaCandidate, Origin
from ytb_tg_backup.source_filter import SOURCE_FILTER_STATE_KEY
from ytb_tg_backup.store import LEGACY_SCHEMA, Store, now_iso


def candidate(
    provider: str,
    external_id: str,
    *,
    kind: str = "video",
    metadata: dict[str, object] | None = None,
) -> MediaCandidate:
    return MediaCandidate(
        provider=provider,
        content_kind=kind,
        external_id=external_id,
        title=f"{provider} {external_id}",
        url=f"https://example.invalid/{provider}/{external_id}",
        published_at=None,
        metadata=metadata or {},
    )


class StoreV2Test(unittest.TestCase):
    def _complete_download_for_media(
        self,
        store: Store,
        media_id: int,
        path: Path,
        *,
        destination: str | None = None,
        delivered_at: str | None = None,
    ) -> int:
        path.write_bytes(b"audio")
        job = store.claim_next_job(
            ("download",),
            owner=f"download-{media_id}",
            lease_seconds=60,
        )
        self.assertIsNotNone(job)
        self.assertEqual(job.media_id, media_id)
        artifact_id = store.complete_download(
            job,
            path=path,
            size_bytes=path.stat().st_size,
            delivery_targets=((destination,) if destination else ()),
        )
        if destination is None:
            return artifact_id
        delivery = store.claim_next_job(
            ("telegram_delivery",),
            owner=f"delivery-{media_id}",
            lease_seconds=60,
        )
        self.assertIsNotNone(delivery)
        self.assertEqual(delivery.media_id, media_id)
        store.complete_delivery(
            delivery,
            artifact_id=artifact_id,
            destination_key=destination,
            remote_id=str(media_id),
        )
        if delivered_at is not None:
            store.conn.execute(
                """
                UPDATE deliveries SET delivered_at=?
                WHERE media_id=? AND sink='telegram'
                  AND destination_key=?
                """,
                (delivered_at, media_id, destination),
            )
            store.conn.commit()
        return artifact_id

    def test_panel_snapshot_is_materialized_and_invalidated_by_relevant_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(Origin("yt", "youtube", "uploads", "YT", "UC-1"))
            store.upsert_discovered("yt", candidate("youtube", "first"))

            first = store.get_panel_snapshot("ASMR")
            cached_row = store.conn.execute(
                "SELECT dirty, generated_at FROM panel_snapshots WHERE cache_key='global'"
            ).fetchone()
            self.assertEqual(cached_row["dirty"], 0)
            self.assertEqual(first["providers"], {"youtube": 1})
            self.assertEqual(first["source_filter_pattern"], "ASMR")

            store.set_bot_state("control_panel_v1:chat:user", "{}")
            self.assertEqual(
                store.conn.execute(
                    "SELECT dirty FROM panel_snapshots WHERE cache_key='global'"
                ).fetchone()[0],
                0,
            )
            second = store.get_panel_snapshot("ASMR")
            self.assertEqual(second["generated_at"], first["generated_at"])

            store.upsert_discovered("yt", candidate("youtube", "second"))
            self.assertEqual(
                store.conn.execute(
                    "SELECT dirty FROM panel_snapshots WHERE cache_key='global'"
                ).fetchone()[0],
                1,
            )
            third = store.get_panel_snapshot("ASMR")
            self.assertEqual(third["providers"], {"youtube": 2})
            self.assertEqual(
                store.conn.execute(
                    "SELECT dirty FROM panel_snapshots WHERE cache_key='global'"
                ).fetchone()[0],
                0,
            )

            store.set_bot_state(SOURCE_FILTER_STATE_KEY, "sleep")
            self.assertEqual(
                store.conn.execute(
                    "SELECT dirty FROM panel_snapshots WHERE cache_key='global'"
                ).fetchone()[0],
                1,
            )
            refreshed = store.get_panel_snapshot("sleep")
            self.assertEqual(refreshed["source_filter_pattern"], "sleep")

    def test_control_origin_management_cannot_mutate_config_origins(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(Origin("config-yt", "youtube", "uploads", "Config", "UC-config"))
            twitch = Origin(
                "db:twitch:vods:example",
                "twitch",
                "vods",
                "Example",
                "example",
            )

            self.assertTrue(store.upsert_control_origin(twitch, created_by="123"))
            self.assertFalse(store.upsert_control_origin(twitch, created_by="123"))
            rows = {str(row["id"]): row for row in store.list_origin_statuses()}
            self.assertEqual(rows[twitch.id]["managed_by"], "control")
            self.assertEqual(rows["config-yt"]["managed_by"], "config")

            self.assertTrue(store.set_control_origin_enabled(twitch.id, False))
            self.assertFalse(store.set_control_origin_enabled("config-yt", False))
            self.assertFalse(
                bool(store.conn.execute("SELECT enabled FROM origins WHERE id=?", (twitch.id,)).fetchone()[0])
            )
            self.assertFalse(store.delete_control_origin("config-yt"))
            self.assertTrue(store.delete_control_origin(twitch.id))
            self.assertIsNotNone(store.conn.execute("SELECT 1 FROM origins WHERE id='config-yt'").fetchone())

    def test_control_twitch_recording_mode_preserves_options_and_invalidates_poll_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            twitch = Origin(
                "db:twitch:vods:example",
                "twitch",
                "vods",
                "Example",
                "example",
                options={
                    "created_from": "telegram_panel",
                    "language": "ja",
                    "recording_mode": "vod",
                },
            )
            store.upsert_control_origin(twitch, created_by="123")
            store.record_origin_poll_success(
                twitch.id,
                cursor='{"external_id":"old-vod"}',
            )
            store.get_panel_snapshot(None, force=True)

            self.assertTrue(
                store.set_control_twitch_recording_mode(twitch.id, "live")
            )

            row = store.conn.execute(
                "SELECT options_json FROM origins WHERE id=?",
                (twitch.id,),
            ).fetchone()
            options = json.loads(str(row["options_json"]))
            self.assertEqual(
                options,
                {
                    "created_from": "telegram_panel",
                    "language": "ja",
                    "recording_mode": "live",
                },
            )
            self.assertIsNone(store.get_origin_checkpoint(twitch.id))
            self.assertEqual(
                store.conn.execute(
                    "SELECT dirty FROM panel_snapshots WHERE cache_key='global'"
                ).fetchone()["dirty"],
                1,
            )
            snapshot = store.get_panel_snapshot(None)
            twitch_snapshot = next(
                origin
                for origin in snapshot["origins"]
                if origin["id"] == twitch.id
            )
            self.assertEqual(twitch_snapshot["recording_mode"], "live")

            config_twitch = Origin(
                "config-twitch",
                "twitch",
                "vods",
                "Config Twitch",
                "config",
            )
            highlight = Origin(
                "db:twitch:highlights:example",
                "twitch",
                "highlights",
                "Highlights",
                "example",
            )
            store.upsert_origin(config_twitch)
            store.upsert_control_origin(highlight, created_by="123")
            self.assertFalse(
                store.set_control_twitch_recording_mode(
                    config_twitch.id,
                    "live",
                )
            )
            self.assertFalse(
                store.set_control_twitch_recording_mode(
                    highlight.id,
                    "live",
                )
            )
            with self.assertRaisesRegex(ValueError, "recording_mode"):
                store.set_control_twitch_recording_mode(twitch.id, "archive")
            store.close()

    def test_provider_namespace_and_many_origin_relationships(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            for origin in (
                Origin("yt-a", "youtube", "uploads", "YT A", "UC-a"),
                Origin("yt-b", "youtube", "uploads", "YT B", "UC-b"),
                Origin("tw-a", "twitch", "vods", "TW A", "42"),
            ):
                store.upsert_origin(origin)

            youtube_id, _ = store.upsert_discovered("yt-a", candidate("youtube", "123"))
            same_youtube_id, _ = store.upsert_discovered("yt-b", candidate("youtube", "123"))
            twitch_id, _ = store.upsert_discovered("tw-a", candidate("twitch", "123", kind="vod"))

            self.assertEqual(youtube_id, same_youtube_id)
            self.assertNotEqual(youtube_id, twitch_id)
            self.assertEqual(
                store.conn.execute("SELECT COUNT(*) FROM origin_items WHERE media_id=?", (youtube_id,)).fetchone()[0],
                2,
            )
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM jobs WHERE job_type='download'").fetchone()[0], 2)

    def test_eligible_origin_creates_job_even_when_another_origin_ignored_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(Origin("ignored", "youtube", "uploads", "Ignored", "UC-1"))
            store.upsert_origin(Origin("eligible", "youtube", "uploads", "Eligible", "UC-2"))
            media_id, _ = store.upsert_discovered(
                "ignored",
                candidate("youtube", "abc"),
                disposition="ignored",
                decision_code="source_filter",
            )
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

            same_id, _ = store.upsert_discovered("eligible", candidate("youtube", "abc"))
            self.assertEqual(same_id, media_id)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_source_filter_cancelled_job_reactivates_when_item_becomes_eligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(Origin("yt", "youtube", "uploads", "YT", "UC-1"))
            media_id, _ = store.upsert_discovered("yt", candidate("youtube", "abc"))
            job = store.claim_next_job(("download",), owner="worker", lease_seconds=60)
            store.cancel_job(
                job,
                reason_code="source_filter",
                error="source filter ignored: /ASMR/i",
            )

            store.upsert_discovered("yt", candidate("youtube", "abc"), disposition="eligible")

            row = store.conn.execute(
                "SELECT state, failure_count, reason_code FROM jobs WHERE media_id=?",
                (media_id,),
            ).fetchone()
            self.assertEqual(tuple(row), ("queued", 0, None))
            self.assertIsNotNone(
                store.claim_next_job(("download",), owner="reactivated", lease_seconds=60)
            )

    def test_initial_seed_survives_filter_changes_until_explicit_backfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(Origin("yt", "youtube", "uploads", "YT", "UC-1"))
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "old"),
                disposition="ignored",
                decision_code="initial_seed",
                decision_reason="initial feed seed ignored",
            )

            store.upsert_discovered(
                "yt",
                candidate("youtube", "old"),
                disposition="ignored",
                decision_code="source_filter",
                decision_reason="source filter ignored",
            )
            store.upsert_discovered("yt", candidate("youtube", "old"))
            row = store.conn.execute(
                "SELECT disposition, decision_code FROM origin_items WHERE media_id=?",
                (media_id,),
            ).fetchone()
            self.assertEqual(tuple(row), ("ignored", "initial_seed"))
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

            store.upsert_discovered(
                "yt",
                candidate("youtube", "old"),
                disposition="eligible",
                decision_code="bootstrap_all",
                decision_reason="explicit backfill",
            )
            row = store.conn.execute(
                "SELECT disposition, decision_code FROM origin_items WHERE media_id=?",
                (media_id,),
            ).fetchone()
            self.assertEqual(tuple(row), ("eligible", "bootstrap_all"))
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_changing_origin_bootstrap_to_all_reactivates_seed_and_resets_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(Origin("tw", "twitch", "vods", "TW", "100"))
            media_id, _ = store.upsert_discovered(
                "tw",
                candidate("twitch", "old", kind="vod"),
                disposition="ignored",
                decision_code="initial_seed",
                decision_reason="initial feed seed ignored",
            )
            store.record_origin_poll_success("tw", cursor='{"external_id":"newest"}')

            store.upsert_origin(
                Origin("tw", "twitch", "vods", "TW", "100", bootstrap="all"),
                max_failures=3,
            )

            row = store.conn.execute(
                "SELECT disposition, decision_code FROM origin_items WHERE media_id=?",
                (media_id,),
            ).fetchone()
            job = store.conn.execute(
                "SELECT state, max_failures FROM jobs WHERE media_id=? AND job_type='download'",
                (media_id,),
            ).fetchone()
            self.assertEqual(tuple(row), ("eligible", "bootstrap_all"))
            self.assertEqual(tuple(job), ("queued", 3))
            self.assertIsNone(store.get_origin_checkpoint("tw"))

            store.record_origin_poll_success("tw", cursor='{"external_id":"after-backfill"}')
            store.upsert_origin(
                Origin("tw", "twitch", "vods", "TW", "100", bootstrap="all"),
                max_failures=3,
            )
            self.assertEqual(store.get_origin_checkpoint("tw"), '{"external_id":"after-backfill"}')

    def test_claim_is_exclusive_and_expired_download_lease_is_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            first = Store(db_path)
            first.initialize()
            first.upsert_origin(Origin("yt", "youtube", "uploads", "YT", "UC-1"))
            first.upsert_discovered("yt", candidate("youtube", "abc"))
            second = Store(db_path)
            second.initialize()

            job = first.claim_next_job(("download",), owner="first", lease_seconds=60)
            self.assertIsNotNone(job)
            self.assertIsNone(second.claim_next_job(("download",), owner="second", lease_seconds=60))

            expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            first.conn.execute("UPDATE jobs SET lease_until=? WHERE id=?", (expired, job.id))
            first.conn.commit()
            reclaimed = second.claim_next_job(("download",), owner="second", lease_seconds=60)
            self.assertIsNotNone(reclaimed)
            self.assertEqual(reclaimed.id, job.id)
            self.assertNotEqual(reclaimed.lease_token, job.lease_token)
            with self.assertRaises(RuntimeError):
                first.complete_download(job, path=Path(tmp) / "stale.m4a", size_bytes=1)

    def test_defer_does_not_consume_failure_budget_and_failure_blocks_at_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(Origin("yt", "youtube", "uploads", "YT", "UC-1"))
            media_id, _ = store.upsert_discovered("yt", candidate("youtube", "abc"), max_failures=2)

            job = store.claim_next_job(("download",), owner="worker", lease_seconds=60)
            store.defer_job(job, reason_code="not_ready", error="live", retry_seconds=0)
            row = store.conn.execute("SELECT state, failure_count FROM jobs WHERE media_id=?", (media_id,)).fetchone()
            self.assertEqual(tuple(row), ("retry", 0))

            job = store.claim_next_job(("download",), owner="worker", lease_seconds=60)
            store.fail_job(job, reason_code="probe_failed", error="one", retry_seconds=0)
            row = store.conn.execute("SELECT state, failure_count FROM jobs WHERE media_id=?", (media_id,)).fetchone()
            self.assertEqual(tuple(row), ("retry", 1))

            job = store.claim_next_job(("download",), owner="worker", lease_seconds=60)
            store.fail_job(job, reason_code="probe_failed", error="two", retry_seconds=0)
            row = store.conn.execute("SELECT state, failure_count FROM jobs WHERE media_id=?", (media_id,)).fetchone()
            self.assertEqual(tuple(row), ("blocked", 2))

            store.requeue_download(media_id, max_failures=2, reason="artifact was lost")
            row = store.conn.execute("SELECT state, failure_count FROM jobs WHERE media_id=?", (media_id,)).fetchone()
            self.assertEqual(tuple(row), ("retry", 0))
            self.assertIsNotNone(store.claim_next_job(("download",), owner="recovery", lease_seconds=60))

    def test_expired_delivery_becomes_uncertain_instead_of_resending(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(Origin("yt", "youtube", "uploads", "YT", "UC-1"))
            media_id, _ = store.upsert_discovered("yt", candidate("youtube", "abc"))
            download = store.claim_next_job(("download",), owner="worker", lease_seconds=60)
            path = Path(tmp) / "abc.m4a"
            path.write_bytes(b"audio")
            store.complete_download(download, path=path, size_bytes=5)
            store.ensure_delivery_job(media_id, "telegram:@archive")
            delivery = store.claim_next_job(("telegram_delivery",), owner="worker", lease_seconds=60)
            store.mark_delivery_sending(delivery)
            expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            store.conn.execute("UPDATE jobs SET lease_until=? WHERE id=?", (expired, delivery.id))
            store.conn.commit()

            store.recover_stale_jobs()
            row = store.conn.execute("SELECT state, reason_code FROM jobs WHERE id=?", (delivery.id,)).fetchone()
            self.assertEqual(tuple(row), ("uncertain", "delivery_uncertain"))
            self.assertIsNone(store.claim_next_job(("telegram_delivery",), owner="other", lease_seconds=60))

    def test_operator_can_confirm_uncertain_delivery_with_cas_and_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(Origin("yt", "youtube", "uploads", "YT", "UC-1"))
            media_id, _ = store.upsert_discovered("yt", candidate("youtube", "abc"))
            download = store.claim_next_job(("download",), owner="worker", lease_seconds=60)
            path = Path(tmp) / "abc.m4a"
            path.write_bytes(b"audio")
            artifact_id = store.complete_download(download, path=path, size_bytes=5)
            store.ensure_delivery_job(media_id, "telegram:@archive")
            delivery = store.claim_next_job(
                ("telegram_delivery",), owner="worker", lease_seconds=60
            )
            store.mark_job_uncertain(delivery, error="response boundary lost")

            recovery = store.get_uncertain_delivery(media_id)
            self.assertIsNotNone(recovery)
            stale_revision = str(recovery["revision"])
            store.conn.execute(
                "UPDATE jobs SET updated_at=? WHERE id=?",
                ("2099-01-01T00:00:00+00:00", delivery.id),
            )
            store.conn.commit()
            with self.assertRaisesRegex(ValueError, "changed after confirmation"):
                store.resolve_uncertain_delivery(
                    delivery.id,
                    expected_revision=stale_revision,
                    action="confirm_delivered",
                    actor="user=1 chat=2 thread=",
                )

            recovery = store.get_uncertain_delivery(media_id)
            result = store.resolve_uncertain_delivery(
                delivery.id,
                expected_revision=str(recovery["revision"]),
                action="confirm_delivered",
                actor="user=1 chat=2 thread=",
            )

            self.assertEqual(result["action"], "confirm_delivered")
            job_row = store.conn.execute(
                "SELECT state, reason_code, last_error FROM jobs WHERE id=?",
                (delivery.id,),
            ).fetchone()
            self.assertEqual(tuple(job_row), ("succeeded", None, None))
            delivery_row = store.conn.execute(
                """
                SELECT artifact_id, remote_id FROM deliveries
                WHERE media_id=? AND destination_key='telegram:@archive'
                """,
                (media_id,),
            ).fetchone()
            self.assertEqual(delivery_row["artifact_id"], artifact_id)
            self.assertTrue(str(delivery_row["remote_id"]).startswith("operator-confirmed:"))
            audit_row = store.conn.execute(
                """
                SELECT action, actor, previous_error, remote_id
                FROM delivery_recovery_events WHERE job_id=?
                """,
                (delivery.id,),
            ).fetchone()
            self.assertEqual(audit_row["action"], "confirm_delivered")
            self.assertEqual(audit_row["actor"], "user=1 chat=2 thread=")
            self.assertEqual(audit_row["previous_error"], "response boundary lost")
            self.assertEqual(audit_row["remote_id"], delivery_row["remote_id"])
            delivered_at = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            store.conn.execute(
                "UPDATE deliveries SET delivered_at=? WHERE media_id=?",
                (delivered_at, media_id),
            )
            store.conn.commit()
            candidates = store.list_delivery_retention_candidates(
                (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            )
            self.assertEqual(
                [int(item["artifact_id"]) for item in candidates],
                [artifact_id],
            )

    def test_operator_can_force_retry_uncertain_delivery_with_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(Origin("yt", "youtube", "uploads", "YT", "UC-1"))
            media_id, _ = store.upsert_discovered("yt", candidate("youtube", "abc"))
            store.ensure_delivery_job(media_id, "telegram:@archive")
            delivery = store.claim_next_job(
                ("telegram_delivery",), owner="worker", lease_seconds=60
            )
            store.mark_job_uncertain(delivery, error="unknown result")
            recovery = store.get_uncertain_delivery(media_id)

            store.resolve_uncertain_delivery(
                delivery.id,
                expected_revision=str(recovery["revision"]),
                action="force_retry",
                actor="user=7 chat=8 thread=9",
            )

            row = store.conn.execute(
                "SELECT state, reason_code, finished_at FROM jobs WHERE id=?",
                (delivery.id,),
            ).fetchone()
            self.assertEqual(tuple(row), ("retry", "operator_retry", None))
            reclaimed = store.claim_next_job(
                ("telegram_delivery",), owner="retry-worker", lease_seconds=60
            )
            self.assertIsNotNone(reclaimed)
            self.assertEqual(reclaimed.id, delivery.id)
            audit_row = store.conn.execute(
                "SELECT action, actor, remote_id FROM delivery_recovery_events WHERE job_id=?",
                (delivery.id,),
            ).fetchone()
            self.assertEqual(
                tuple(audit_row),
                ("force_retry", "user=7 chat=8 thread=9", None),
            )

    def test_expired_delivery_preparation_is_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(Origin("yt", "youtube", "uploads", "YT", "UC-1"))
            media_id, _ = store.upsert_discovered("yt", candidate("youtube", "abc"))
            store.ensure_delivery_job(media_id, "telegram:@archive")
            delivery = store.claim_next_job(("telegram_delivery",), owner="worker", lease_seconds=60)
            expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            store.conn.execute("UPDATE jobs SET lease_until=? WHERE id=?", (expired, delivery.id))
            store.conn.commit()

            store.recover_stale_jobs()

            row = store.conn.execute("SELECT state, reason_code FROM jobs WHERE id=?", (delivery.id,)).fetchone()
            self.assertEqual(tuple(row), ("retry", "worker_recovered"))
            self.assertIsNotNone(store.claim_next_job(("telegram_delivery",), owner="other", lease_seconds=60))

    def test_delivery_retention_candidates_require_cutoff_and_terminal_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            now = datetime.now(timezone.utc)
            delivered_before = (now - timedelta(hours=48)).isoformat()
            old_delivery = (now - timedelta(hours=72)).isoformat()
            recent_delivery = (now - timedelta(hours=1)).isoformat()

            old_media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "retention-old"),
            )
            old_artifact_id = self._complete_download_for_media(
                store,
                old_media_id,
                root / "retention-old.m4a",
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            recent_media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "retention-recent"),
            )
            self._complete_download_for_media(
                store,
                recent_media_id,
                root / "retention-recent.m4a",
                destination="telegram:@archive",
                delivered_at=recent_delivery,
            )
            undelivered_media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "retention-undelivered"),
            )
            self._complete_download_for_media(
                store,
                undelivered_media_id,
                root / "retention-undelivered.m4a",
            )

            candidates = store.list_delivery_retention_candidates(
                delivered_before
            )
            self.assertEqual(
                candidates,
                [
                    {
                        "group_key": f"media:{old_media_id}",
                        "artifact_id": old_artifact_id,
                        "media_id": old_media_id,
                        "artifact_state": "ready",
                        "delivered_at": old_delivery,
                    }
                ],
            )

            extra_job_id = store.ensure_delivery_job(
                old_media_id,
                "telegram:@extra",
            )
            for state in ("queued", "retry", "running", "blocked", "uncertain"):
                store.conn.execute(
                    "UPDATE jobs SET state=? WHERE id=?",
                    (state, extra_job_id),
                )
                store.conn.commit()
                self.assertEqual(
                    store.list_delivery_retention_candidates(
                        delivered_before
                    ),
                    [],
                    state,
                )

            store.conn.execute(
                "UPDATE jobs SET state='succeeded' WHERE id=?",
                (extra_job_id,),
            )
            store.conn.execute(
                """
                INSERT INTO deliveries(
                  media_id, artifact_id, sink, destination_key,
                  remote_id, delivered_at
                ) VALUES (?, ?, 'telegram', 'telegram:@extra', 'new', ?)
                """,
                (old_media_id, old_artifact_id, recent_delivery),
            )
            store.conn.commit()
            self.assertEqual(
                store.list_delivery_retention_candidates(delivered_before),
                [],
            )

            store.conn.execute(
                """
                UPDATE deliveries SET delivered_at=?
                WHERE media_id=? AND destination_key='telegram:@extra'
                """,
                (old_delivery, old_media_id),
            )
            store.conn.commit()
            self.assertEqual(
                store.list_delivery_retention_candidates(
                    delivered_before,
                    limit=1,
                )[0]["artifact_id"],
                old_artifact_id,
            )

    def test_master_archive_moves_verified_file_and_keeps_multi_root_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            download_root = Path(tmp) / "downloads"
            archive_root = Path(tmp) / "mounted-archive"
            download_root.mkdir()
            archive_root.mkdir()
            roots = (download_root, archive_root)
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "archive-master"),
            )
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).isoformat()
            master_path = download_root / "youtube" / "archive-master.m4a"
            master_path.parent.mkdir()
            master_id = self._complete_download_for_media(
                store,
                media_id,
                master_path,
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            segment_path = download_root / "archive-master.segment.ts"
            segment_path.write_bytes(b"segment")
            store.record_live_segment(
                media_id,
                path=segment_path,
                size_bytes=segment_path.stat().st_size,
            )

            self.assertEqual(
                store.list_master_archive_candidates(cutoff)[0][
                    "artifact_id"
                ],
                master_id,
            )
            detail = store.get_disk_resource(master_id, roots)
            archived = store.archive_master(
                master_id,
                download_root,
                archive_root,
                expected_revision=str(detail["resource_revision"]),
                delivered_before=cutoff,
                require_mount=False,
            )

            archived_path = archive_root / "youtube" / "archive-master.m4a"
            self.assertTrue(archived["completed"])
            self.assertTrue(archived["source_deleted"])
            self.assertFalse(master_path.exists())
            self.assertEqual(archived_path.read_bytes(), b"audio")
            master = store.get_artifact(media_id)
            self.assertEqual(Path(str(master["path"])), archived_path)
            archive_metadata = json.loads(str(master["metadata_json"]))[
                "archive"
            ]
            self.assertEqual(
                archive_metadata["backend"],
                "mounted_filesystem",
            )
            self.assertEqual(
                archive_metadata["verified_at"],
                archive_metadata["archived_at"],
            )
            self.assertFalse(archive_metadata["source_cleanup_pending"])
            self.assertEqual(len(archive_metadata["sha256"]), 64)
            self.assertEqual(
                store.conn.execute(
                    "SELECT COUNT(*) FROM artifact_archive_moves"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(store.list_master_archive_candidates(cutoff), [])

            process_detail = store.get_disk_resource(master_id, roots)
            process_result = store.purge_process_artifacts(
                master_id,
                roots,
                expected_revision=str(process_detail["resource_revision"]),
                delivered_before=cutoff,
            )
            self.assertTrue(process_result["completed"])
            self.assertTrue(archived_path.exists())
            self.assertFalse(segment_path.exists())

            purge_detail = store.get_disk_resource(master_id, roots)
            purge_result = store.purge_disk_resource(
                master_id,
                roots,
                expected_revision=str(purge_detail["resource_revision"]),
            )
            self.assertTrue(purge_result["completed"])
            self.assertFalse(archived_path.exists())

    def test_master_archive_requires_mount_and_preserves_target_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            download_root = Path(tmp) / "downloads"
            archive_root = Path(tmp) / "ordinary-directory"
            download_root.mkdir()
            archive_root.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "archive-guard"),
            )
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).isoformat()
            master_path = download_root / "archive-guard.m4a"
            master_id = self._complete_download_for_media(
                store,
                media_id,
                master_path,
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            roots = (download_root, archive_root)
            detail = store.get_disk_resource(master_id, roots)

            with (
                mock.patch.object(
                    store_module,
                    "_is_on_nonroot_mount",
                    return_value=False,
                ),
                self.assertRaisesRegex(ValueError, "on a non-root mount"),
            ):
                store.archive_master(
                    master_id,
                    download_root,
                    archive_root,
                    expected_revision=str(detail["resource_revision"]),
                    delivered_before=cutoff,
                )
            self.assertTrue(master_path.exists())
            self.assertEqual(store.get_artifact(media_id)["path"], str(master_path))

            collision = archive_root / "archive-guard.m4a"
            collision.write_bytes(b"different archive")
            with self.assertRaisesRegex(ValueError, "different content"):
                store.archive_master(
                    master_id,
                    download_root,
                    archive_root,
                    expected_revision=str(detail["resource_revision"]),
                    delivered_before=cutoff,
                    require_mount=False,
                )
            self.assertEqual(collision.read_bytes(), b"different archive")
            self.assertTrue(master_path.exists())
            self.assertEqual(store.get_artifact(media_id)["path"], str(master_path))
            self.assertEqual(
                store.conn.execute(
                    "SELECT state FROM artifact_archive_moves WHERE artifact_id=?",
                    (master_id,),
                ).fetchone()[0],
                "retry",
            )

            with self.assertRaisesRegex(ValueError, "archive directory"):
                store.purge_disk_resource(
                    master_id,
                    roots,
                    expected_revision=str(detail["resource_revision"]),
                )
            self.assertTrue(master_path.exists())
            self.assertEqual(collision.read_bytes(), b"different archive")

    def test_master_archive_rejects_target_that_aliases_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            download_root = Path(tmp) / "downloads"
            archive_root = Path(tmp) / "archive"
            download_root.mkdir()
            archive_root.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "archive-alias"),
            )
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).isoformat()
            master_path = download_root / "archive-alias.m4a"
            master_id = self._complete_download_for_media(
                store,
                media_id,
                master_path,
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            target = archive_root / master_path.name
            target.hardlink_to(master_path)
            detail = store.get_disk_resource(
                master_id,
                (download_root, archive_root),
            )

            with self.assertRaisesRegex(ValueError, "aliases the source"):
                store.archive_master(
                    master_id,
                    download_root,
                    archive_root,
                    expected_revision=str(detail["resource_revision"]),
                    delivered_before=cutoff,
                    require_mount=False,
                )

            self.assertTrue(master_path.exists())
            self.assertTrue(target.exists())
            self.assertEqual(store.get_artifact(media_id)["path"], str(master_path))

    def test_master_archive_retries_source_cleanup_after_interruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            download_root = Path(tmp) / "downloads"
            archive_root = Path(tmp) / "archive"
            download_root.mkdir()
            archive_root.mkdir()
            roots = (download_root, archive_root)
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "archive-cleanup"),
            )
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).isoformat()
            master_path = download_root / "archive-cleanup.m4a"
            master_id = self._complete_download_for_media(
                store,
                media_id,
                master_path,
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            detail = store.get_disk_resource(master_id, roots)
            with mock.patch.object(
                store,
                "cleanup_archived_master_source",
                return_value={
                    "completed": False,
                    "source_deleted": False,
                    "errors": ["simulated interruption"],
                },
            ):
                archived = store.archive_master(
                    master_id,
                    download_root,
                    archive_root,
                    expected_revision=str(detail["resource_revision"]),
                    delivered_before=cutoff,
                    require_mount=False,
                )
            self.assertTrue(archived["archived"])
            self.assertFalse(archived["completed"])
            self.assertTrue(master_path.exists())
            self.assertEqual(
                store.list_archive_source_cleanup_candidates(),
                [master_id],
            )

            cleanup = Store.cleanup_archived_master_source(
                store,
                master_id,
                roots,
            )
            self.assertTrue(cleanup["completed"])
            self.assertTrue(cleanup["source_deleted"])
            self.assertFalse(master_path.exists())
            self.assertEqual(store.list_archive_source_cleanup_candidates(), [])

    def test_master_archive_phase_two_rechecks_job_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            download_root = Path(tmp) / "downloads"
            archive_root = Path(tmp) / "archive"
            download_root.mkdir()
            archive_root.mkdir()
            roots = (download_root, archive_root)
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "archive-job-race"),
            )
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).isoformat()
            master_path = download_root / "archive-job-race.m4a"
            master_id = self._complete_download_for_media(
                store,
                media_id,
                master_path,
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            detail = store.get_disk_resource(master_id, roots)
            original_copy = store_module._copy_file_to_archive

            def add_job_after_copy(*args, **kwargs):
                copied = original_copy(*args, **kwargs)
                store.ensure_delivery_job(media_id, "telegram:@new")
                return copied

            with mock.patch.object(
                store_module,
                "_copy_file_to_archive",
                side_effect=add_job_after_copy,
            ):
                result = store.archive_master(
                    master_id,
                    download_root,
                    archive_root,
                    expected_revision=str(detail["resource_revision"]),
                    delivered_before=cutoff,
                    require_mount=False,
                )

            self.assertTrue(result["skipped"])
            self.assertFalse(result["archived"])
            self.assertTrue(master_path.exists())
            self.assertEqual(store.get_artifact(media_id)["path"], str(master_path))
            self.assertEqual(
                store.conn.execute(
                    "SELECT state FROM artifact_archive_moves WHERE artifact_id=?",
                    (master_id,),
                ).fetchone()[0],
                "retry",
            )

            store.conn.execute(
                """
                UPDATE jobs SET state='cancelled'
                WHERE media_id=? AND job_type='telegram_delivery'
                  AND target_key='telegram:@new'
                """,
                (media_id,),
            )
            store.conn.commit()
            retry_detail = store.get_disk_resource(master_id, roots)
            retried = store.archive_master(
                master_id,
                download_root,
                archive_root,
                expected_revision=str(retry_detail["resource_revision"]),
                delivered_before=cutoff,
                require_mount=False,
            )
            self.assertTrue(retried["completed"])
            self.assertTrue(retried["source_deleted"])
            self.assertFalse(master_path.exists())
            self.assertEqual(Path(str(retried["path"])).read_bytes(), b"audio")

    def test_master_archive_recovers_dead_copy_claim_as_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            download_root = Path(tmp) / "downloads"
            archive_root = Path(tmp) / "archive"
            download_root.mkdir()
            archive_root.mkdir()
            db_path = Path(tmp) / "state.db"
            store = Store(db_path)
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "archive-recovery"),
            )
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).isoformat()
            master_path = download_root / "archive-recovery.m4a"
            master_id = self._complete_download_for_media(
                store,
                media_id,
                master_path,
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            detail = store.get_disk_resource(master_id, download_root)

            with (
                mock.patch.object(
                    store_module,
                    "_copy_file_to_archive",
                    side_effect=SystemExit("simulated process death"),
                ),
                self.assertRaises(SystemExit),
            ):
                store.archive_master(
                    master_id,
                    download_root,
                    archive_root,
                    expected_revision=str(detail["resource_revision"]),
                    delivered_before=cutoff,
                    require_mount=False,
                )
            self.assertEqual(
                store.conn.execute(
                    "SELECT state FROM artifact_archive_moves WHERE artifact_id=?",
                    (master_id,),
                ).fetchone()[0],
                "copying",
            )
            store.close()

            recovered = Store(db_path)
            with mock.patch.object(
                store_module,
                "_process_instance_is_alive",
                return_value=False,
            ):
                recovered.initialize()
            move = recovered.conn.execute(
                """
                SELECT state, owner_pid, owner_start_id
                FROM artifact_archive_moves WHERE artifact_id=?
                """,
                (master_id,),
            ).fetchone()
            self.assertEqual(tuple(move), ("retry", 0, ""))
            candidate_row = recovered.list_master_archive_candidates(cutoff)[0]
            self.assertEqual(candidate_row["artifact_id"], master_id)
            self.assertEqual(candidate_row["move_state"], "retry")
            self.assertTrue(master_path.exists())

    def test_master_archive_cleanup_keeps_changed_source_or_target(self):
        for scenario in ("source-reused", "target-changed"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                download_root = Path(tmp) / "downloads"
                archive_root = Path(tmp) / "archive"
                download_root.mkdir()
                archive_root.mkdir()
                roots = (download_root, archive_root)
                store = Store(Path(tmp) / "state.db")
                store.initialize()
                store.upsert_origin(
                    Origin("yt", "youtube", "uploads", "YT", "UC-1")
                )
                media_id, _ = store.upsert_discovered(
                    "yt",
                    candidate("youtube", f"archive-{scenario}"),
                )
                old_delivery = (
                    datetime.now(timezone.utc) - timedelta(hours=48)
                ).isoformat()
                cutoff = (
                    datetime.now(timezone.utc) - timedelta(hours=24)
                ).isoformat()
                master_path = download_root / f"archive-{scenario}.m4a"
                master_id = self._complete_download_for_media(
                    store,
                    media_id,
                    master_path,
                    destination="telegram:@archive",
                    delivered_at=old_delivery,
                )
                detail = store.get_disk_resource(master_id, roots)
                with mock.patch.object(
                    store,
                    "cleanup_archived_master_source",
                    return_value={
                        "completed": False,
                        "source_deleted": False,
                        "errors": ["simulated interruption"],
                    },
                ):
                    archived = store.archive_master(
                        master_id,
                        download_root,
                        archive_root,
                        expected_revision=str(detail["resource_revision"]),
                        delivered_before=cutoff,
                        require_mount=False,
                    )
                archived_path = Path(str(archived["path"]))

                if scenario == "source-reused":
                    master_path.unlink()
                    master_path.write_bytes(b"unknown replacement")
                else:
                    archived_path.write_bytes(b"corrupt archive target")
                cleanup = Store.cleanup_archived_master_source(
                    store,
                    master_id,
                    roots,
                )

                self.assertFalse(cleanup["completed"])
                self.assertFalse(cleanup["source_deleted"])
                self.assertTrue(master_path.exists())
                if scenario == "source-reused":
                    self.assertEqual(
                        master_path.read_bytes(),
                        b"unknown replacement",
                    )
                    expected_state = "orphaned"
                else:
                    self.assertEqual(master_path.read_bytes(), b"audio")
                    expected_state = "source_cleanup"
                self.assertEqual(
                    store.conn.execute(
                        """
                        SELECT state FROM artifact_archive_moves
                        WHERE artifact_id=?
                        """,
                        (master_id,),
                    ).fetchone()[0],
                    expected_state,
                )

    def test_master_archive_cleanup_keeps_source_after_mount_is_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            download_root = Path(tmp) / "downloads"
            archive_root = Path(tmp) / "archive"
            download_root.mkdir()
            archive_root.mkdir()
            roots = (download_root, archive_root)
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "archive-mount-lost"),
            )
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).isoformat()
            master_path = download_root / "archive-mount-lost.m4a"
            master_id = self._complete_download_for_media(
                store,
                media_id,
                master_path,
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            detail = store.get_disk_resource(master_id, roots)
            with (
                mock.patch.object(
                    store_module,
                    "_is_on_nonroot_mount",
                    return_value=True,
                ),
                mock.patch.object(
                    store,
                    "cleanup_archived_master_source",
                    return_value={
                        "completed": False,
                        "source_deleted": False,
                        "errors": ["simulated interruption"],
                    },
                ),
            ):
                archived = store.archive_master(
                    master_id,
                    download_root,
                    archive_root,
                    expected_revision=str(detail["resource_revision"]),
                    delivered_before=cutoff,
                )
            self.assertTrue(archived["archived"])
            with mock.patch.object(
                store_module,
                "_is_on_nonroot_mount",
                return_value=False,
            ):
                cleanup = Store.cleanup_archived_master_source(
                    store,
                    master_id,
                    roots,
                )

            self.assertFalse(cleanup["completed"])
            self.assertFalse(cleanup["source_deleted"])
            self.assertTrue(master_path.exists())
            self.assertIn("not on a non-root mount", cleanup["errors"][0])
            move = store.conn.execute(
                """
                SELECT state, error FROM artifact_archive_moves
                WHERE artifact_id=?
                """,
                (master_id,),
            ).fetchone()
            self.assertEqual(move["state"], "source_cleanup")
            self.assertIn("not on a non-root mount", move["error"])

    def test_archive_directory_may_be_below_a_nonroot_mount(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount_root = Path(tmp) / "remote-mount"
            archive_root = mount_root / "asmr-data"
            archive_root.mkdir(parents=True)
            mountinfo = (
                f"1 0 0:1 / {mount_root} rw - fuse.remote remote rw\n"
            )
            with mock.patch.object(
                store_module.Path,
                "read_text",
                return_value=mountinfo,
            ):
                self.assertTrue(
                    store_module._is_on_nonroot_mount(archive_root)
                )

    def test_process_retention_candidates_are_media_local_and_split_delivery_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("tw", "twitch", "vods", "TW", "streamer")
            )
            now = datetime.now(timezone.utc)
            cutoff = (now - timedelta(hours=24)).isoformat()
            old_delivery = (now - timedelta(hours=48)).isoformat()
            recent_delivery = (now - timedelta(hours=1)).isoformat()
            stream_id = "process-own-delivery"

            delivered_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "process-delivered",
                    kind="live_stream",
                    metadata={"stream_id": stream_id},
                ),
            )
            delivered_artifact_id = self._complete_download_for_media(
                store,
                delivered_media_id,
                root / "process-delivered.m4a",
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            delivered_segment = root / "process-delivered.segment.ts"
            delivered_segment.write_bytes(b"segment")
            store.record_live_segment(
                delivered_media_id,
                path=delivered_segment,
                size_bytes=delivered_segment.stat().st_size,
            )

            sibling_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "process-undelivered-sibling",
                    kind="vod",
                    metadata={"stream_id": stream_id},
                ),
            )
            self._complete_download_for_media(
                store,
                sibling_media_id,
                root / "process-undelivered-sibling.m4a",
            )
            sibling_upload = root / "process-undelivered-sibling.tg.m4a"
            sibling_upload.write_bytes(b"upload")
            store.record_artifact(
                sibling_media_id,
                role="telegram_upload",
                path=sibling_upload,
                size_bytes=sibling_upload.stat().st_size,
            )

            recent_media_id, _ = store.upsert_discovered(
                "tw",
                candidate("twitch", "process-recent"),
            )
            self._complete_download_for_media(
                store,
                recent_media_id,
                root / "process-recent.m4a",
                destination="telegram:@archive",
                delivered_at=recent_delivery,
            )
            recent_upload = root / "process-recent.tg.m4a"
            recent_upload.write_bytes(b"recent")
            store.record_artifact(
                recent_media_id,
                role="telegram_upload",
                path=recent_upload,
                size_bytes=recent_upload.stat().st_size,
            )

            self.assertEqual(
                store.list_process_retention_candidates(cutoff),
                [
                    {
                        "group_key": f"media:{delivered_media_id}",
                        "artifact_id": delivered_artifact_id,
                        "media_id": delivered_media_id,
                        "artifact_state": "ready",
                        "delivered_at": old_delivery,
                    }
                ],
            )

            extra_job_id = store.ensure_delivery_job(
                delivered_media_id,
                "telegram:@extra",
            )
            for state in ("queued", "retry", "running", "blocked", "uncertain"):
                store.conn.execute(
                    "UPDATE jobs SET state=? WHERE id=?",
                    (state, extra_job_id),
                )
                store.conn.commit()
                candidates = store.list_process_retention_candidates(cutoff)
                if state == "running":
                    self.assertEqual(candidates, [], state)
                else:
                    self.assertEqual(
                        candidates[0]["artifact_id"],
                        delivered_artifact_id,
                        state,
                    )
            store.conn.execute(
                "UPDATE jobs SET state='cancelled' WHERE id=?",
                (extra_job_id,),
            )
            store.conn.commit()
            self.assertEqual(
                store.list_process_retention_candidates(cutoff)[0][
                    "artifact_id"
                ],
                delivered_artifact_id,
            )

    def test_process_retention_purges_merged_live_segment_before_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("tw", "twitch", "vods", "TW", "streamer")
            )
            media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "process-blocked-delivery",
                    kind="live_stream",
                    metadata={"stream_id": "process-blocked-delivery"},
                ),
            )
            master_path = root / "process-blocked-delivery.mp4"
            master_id = self._complete_download_for_media(
                store,
                media_id,
                master_path,
            )
            old_merge = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).isoformat()
            store.conn.execute(
                "UPDATE artifacts SET created_at=?, updated_at=? WHERE id=?",
                (old_merge, old_merge, master_id),
            )
            delivery_job_id = store.ensure_delivery_job(
                media_id,
                "telegram:@archive",
            )
            store.conn.execute(
                """
                UPDATE jobs SET state='blocked', reason_code='upload_too_large',
                  last_error='video master exceeds transport limit'
                WHERE id=?
                """,
                (delivery_job_id,),
            )
            store.conn.commit()
            segment_path = root / "process-blocked-delivery.segment.ts"
            segment_path.write_bytes(b"segment")
            segment_part = store.record_live_segment(
                media_id,
                path=segment_path,
                size_bytes=segment_path.stat().st_size,
            )
            upload_path = root / "process-blocked-delivery.tgaudio.m4a"
            upload_path.write_bytes(b"pending upload")
            upload_id = store.record_artifact(
                media_id,
                role="telegram_upload",
                path=upload_path,
                size_bytes=upload_path.stat().st_size,
            )
            store.conn.commit()
            segment_id = int(
                store.conn.execute(
                    """
                    SELECT id FROM artifacts
                    WHERE media_id=? AND role='live_segment' AND part_no=?
                    """,
                    (media_id, segment_part),
                ).fetchone()[0]
            )

            candidates = store.list_process_retention_candidates(cutoff)
            self.assertEqual(
                [candidate["artifact_id"] for candidate in candidates],
                [master_id],
            )
            detail = store.get_disk_resource(master_id, root)
            result = store.purge_process_artifacts(
                master_id,
                root,
                expected_revision=str(detail["resource_revision"]),
                delivered_before=cutoff,
            )

            self.assertTrue(result["completed"])
            self.assertEqual(result["deleted_files"], 1)
            self.assertTrue(master_path.exists())
            self.assertFalse(segment_path.exists())
            self.assertTrue(upload_path.exists())
            self.assertEqual(
                store.conn.execute(
                    "SELECT state FROM artifacts WHERE id=?",
                    (segment_id,),
                ).fetchone()[0],
                "purged",
            )
            self.assertEqual(
                store.conn.execute(
                    "SELECT state FROM artifacts WHERE id=?",
                    (upload_id,),
                ).fetchone()[0],
                "ready",
            )
            self.assertEqual(
                store.conn.execute(
                    "SELECT state FROM jobs WHERE id=?",
                    (delivery_job_id,),
                ).fetchone()[0],
                "blocked",
            )
            store.close()

    def test_process_purge_keeps_master_and_allows_derivative_regeneration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "process-purge"),
            )
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).isoformat()
            master_path = root / "process-purge.m4a"
            master_id = self._complete_download_for_media(
                store,
                media_id,
                master_path,
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            segment_path = root / "process-purge.segment.ts"
            upload_path = root / "process-purge.tg.m4a"
            thumbnail_path = root / "process-purge.tgthumb.jpg"
            for path, payload in (
                (segment_path, b"segment"),
                (upload_path, b"upload"),
                (thumbnail_path, b"thumbnail"),
            ):
                path.write_bytes(payload)
            segment_part = store.record_live_segment(
                media_id,
                path=segment_path,
                size_bytes=segment_path.stat().st_size,
            )
            upload_id = store.record_artifact(
                media_id,
                role="telegram_upload",
                path=upload_path,
                size_bytes=upload_path.stat().st_size,
            )
            thumbnail_id = store.record_artifact(
                media_id,
                role="thumbnail",
                path=thumbnail_path,
                size_bytes=thumbnail_path.stat().st_size,
                metadata={"delivery_derivative": True},
            )
            segment_id = int(
                store.conn.execute(
                    """
                    SELECT id FROM artifacts
                    WHERE media_id=? AND role='live_segment' AND part_no=?
                    """,
                    (media_id, segment_part),
                ).fetchone()[0]
            )
            detail = store.get_disk_resource(master_id, root)

            result = store.purge_process_artifacts(
                master_id,
                root,
                expected_revision=str(detail["resource_revision"]),
                delivered_before=cutoff,
            )

            self.assertTrue(result["completed"])
            self.assertEqual(result["deleted_files"], 3)
            self.assertTrue(master_path.exists())
            self.assertEqual(store.get_artifact(media_id)["state"], "ready")
            for artifact_id, path in (
                (segment_id, segment_path),
                (upload_id, upload_path),
                (thumbnail_id, thumbnail_path),
            ):
                self.assertFalse(path.exists())
                self.assertEqual(
                    store.conn.execute(
                        "SELECT state FROM artifacts WHERE id=?",
                        (artifact_id,),
                    ).fetchone()[0],
                    "purged",
                )
            self.assertEqual(
                store.list_process_retention_candidates(cutoff),
                [],
            )
            self.assertEqual(
                store.conn.execute(
                    "SELECT COUNT(*) FROM purge_path_reservations"
                ).fetchone()[0],
                0,
            )

            upload_path.write_bytes(b"new upload")
            regenerated_id = store.record_artifact(
                media_id,
                role="telegram_upload",
                path=upload_path,
                size_bytes=upload_path.stat().st_size,
            )
            self.assertEqual(regenerated_id, upload_id)
            self.assertEqual(
                store.conn.execute(
                    "SELECT state FROM artifacts WHERE id=?",
                    (upload_id,),
                ).fetchone()[0],
                "ready",
            )
            self.assertEqual(
                store.list_process_retention_candidates(cutoff)[0][
                    "artifact_id"
                ],
                master_id,
            )

    def test_purged_live_segment_can_be_recorded_again_at_same_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "segment-regeneration"),
            )
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).isoformat()
            master_id = self._complete_download_for_media(
                store,
                media_id,
                root / "segment-regeneration.m4a",
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            segment_path = root / "segment-regeneration.live.ts"
            segment_path.write_bytes(b"old segment")
            original_part = store.record_live_segment(
                media_id,
                path=segment_path,
                size_bytes=segment_path.stat().st_size,
                metadata={"attempt_order": 1},
            )
            segment_id = int(
                store.conn.execute(
                    """
                    SELECT id FROM artifacts
                    WHERE media_id=? AND role='live_segment' AND part_no=?
                    """,
                    (media_id, original_part),
                ).fetchone()[0]
            )
            detail = store.get_disk_resource(master_id, root)
            result = store.purge_process_artifacts(
                master_id,
                root,
                expected_revision=str(detail["resource_revision"]),
                delivered_before=cutoff,
            )
            self.assertTrue(result["completed"])
            self.assertFalse(segment_path.exists())
            self.assertEqual(
                store.conn.execute(
                    "SELECT COUNT(*) FROM purge_path_reservations"
                ).fetchone()[0],
                0,
            )

            segment_path.write_bytes(b"new longer segment")
            regenerated_part = store.record_live_segment(
                media_id,
                path=segment_path,
                size_bytes=segment_path.stat().st_size,
                metadata={"attempt_order": 2},
            )

            self.assertEqual(regenerated_part, original_part)
            regenerated = store.conn.execute(
                """
                SELECT id, state, size_bytes, metadata_json
                FROM artifacts WHERE id=?
                """,
                (segment_id,),
            ).fetchone()
            self.assertEqual(int(regenerated["id"]), segment_id)
            self.assertEqual(str(regenerated["state"]), "ready")
            self.assertEqual(
                int(regenerated["size_bytes"]),
                segment_path.stat().st_size,
            )
            self.assertEqual(
                json.loads(str(regenerated["metadata_json"])),
                {"attempt_order": 2},
            )
            self.assertEqual(
                store.list_process_retention_candidates(cutoff)[0][
                    "artifact_id"
                ],
                master_id,
            )

    def test_purge_failed_live_segment_can_be_recorded_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "segment-failed-regeneration"),
            )
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).isoformat()
            master_id = self._complete_download_for_media(
                store,
                media_id,
                root / "segment-failed-regeneration.m4a",
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            segment_path = root / "segment-failed-regeneration.live.ts"
            segment_path.write_bytes(b"old segment")
            part_no = store.record_live_segment(
                media_id,
                path=segment_path,
                size_bytes=segment_path.stat().st_size,
                metadata={"attempt_order": 1},
            )
            segment_id = int(
                store.conn.execute(
                    """
                    SELECT id FROM artifacts
                    WHERE media_id=? AND role='live_segment' AND part_no=?
                    """,
                    (media_id, part_no),
                ).fetchone()[0]
            )
            detail = store.get_disk_resource(master_id, root)
            with mock.patch.object(
                store_module,
                "_unlink_tracked_file",
                side_effect=PermissionError("read-only filesystem"),
            ):
                failed = store.purge_process_artifacts(
                    master_id,
                    root,
                    expected_revision=str(detail["resource_revision"]),
                    delivered_before=cutoff,
                )
            self.assertFalse(failed["completed"])
            self.assertEqual(
                store.conn.execute(
                    "SELECT state FROM artifacts WHERE id=?",
                    (segment_id,),
                ).fetchone()[0],
                "purge_failed",
            )
            self.assertEqual(
                store.conn.execute(
                    "SELECT COUNT(*) FROM purge_path_reservations"
                ).fetchone()[0],
                0,
            )

            segment_path.write_bytes(b"replacement segment")
            returned_part = store.record_live_segment(
                media_id,
                path=segment_path,
                size_bytes=segment_path.stat().st_size,
                metadata={"attempt_order": 9},
            )

            self.assertEqual(returned_part, part_no)
            regenerated = store.conn.execute(
                """
                SELECT state, size_bytes, metadata_json
                FROM artifacts WHERE id=?
                """,
                (segment_id,),
            ).fetchone()
            self.assertEqual(str(regenerated["state"]), "ready")
            self.assertEqual(
                int(regenerated["size_bytes"]),
                segment_path.stat().st_size,
            )
            self.assertEqual(
                json.loads(str(regenerated["metadata_json"])),
                {"attempt_order": 9},
            )

    def test_process_retention_requires_explicit_thumbnail_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "legacy-thumbnail"),
            )
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).isoformat()
            master_id = self._complete_download_for_media(
                store,
                media_id,
                root / "legacy-thumbnail.m4a",
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            thumbnail_path = root / "legacy-thumbnail.tgthumb.jpg"
            thumbnail_path.write_bytes(b"legacy")
            store.record_artifact(
                media_id,
                role="thumbnail",
                path=thumbnail_path,
                size_bytes=thumbnail_path.stat().st_size,
            )

            self.assertEqual(
                store.list_process_retention_candidates(cutoff),
                [],
            )
            detail = store.get_disk_resource(master_id, root)
            result = store.purge_process_artifacts(
                master_id,
                root,
                expected_revision=str(detail["resource_revision"]),
                delivered_before=cutoff,
            )
            self.assertTrue(result["skipped"])
            self.assertTrue(thumbnail_path.exists())
            self.assertEqual(store.get_artifact(media_id)["state"], "ready")

    def test_process_purge_rejects_paths_shared_with_retained_artifacts(self):
        for shared_with_other_media in (False, True):
            with self.subTest(shared_with_other_media=shared_with_other_media):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp) / "downloads"
                    root.mkdir()
                    store = Store(Path(tmp) / "state.db")
                    store.initialize()
                    store.upsert_origin(
                        Origin("yt", "youtube", "uploads", "YT", "UC-1")
                    )
                    media_id, _ = store.upsert_discovered(
                        "yt",
                        candidate(
                            "youtube",
                            f"process-shared-{shared_with_other_media}",
                        ),
                    )
                    old_delivery = (
                        datetime.now(timezone.utc) - timedelta(hours=48)
                    ).isoformat()
                    cutoff = (
                        datetime.now(timezone.utc) - timedelta(hours=24)
                    ).isoformat()
                    master_path = root / "process-shared-master.m4a"
                    master_id = self._complete_download_for_media(
                        store,
                        media_id,
                        master_path,
                        destination="telegram:@archive",
                        delivered_at=old_delivery,
                    )
                    shared_path = master_path
                    if shared_with_other_media:
                        other_media_id, _ = store.upsert_discovered(
                            "yt",
                            candidate("youtube", "process-shared-other"),
                        )
                        shared_path = root / "process-shared-other.m4a"
                        self._complete_download_for_media(
                            store,
                            other_media_id,
                            shared_path,
                        )
                    upload_id = store.record_artifact(
                        media_id,
                        role="telegram_upload",
                        path=shared_path,
                        size_bytes=shared_path.stat().st_size,
                    )
                    detail = store.get_disk_resource(master_id, root)

                    with self.assertRaisesRegex(
                        ValueError,
                        "another media item or retained artifact",
                    ):
                        store.purge_process_artifacts(
                            master_id,
                            root,
                            expected_revision=str(
                                detail["resource_revision"]
                            ),
                            delivered_before=cutoff,
                        )

                    self.assertTrue(master_path.exists())
                    self.assertTrue(shared_path.exists())
                    self.assertEqual(store.get_artifact(media_id)["state"], "ready")
                    self.assertEqual(
                        store.conn.execute(
                            "SELECT state FROM artifacts WHERE id=?",
                            (upload_id,),
                        ).fetchone()[0],
                        "ready",
                    )
                    self.assertEqual(
                        store.conn.execute(
                            "SELECT COUNT(*) FROM purge_path_reservations"
                        ).fetchone()[0],
                        0,
                    )

    def test_process_purge_phase_two_rechecks_job_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            db_path = Path(tmp) / "state.db"
            store = Store(db_path)
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "process-job-race"),
            )
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).isoformat()
            master_path = root / "process-job-race.m4a"
            master_id = self._complete_download_for_media(
                store,
                media_id,
                master_path,
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            upload_path = root / "process-job-race.tg.m4a"
            upload_path.write_bytes(b"upload")
            upload_id = store.record_artifact(
                media_id,
                role="telegram_upload",
                path=upload_path,
                size_bytes=upload_path.stat().st_size,
            )
            detail = store.get_disk_resource(master_id, root)
            contender = Store(db_path)
            contender.initialize()
            original_purge = Store._purge_reserved_files

            def queue_delivery_after_reservation(active_store: Store, **kwargs):
                contender.ensure_delivery_job(media_id, "telegram:@new")
                return original_purge(active_store, **kwargs)

            try:
                with mock.patch.object(
                    Store,
                    "_purge_reserved_files",
                    new=queue_delivery_after_reservation,
                ):
                    result = store.purge_process_artifacts(
                        master_id,
                        root,
                        expected_revision=str(detail["resource_revision"]),
                        delivered_before=cutoff,
                    )
            finally:
                contender.close()

            self.assertFalse(result["completed"])
            self.assertTrue(result["skipped"])
            self.assertTrue(master_path.exists())
            self.assertTrue(upload_path.exists())
            self.assertEqual(store.get_artifact(media_id)["state"], "ready")
            self.assertEqual(
                store.conn.execute(
                    "SELECT state FROM artifacts WHERE id=?",
                    (upload_id,),
                ).fetchone()[0],
                "ready",
            )
            self.assertEqual(
                store.conn.execute(
                    "SELECT COUNT(*) FROM purge_path_reservations"
                ).fetchone()[0],
                0,
            )

    def test_process_purge_phase_one_rechecks_delivery_and_anchor_state(self):
        for change in ("delivery_deleted", "anchor_staged"):
            with self.subTest(change=change):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp) / "downloads"
                    root.mkdir()
                    store = Store(Path(tmp) / "state.db")
                    store.initialize()
                    store.upsert_origin(
                        Origin("yt", "youtube", "uploads", "YT", "UC-1")
                    )
                    media_id, _ = store.upsert_discovered(
                        "yt",
                        candidate("youtube", f"process-phase-one-{change}"),
                    )
                    old_delivery = (
                        datetime.now(timezone.utc) - timedelta(hours=48)
                    ).isoformat()
                    cutoff = (
                        datetime.now(timezone.utc) - timedelta(hours=24)
                    ).isoformat()
                    master_path = root / f"process-phase-one-{change}.m4a"
                    master_id = self._complete_download_for_media(
                        store,
                        media_id,
                        master_path,
                        destination="telegram:@archive",
                        delivered_at=old_delivery,
                    )
                    upload_path = root / f"process-phase-one-{change}.tg.m4a"
                    upload_path.write_bytes(b"upload")
                    upload_id = store.record_artifact(
                        media_id,
                        role="telegram_upload",
                        path=upload_path,
                        size_bytes=upload_path.stat().st_size,
                    )
                    detail = store.get_disk_resource(master_id, root)
                    if change == "delivery_deleted":
                        store.conn.execute(
                            "DELETE FROM deliveries WHERE media_id=?",
                            (media_id,),
                        )
                    else:
                        store.conn.execute(
                            "UPDATE artifacts SET state='staged' WHERE id=?",
                            (master_id,),
                        )
                    store.conn.commit()

                    result = store.purge_process_artifacts(
                        master_id,
                        root,
                        expected_revision=str(detail["resource_revision"]),
                        delivered_before=cutoff,
                    )

                    self.assertFalse(result["completed"])
                    self.assertTrue(result["skipped"])
                    self.assertTrue(master_path.exists())
                    self.assertTrue(upload_path.exists())
                    self.assertEqual(
                        store.conn.execute(
                            "SELECT state FROM artifacts WHERE id=?",
                            (upload_id,),
                        ).fetchone()[0],
                        "ready",
                    )
                    self.assertEqual(
                        store.conn.execute(
                            "SELECT COUNT(*) FROM purge_path_reservations"
                        ).fetchone()[0],
                        0,
                    )

    def test_process_retention_keeps_process_files_without_usable_master(self):
        for master_condition in (
            "purged",
            "missing",
            "unsafe",
            "truncated",
            "zero",
        ):
            with self.subTest(master_condition=master_condition):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp) / "downloads"
                    root.mkdir()
                    store = Store(Path(tmp) / "state.db")
                    store.initialize()
                    store.upsert_origin(
                        Origin("yt", "youtube", "uploads", "YT", "UC-1")
                    )
                    media_id, _ = store.upsert_discovered(
                        "yt",
                        candidate(
                            "youtube",
                            f"process-master-{master_condition}",
                        ),
                    )
                    old_delivery = (
                        datetime.now(timezone.utc) - timedelta(hours=48)
                    ).isoformat()
                    cutoff = (
                        datetime.now(timezone.utc) - timedelta(hours=24)
                    ).isoformat()
                    master_path = (
                        Path(tmp) / "outside-master.m4a"
                        if master_condition == "unsafe"
                        else root / f"process-master-{master_condition}.m4a"
                    )
                    master_id = self._complete_download_for_media(
                        store,
                        media_id,
                        master_path,
                        destination="telegram:@archive",
                        delivered_at=old_delivery,
                    )
                    segment_path = (
                        root / f"process-master-{master_condition}.segment.ts"
                    )
                    segment_path.write_bytes(b"only recoverable segment")
                    store.record_live_segment(
                        media_id,
                        path=segment_path,
                        size_bytes=segment_path.stat().st_size,
                    )
                    if master_condition == "purged":
                        store.conn.execute(
                            "UPDATE artifacts SET state='purged' WHERE id=?",
                            (master_id,),
                        )
                        store.conn.commit()
                        self.assertEqual(
                            store.list_process_retention_candidates(cutoff),
                            [],
                        )
                        process_anchor = int(
                            store.list_disk_resources(root)["items"][0][
                                "artifact_id"
                            ]
                        )
                    else:
                        if master_condition == "missing":
                            master_path.unlink()
                        elif master_condition == "truncated":
                            master_path.write_bytes(b"a")
                        elif master_condition == "zero":
                            master_path.write_bytes(b"")
                            store.conn.execute(
                                "UPDATE artifacts SET size_bytes=0 WHERE id=?",
                                (master_id,),
                            )
                            store.conn.commit()
                        self.assertEqual(
                            store.list_process_retention_candidates(cutoff)[0][
                                "artifact_id"
                            ],
                            master_id,
                        )
                        process_anchor = master_id
                    detail = store.get_disk_resource(process_anchor, root)

                    result = store.purge_process_artifacts(
                        process_anchor,
                        root,
                        expected_revision=str(detail["resource_revision"]),
                        delivered_before=cutoff,
                    )

                    self.assertFalse(result["completed"])
                    self.assertTrue(result["skipped"])
                    self.assertTrue(segment_path.exists())
                    self.assertEqual(
                        store.conn.execute(
                            """
                            SELECT state FROM artifacts
                            WHERE media_id=? AND role='live_segment'
                            """,
                            (media_id,),
                        ).fetchone()[0],
                        "ready",
                    )
                    self.assertEqual(
                        store.conn.execute(
                            "SELECT COUNT(*) FROM purge_path_reservations"
                        ).fetchone()[0],
                        0,
                    )

    def test_process_purge_stops_if_master_disappears_after_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "process-master-race"),
            )
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).isoformat()
            master_path = root / "process-master-race.m4a"
            master_id = self._complete_download_for_media(
                store,
                media_id,
                master_path,
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            segment_path = root / "process-master-race.segment.ts"
            segment_path.write_bytes(b"recoverable segment")
            part_no = store.record_live_segment(
                media_id,
                path=segment_path,
                size_bytes=segment_path.stat().st_size,
            )
            segment_id = int(
                store.conn.execute(
                    """
                    SELECT id FROM artifacts
                    WHERE media_id=? AND role='live_segment' AND part_no=?
                    """,
                    (media_id, part_no),
                ).fetchone()[0]
            )
            detail = store.get_disk_resource(master_id, root)
            original_purge = Store._purge_reserved_files

            def remove_master_after_reservation(active_store: Store, **kwargs):
                master_path.unlink()
                return original_purge(active_store, **kwargs)

            with mock.patch.object(
                Store,
                "_purge_reserved_files",
                new=remove_master_after_reservation,
            ):
                result = store.purge_process_artifacts(
                    master_id,
                    root,
                    expected_revision=str(detail["resource_revision"]),
                    delivered_before=cutoff,
                )

            self.assertFalse(result["completed"])
            self.assertFalse(result["skipped"])
            self.assertTrue(segment_path.exists())
            self.assertEqual(
                store.conn.execute(
                    "SELECT state FROM artifacts WHERE id=?",
                    (segment_id,),
                ).fetchone()[0],
                "purge_failed",
            )
            self.assertEqual(
                store.conn.execute(
                    "SELECT COUNT(*) FROM purge_path_reservations"
                ).fetchone()[0],
                0,
            )

    def test_process_purge_crash_recovery_releases_missing_path_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            db_path = Path(tmp) / "state.db"
            store = Store(db_path)
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "process-crash"),
            )
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).isoformat()
            master_path = root / "process-crash.m4a"
            master_id = self._complete_download_for_media(
                store,
                media_id,
                master_path,
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            upload_path = root / "process-crash.tg.m4a"
            upload_path.write_bytes(b"upload")
            upload_id = store.record_artifact(
                media_id,
                role="telegram_upload",
                path=upload_path,
                size_bytes=upload_path.stat().st_size,
            )
            detail = store.get_disk_resource(master_id, root)

            with mock.patch.object(
                store,
                "_purge_reserved_files",
                side_effect=RuntimeError("simulated process death"),
            ):
                with self.assertRaisesRegex(RuntimeError, "process death"):
                    store.purge_process_artifacts(
                        master_id,
                        root,
                        expected_revision=str(detail["resource_revision"]),
                        delivered_before=cutoff,
                    )
            self.assertEqual(
                store.conn.execute(
                    "SELECT state FROM artifacts WHERE id=?",
                    (upload_id,),
                ).fetchone()[0],
                "purging",
            )
            upload_path.unlink()

            recovered = Store(db_path)
            try:
                with mock.patch.object(
                    store_module,
                    "_process_instance_is_alive",
                    return_value=False,
                ):
                    recovered.initialize()
                self.assertEqual(
                    recovered.conn.execute(
                        "SELECT state FROM artifacts WHERE id=?",
                        (upload_id,),
                    ).fetchone()[0],
                    "purged",
                )
                self.assertEqual(
                    recovered.conn.execute(
                        "SELECT COUNT(*) FROM purge_path_reservations"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    recovered.get_artifact(media_id)["state"],
                    "ready",
                )
                self.assertTrue(master_path.exists())
                self.assertEqual(
                    recovered.list_process_retention_candidates(cutoff),
                    [],
                )
            finally:
                recovered.close()

    def test_process_purge_does_not_pin_twitch_fallback_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("tw", "twitch", "vods", "TW", "streamer")
            )
            stream_id = "process-no-pin"
            vod_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "process-no-pin-vod",
                    kind="vod",
                    metadata={"stream_id": stream_id},
                ),
            )
            self._complete_download_for_media(
                store,
                vod_media_id,
                root / "process-no-pin-vod.m4a",
            )
            live_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "process-no-pin-live",
                    kind="live_stream",
                    metadata={"stream_id": stream_id},
                ),
            )
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).isoformat()
            live_master_path = root / "process-no-pin-live.m4a"
            live_master_id = self._complete_download_for_media(
                store,
                live_media_id,
                live_master_path,
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            segment_path = root / "process-no-pin-live.segment.ts"
            segment_path.write_bytes(b"segment")
            store.record_live_segment(
                live_media_id,
                path=segment_path,
                size_bytes=segment_path.stat().st_size,
            )
            fallback_before = store.conn.execute(
                """
                SELECT state, reason_code FROM jobs
                WHERE media_id=? AND job_type='download'
                """,
                (vod_media_id,),
            ).fetchone()
            self.assertEqual(
                tuple(fallback_before),
                ("cancelled", "live_recording_exists"),
            )
            detail = store.get_disk_resource(live_master_id, root)

            result = store.purge_process_artifacts(
                live_master_id,
                root,
                expected_revision=str(detail["resource_revision"]),
                delivered_before=cutoff,
            )

            self.assertTrue(result["completed"])
            self.assertFalse(segment_path.exists())
            self.assertTrue(live_master_path.exists())
            fallback_after = store.conn.execute(
                """
                SELECT state, reason_code FROM jobs
                WHERE media_id=? AND job_type='download'
                """,
                (vod_media_id,),
            ).fetchone()
            self.assertEqual(
                tuple(fallback_after),
                ("cancelled", "live_recording_exists"),
            )

    def test_twitch_retention_group_uses_latest_delivery_and_suppressed_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("tw", "twitch", "vods", "TW", "streamer")
            )
            stream_id = "retention-stream"
            vod_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "retention-vod",
                    kind="vod",
                    metadata={"stream_id": stream_id},
                ),
            )
            vod_artifact_id = self._complete_download_for_media(
                store,
                vod_media_id,
                root / "retention-vod.m4a",
            )
            live_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "retention-live",
                    kind="live_stream",
                    metadata={
                        "stream_id": stream_id,
                        "recording_mode": "live",
                    },
                ),
            )
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=72)
            ).isoformat()
            live_artifact_id = self._complete_download_for_media(
                store,
                live_media_id,
                root / "retention-live.m4a",
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()

            candidates = store.list_delivery_retention_candidates(cutoff)
            self.assertEqual(
                [item["artifact_id"] for item in candidates],
                [vod_artifact_id, live_artifact_id],
            )
            self.assertEqual(
                [item["artifact_state"] for item in candidates],
                ["suppressed", "ready"],
            )
            self.assertEqual(
                {item["group_key"] for item in candidates},
                {f"twitch:stream:{stream_id}"},
            )
            self.assertEqual(
                {item["delivered_at"] for item in candidates},
                {old_delivery},
            )

            recent_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=1)
            ).isoformat()
            store.conn.execute(
                """
                INSERT INTO deliveries(
                  media_id, artifact_id, sink, destination_key,
                  remote_id, delivered_at
                ) VALUES (?, ?, 'telegram', 'telegram:@other', 'new', ?)
                """,
                (vod_media_id, vod_artifact_id, recent_delivery),
            )
            store.conn.commit()
            self.assertEqual(
                store.list_delivery_retention_candidates(cutoff),
                [],
            )

            store.conn.execute(
                "DELETE FROM deliveries WHERE destination_key='telegram:@other'"
            )
            store.conn.execute(
                """
                UPDATE jobs SET state='retry'
                WHERE media_id=? AND job_type='download'
                """,
                (vod_media_id,),
            )
            store.conn.commit()
            self.assertEqual(
                store.list_delivery_retention_candidates(cutoff),
                [],
            )

    def test_retention_group_only_lends_delivery_to_suppressed_fallbacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("tw", "twitch", "vods", "TW", "streamer")
            )
            stream_id = "retention-resource-gate"
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=72)
            ).isoformat()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()

            live_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "retention-gate-live",
                    kind="live_stream",
                    metadata={
                        "stream_id": stream_id,
                        "recording_mode": "live",
                    },
                ),
            )
            live_artifact_id = self._complete_download_for_media(
                store,
                live_media_id,
                root / "retention-gate-live.m4a",
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            highlight_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "retention-gate-highlight",
                    kind="highlight",
                    metadata={"stream_id": stream_id},
                ),
            )
            highlight_artifact_id = self._complete_download_for_media(
                store,
                highlight_media_id,
                root / "retention-gate-highlight.m4a",
            )
            vod_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "retention-gate-vod",
                    kind="vod",
                    metadata={"stream_id": stream_id},
                ),
            )
            vod_artifact_id = self._complete_download_for_media(
                store,
                vod_media_id,
                root / "retention-gate-vod.m4a",
            )

            initial = store.list_delivery_retention_candidates(cutoff)
            self.assertEqual(
                [item["artifact_id"] for item in initial],
                [vod_artifact_id, live_artifact_id],
            )
            self.assertNotIn(
                highlight_artifact_id,
                [item["artifact_id"] for item in initial],
            )
            stale_detail = store.get_disk_resource(vod_artifact_id, root)
            store.conn.execute(
                "UPDATE artifacts SET state='ready', updated_at=? WHERE id=?",
                (now_iso(), vod_artifact_id),
            )
            store.conn.commit()

            candidates = store.list_delivery_retention_candidates(cutoff)
            self.assertEqual(
                [item["artifact_id"] for item in candidates],
                [live_artifact_id],
            )
            vod_result = store.purge_disk_resource(
                vod_artifact_id,
                root,
                expected_revision=str(stale_detail["resource_revision"]),
                source="delivery_retention",
                delivery_retention_before=cutoff,
            )
            self.assertFalse(vod_result["completed"])
            self.assertTrue(vod_result["skipped"])
            self.assertTrue((root / "retention-gate-vod.m4a").exists())

            highlight_detail = store.get_disk_resource(
                highlight_artifact_id,
                root,
            )
            highlight_result = store.purge_disk_resource(
                highlight_artifact_id,
                root,
                expected_revision=str(
                    highlight_detail["resource_revision"]
                ),
                source="delivery_retention",
                delivery_retention_before=cutoff,
            )
            self.assertFalse(highlight_result["completed"])
            self.assertTrue(highlight_result["skipped"])
            self.assertTrue(
                (root / "retention-gate-highlight.m4a").exists()
            )

    def test_retention_phase_two_rechecks_resource_own_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            db_path = Path(tmp) / "state.db"
            store = Store(db_path)
            store.initialize()
            store.upsert_origin(
                Origin("tw", "twitch", "vods", "TW", "streamer")
            )
            stream_id = "retention-own-delivery-race"
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=72)
            ).isoformat()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            live_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "retention-own-live",
                    kind="live_stream",
                    metadata={
                        "stream_id": stream_id,
                        "recording_mode": "live",
                    },
                ),
            )
            live_path = root / "retention-own-live.m4a"
            live_artifact_id = self._complete_download_for_media(
                store,
                live_media_id,
                live_path,
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            vod_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "retention-own-vod",
                    kind="vod",
                    metadata={"stream_id": stream_id},
                ),
            )
            vod_artifact_id = self._complete_download_for_media(
                store,
                vod_media_id,
                root / "retention-own-vod.m4a",
            )
            store.conn.execute(
                """
                INSERT INTO deliveries(
                  media_id, artifact_id, sink, destination_key,
                  remote_id, delivered_at
                ) VALUES (?, ?, 'telegram', 'telegram:@fallback', 'vod', ?)
                """,
                (vod_media_id, vod_artifact_id, old_delivery),
            )
            store.conn.commit()
            detail = store.get_disk_resource(live_artifact_id, root)
            second = Store(db_path)
            second.initialize()
            original_purge = Store._purge_reserved_files

            def remove_own_delivery_after_reservation(
                active_store: Store,
                **kwargs,
            ):
                second.conn.execute(
                    "DELETE FROM deliveries WHERE media_id=?",
                    (live_media_id,),
                )
                second.conn.commit()
                return original_purge(active_store, **kwargs)

            with mock.patch.object(
                Store,
                "_purge_reserved_files",
                new=remove_own_delivery_after_reservation,
            ):
                result = store.purge_disk_resource(
                    live_artifact_id,
                    root,
                    expected_revision=str(detail["resource_revision"]),
                    source="delivery_retention",
                    delivery_retention_before=cutoff,
                )

            self.assertFalse(result["completed"])
            self.assertTrue(result["skipped"])
            self.assertTrue(live_path.exists())
            self.assertEqual(
                store.get_artifact(live_media_id)["state"],
                "ready",
            )
            self.assertEqual(
                store.conn.execute(
                    "SELECT COUNT(*) FROM purge_path_reservations"
                ).fetchone()[0],
                0,
            )

            refreshed = store.get_disk_resource(live_artifact_id, root)
            phase_one_skip = store.purge_disk_resource(
                live_artifact_id,
                root,
                expected_revision=str(refreshed["resource_revision"]),
                source="delivery_retention",
                delivery_retention_before=cutoff,
            )
            self.assertFalse(phase_one_skip["completed"])
            self.assertTrue(phase_one_skip["skipped"])
            self.assertTrue(live_path.exists())
            second.close()

    def test_failed_suppressed_twitch_resource_stays_before_delivered_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("tw", "twitch", "vods", "TW", "streamer")
            )
            stream_id = "retention-retry-order"
            vod_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "retention-delivered-vod",
                    kind="vod",
                    metadata={"stream_id": stream_id},
                ),
            )
            vod_path = root / "retention-delivered-vod.m4a"
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=72)
            ).isoformat()
            vod_artifact_id = self._complete_download_for_media(
                store,
                vod_media_id,
                vod_path,
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            live_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "retention-suppressed-live",
                    kind="live_stream",
                    metadata={
                        "stream_id": stream_id,
                        "recording_mode": "live",
                    },
                ),
            )
            live_path = root / "retention-suppressed-live.m4a"
            live_artifact_id = self._complete_download_for_media(
                store,
                live_media_id,
                live_path,
            )
            self.assertGreater(live_artifact_id, vod_artifact_id)
            self.assertEqual(
                store.get_artifact(live_media_id)["state"],
                "suppressed",
            )
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            initial = store.list_delivery_retention_candidates(cutoff)
            self.assertEqual(
                [item["artifact_id"] for item in initial],
                [live_artifact_id, vod_artifact_id],
            )
            detail = store.get_disk_resource(live_artifact_id, root)

            with mock.patch.object(
                store_module,
                "_unlink_tracked_file",
                side_effect=PermissionError("read-only filesystem"),
            ):
                failed = store.purge_disk_resource(
                    live_artifact_id,
                    root,
                    expected_revision=str(detail["resource_revision"]),
                    source="delivery_retention",
                    delivery_retention_before=cutoff,
                )

            self.assertFalse(failed["completed"])
            self.assertEqual(
                store.get_artifact(live_media_id)["state"],
                "purge_failed",
            )
            retried = store.list_delivery_retention_candidates(cutoff)
            self.assertEqual(
                [item["artifact_id"] for item in retried],
                [live_artifact_id, vod_artifact_id],
            )
            self.assertEqual(
                [item["artifact_state"] for item in retried],
                ["purge_failed", "ready"],
            )

            retry_detail = store.get_disk_resource(live_artifact_id, root)
            with mock.patch.object(
                store_module,
                "_unlink_tracked_file",
                side_effect=PermissionError("still read-only"),
            ):
                failed_again = store.purge_disk_resource(
                    live_artifact_id,
                    root,
                    expected_revision=str(
                        retry_detail["resource_revision"]
                    ),
                    source="delivery_retention",
                    delivery_retention_before=cutoff,
                )

            self.assertFalse(failed_again["completed"])
            failed_metadata = json.loads(
                str(store.get_artifact(live_media_id)["metadata_json"])
            )["local_purge"]
            self.assertEqual(
                failed_metadata["previous_state"],
                "purge_failed",
            )
            self.assertTrue(failed_metadata["retention_fallback"])
            retried_again = store.list_delivery_retention_candidates(cutoff)
            self.assertEqual(
                [item["artifact_id"] for item in retried_again],
                [live_artifact_id, vod_artifact_id],
            )

    def test_retention_purge_pins_cancelled_twitch_download_before_deleting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("tw", "twitch", "vods", "TW", "streamer")
            )
            stream_id = "retention-pin-stream"
            vod_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "retention-pin-vod",
                    kind="vod",
                    metadata={"stream_id": stream_id},
                ),
            )
            vod_path = root / "retention-pin-vod.m4a"
            vod_artifact_id = self._complete_download_for_media(
                store,
                vod_media_id,
                vod_path,
            )
            live_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "retention-pin-live",
                    kind="live_stream",
                    metadata={
                        "stream_id": stream_id,
                        "recording_mode": "live",
                    },
                ),
            )
            live_path = root / "retention-pin-live.m4a"
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=72)
            ).isoformat()
            self._complete_download_for_media(
                store,
                live_media_id,
                live_path,
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            detail = store.get_disk_resource(vod_artifact_id, root)

            result = store.purge_disk_resource(
                vod_artifact_id,
                root,
                expected_revision=str(detail["resource_revision"]),
                source="delivery_retention",
                delivery_retention_before=cutoff,
            )

            self.assertTrue(result["completed"])
            self.assertFalse(result["skipped"])
            self.assertFalse(vod_path.exists())
            self.assertTrue(live_path.exists())
            self.assertEqual(
                store.get_artifact(vod_media_id)["state"],
                "purged",
            )
            pinned = store.conn.execute(
                """
                SELECT state, reason_code
                FROM jobs
                WHERE media_id=? AND job_type='download'
                """,
                (vod_media_id,),
            ).fetchone()
            self.assertEqual(tuple(pinned), ("cancelled", "resource_purged"))
            purge_metadata = json.loads(
                str(store.get_artifact(vod_media_id)["metadata_json"])
            )["local_purge"]
            self.assertEqual(purge_metadata["source"], "delivery_retention")
            self.assertEqual(
                purge_metadata["retention_group_key"],
                f"twitch:stream:{stream_id}",
            )

    def test_retention_recovery_repairs_pin_after_unlink_crash(self):
        class SimulatedCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            db_path = Path(tmp) / "state.db"
            store = Store(db_path)
            store.initialize()
            store.upsert_origin(
                Origin("tw", "twitch", "vods", "TW", "streamer")
            )
            stream_id = "retention-crash-pin"
            vod_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "retention-crash-vod",
                    kind="vod",
                    metadata={"stream_id": stream_id},
                ),
            )
            vod_path = root / "retention-crash-vod.m4a"
            vod_artifact_id = self._complete_download_for_media(
                store,
                vod_media_id,
                vod_path,
            )
            live_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "retention-crash-live",
                    kind="live_stream",
                    metadata={
                        "stream_id": stream_id,
                        "recording_mode": "live",
                    },
                ),
            )
            live_path = root / "retention-crash-live.m4a"
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=72)
            ).isoformat()
            self._complete_download_for_media(
                store,
                live_media_id,
                live_path,
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            detail = store.get_disk_resource(vod_artifact_id, root)
            original_unlink = store_module._unlink_tracked_file

            def unlink_then_crash(*args, **kwargs):
                original_unlink(*args, **kwargs)
                raise SimulatedCrash("process stopped after unlink")

            with mock.patch.object(
                store_module,
                "_unlink_tracked_file",
                new=unlink_then_crash,
            ):
                with self.assertRaises(SimulatedCrash):
                    store.purge_disk_resource(
                        vod_artifact_id,
                        root,
                        expected_revision=str(detail["resource_revision"]),
                        source="delivery_retention",
                        delivery_retention_before=cutoff,
                    )

            self.assertFalse(vod_path.exists())
            store.close()
            raw = sqlite3.connect(db_path)
            raw.execute(
                """
                UPDATE purge_path_reservations
                SET owner_pid=0, owner_start_id=''
                WHERE state='active'
                """
            )
            raw.commit()
            raw.close()

            recovered = Store(db_path)
            recovered.initialize()
            pinned = recovered.conn.execute(
                """
                SELECT state, reason_code
                FROM jobs
                WHERE media_id=? AND job_type='download'
                """,
                (vod_media_id,),
            ).fetchone()
            self.assertEqual(tuple(pinned), ("cancelled", "resource_purged"))
            recovered_artifact = recovered.get_artifact(vod_media_id)
            self.assertEqual(recovered_artifact["state"], "purge_failed")
            recovered_metadata = json.loads(
                str(recovered_artifact["metadata_json"])
            )["local_purge"]
            self.assertTrue(recovered_metadata["path_existed"])
            self.assertEqual(recovered_metadata["result"], "interrupted")
            self.assertEqual(
                recovered.conn.execute(
                    """
                    SELECT state FROM purge_path_reservations
                    WHERE media_id=?
                    """,
                    (vod_media_id,),
                ).fetchone()[0],
                "purged",
            )

            live_path.unlink()
            recovered.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "retention-crash-vod",
                    kind="vod",
                    metadata={"stream_id": stream_id},
                ),
            )
            still_pinned = recovered.conn.execute(
                """
                SELECT state, reason_code
                FROM jobs
                WHERE media_id=? AND job_type='download'
                """,
                (vod_media_id,),
            ).fetchone()
            self.assertEqual(
                tuple(still_pinned),
                ("cancelled", "resource_purged"),
            )
            recovered.close()

    def test_live_retention_crash_pins_matching_vod_without_artifact(self):
        class SimulatedCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            db_path = Path(tmp) / "state.db"
            store = Store(db_path)
            store.initialize()
            store.upsert_origin(
                Origin("tw", "twitch", "vods", "TW", "streamer")
            )
            stream_id = "retention-live-crash-no-vod-artifact"
            vod_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "retention-no-artifact-vod",
                    kind="vod",
                    metadata={"stream_id": stream_id},
                ),
            )
            vod_job = store.claim_next_job(
                ("download",),
                owner="vod-cancel",
                lease_seconds=60,
            )
            store.cancel_job(
                vod_job,
                reason_code="live_recording_exists",
                error="matching Twitch live stream was already archived",
            )
            self.assertIsNone(store.get_artifact(vod_media_id))

            live_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "retention-live-crash-target",
                    kind="live_stream",
                    metadata={
                        "stream_id": stream_id,
                        "recording_mode": "live",
                    },
                ),
            )
            live_path = root / "retention-live-crash-target.m4a"
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=72)
            ).isoformat()
            live_artifact_id = self._complete_download_for_media(
                store,
                live_media_id,
                live_path,
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            detail = store.get_disk_resource(live_artifact_id, root)
            original_unlink = store_module._unlink_tracked_file

            def unlink_then_crash(*args, **kwargs):
                original_unlink(*args, **kwargs)
                raise SimulatedCrash("process stopped after live unlink")

            with mock.patch.object(
                store_module,
                "_unlink_tracked_file",
                new=unlink_then_crash,
            ):
                with self.assertRaises(SimulatedCrash):
                    store.purge_disk_resource(
                        live_artifact_id,
                        root,
                        expected_revision=str(detail["resource_revision"]),
                        source="delivery_retention",
                        delivery_retention_before=cutoff,
                    )

            store.close()
            raw = sqlite3.connect(db_path)
            raw.execute(
                """
                UPDATE purge_path_reservations
                SET owner_pid=0, owner_start_id=''
                WHERE state='active'
                """
            )
            raw.commit()
            raw.close()
            recovered = Store(db_path)
            recovered.initialize()

            vod_download = recovered.conn.execute(
                """
                SELECT state, reason_code
                FROM jobs
                WHERE media_id=? AND job_type='download'
                """,
                (vod_media_id,),
            ).fetchone()
            self.assertEqual(
                tuple(vod_download),
                ("cancelled", "resource_purged"),
            )
            self.assertIsNone(recovered.get_artifact(vod_media_id))
            recovered.close()

    def test_retention_pin_preserves_same_stream_source_filter_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("tw", "twitch", "vods", "TW", "streamer")
            )
            stream_id = "retention-source-filter-stream"
            vod_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "retention-source-filter-vod",
                    kind="vod",
                    metadata={"stream_id": stream_id},
                ),
            )
            vod_job = store.claim_next_job(
                ("download",),
                owner="source-filter",
                lease_seconds=60,
            )
            store.cancel_job(
                vod_job,
                reason_code="source_filter",
                error="excluded by source filter",
            )
            live_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "retention-source-filter-live",
                    kind="live_stream",
                    metadata={
                        "stream_id": stream_id,
                        "recording_mode": "live",
                    },
                ),
            )
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=72)
            ).isoformat()
            live_artifact_id = self._complete_download_for_media(
                store,
                live_media_id,
                root / "retention-source-filter-live.m4a",
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            detail = store.get_disk_resource(live_artifact_id, root)

            result = store.purge_disk_resource(
                live_artifact_id,
                root,
                expected_revision=str(detail["resource_revision"]),
                source="delivery_retention",
                delivery_retention_before=cutoff,
            )

            self.assertTrue(result["completed"])
            vod_download = store.conn.execute(
                """
                SELECT state, reason_code
                FROM jobs
                WHERE media_id=? AND job_type='download'
                """,
                (vod_media_id,),
            ).fetchone()
            self.assertEqual(
                tuple(vod_download),
                ("cancelled", "source_filter"),
            )

    def test_non_twitch_retention_crash_preserves_source_filter_cancel(self):
        class SimulatedCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            db_path = Path(tmp) / "state.db"
            store = Store(db_path)
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "retention-non-twitch-source-filter"),
            )
            path = root / "retention-non-twitch-source-filter.m4a"
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=72)
            ).isoformat()
            artifact_id = self._complete_download_for_media(
                store,
                media_id,
                path,
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            store.conn.execute(
                """
                UPDATE jobs
                SET state='cancelled', reason_code='source_filter'
                WHERE media_id=? AND job_type='download'
                """,
                (media_id,),
            )
            store.conn.commit()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            detail = store.get_disk_resource(artifact_id, root)
            original_unlink = store_module._unlink_tracked_file

            def unlink_then_crash(*args, **kwargs):
                original_unlink(*args, **kwargs)
                raise SimulatedCrash("process stopped after YouTube unlink")

            with mock.patch.object(
                store_module,
                "_unlink_tracked_file",
                new=unlink_then_crash,
            ):
                with self.assertRaises(SimulatedCrash):
                    store.purge_disk_resource(
                        artifact_id,
                        root,
                        expected_revision=str(detail["resource_revision"]),
                        source="delivery_retention",
                        delivery_retention_before=cutoff,
                    )

            store.close()
            raw = sqlite3.connect(db_path)
            raw.execute(
                """
                UPDATE purge_path_reservations
                SET owner_pid=0, owner_start_id=''
                WHERE state='active'
                """
            )
            raw.commit()
            raw.close()
            recovered = Store(db_path)
            recovered.initialize()

            download = recovered.conn.execute(
                """
                SELECT state, reason_code
                FROM jobs
                WHERE media_id=? AND job_type='download'
                """,
                (media_id,),
            ).fetchone()
            self.assertEqual(
                tuple(download),
                ("cancelled", "source_filter"),
            )
            recovered.close()

    def test_retention_recovery_does_not_pin_path_missing_at_reservation(self):
        class SimulatedCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            db_path = Path(tmp) / "state.db"
            store = Store(db_path)
            store.initialize()
            store.upsert_origin(
                Origin("tw", "twitch", "vods", "TW", "streamer")
            )
            stream_id = "retention-missing-before-reserve"
            vod_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "retention-missing-vod",
                    kind="vod",
                    metadata={"stream_id": stream_id},
                ),
            )
            vod_path = root / "retention-missing-vod.m4a"
            vod_artifact_id = self._complete_download_for_media(
                store,
                vod_media_id,
                vod_path,
            )
            live_media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "retention-missing-live",
                    kind="live_stream",
                    metadata={
                        "stream_id": stream_id,
                        "recording_mode": "live",
                    },
                ),
            )
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=72)
            ).isoformat()
            self._complete_download_for_media(
                store,
                live_media_id,
                root / "retention-missing-live.m4a",
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            vod_path.unlink()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            detail = store.get_disk_resource(vod_artifact_id, root)

            with mock.patch.object(
                Store,
                "_purge_reserved_files",
                side_effect=SimulatedCrash("before phase two"),
            ):
                with self.assertRaises(SimulatedCrash):
                    store.purge_disk_resource(
                        vod_artifact_id,
                        root,
                        expected_revision=str(detail["resource_revision"]),
                        source="delivery_retention",
                        delivery_retention_before=cutoff,
                    )

            store.close()
            raw = sqlite3.connect(db_path)
            raw.execute(
                """
                UPDATE purge_path_reservations
                SET owner_pid=0, owner_start_id=''
                WHERE state='active'
                """
            )
            raw.commit()
            raw.close()
            recovered = Store(db_path)
            recovered.initialize()

            job = recovered.conn.execute(
                """
                SELECT state, reason_code
                FROM jobs
                WHERE media_id=? AND job_type='download'
                """,
                (vod_media_id,),
            ).fetchone()
            self.assertEqual(
                tuple(job),
                ("cancelled", "live_recording_exists"),
            )
            artifact = recovered.get_artifact(vod_media_id)
            self.assertEqual(artifact["state"], "purge_failed")
            self.assertFalse(
                json.loads(str(artifact["metadata_json"]))["local_purge"][
                    "path_existed"
                ]
            )
            recovered.close()

    def test_retention_purge_skips_and_restores_when_group_job_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            db_path = Path(tmp) / "state.db"
            store = Store(db_path)
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "retention-race"),
            )
            path = root / "retention-race.m4a"
            old_delivery = (
                datetime.now(timezone.utc) - timedelta(hours=72)
            ).isoformat()
            artifact_id = self._complete_download_for_media(
                store,
                media_id,
                path,
                destination="telegram:@archive",
                delivered_at=old_delivery,
            )
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            detail = store.get_disk_resource(artifact_id, root)
            second = Store(db_path)
            second.initialize()
            original_purge = Store._purge_reserved_files

            def add_job_after_reservation(active_store: Store, **kwargs):
                second.ensure_delivery_job(media_id, "telegram:@late")
                return original_purge(active_store, **kwargs)

            with mock.patch.object(
                Store,
                "_purge_reserved_files",
                new=add_job_after_reservation,
            ):
                result = store.purge_disk_resource(
                    artifact_id,
                    root,
                    expected_revision=str(detail["resource_revision"]),
                    source="delivery_retention",
                    delivery_retention_before=cutoff,
                )

            self.assertFalse(result["completed"])
            self.assertTrue(result["skipped"])
            self.assertTrue(path.exists())
            artifact = store.get_artifact(media_id)
            self.assertEqual(artifact["state"], "ready")
            self.assertEqual(
                json.loads(str(artifact["metadata_json"]))["local_purge"][
                    "result"
                ],
                "skipped",
            )
            self.assertEqual(
                store.conn.execute(
                    "SELECT COUNT(*) FROM purge_path_reservations"
                ).fetchone()[0],
                0,
            )
            late_job = store.conn.execute(
                """
                SELECT state, reason_code
                FROM jobs
                WHERE media_id=? AND target_key='telegram:@late'
                """,
                (media_id,),
            ).fetchone()
            self.assertEqual(tuple(late_job), ("queued", None))

            refreshed = store.get_disk_resource(artifact_id, root)
            phase_one_skip = store.purge_disk_resource(
                artifact_id,
                root,
                expected_revision=str(refreshed["resource_revision"]),
                source="delivery_retention",
                delivery_retention_before=cutoff,
            )
            self.assertFalse(phase_one_skip["completed"])
            self.assertTrue(phase_one_skip["skipped"])
            self.assertEqual(store.get_artifact(media_id)["state"], "ready")
            self.assertTrue(path.exists())
            second.close()

    def test_disk_resource_library_search_detail_and_purge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            youtube_dir = root / "youtube" / "channel"
            twitch_dir = root / "twitch" / "streamer"
            youtube_dir.mkdir(parents=True)
            twitch_dir.mkdir(parents=True)
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "Quiet ASMR", "UC-1")
            )
            store.upsert_origin(
                Origin("tw", "twitch", "vods", "Live ASMR", "42")
            )
            youtube_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "first"),
            )
            twitch_id, _ = store.upsert_discovered(
                "tw",
                candidate("twitch", "second", kind="vod"),
            )

            youtube_path = youtube_dir / "first.m4a"
            youtube_path.write_bytes(b"one")
            youtube_job = store.claim_next_job(
                ("download",),
                owner="youtube",
                lease_seconds=60,
            )
            self.assertEqual(youtube_job.media_id, youtube_id)
            youtube_artifact_id = store.complete_download(
                youtube_job,
                path=youtube_path,
                size_bytes=3,
                delivery_targets=("telegram:@archive",),
            )
            thumbnail_path = youtube_dir / "first.jpg"
            thumbnail_path.write_bytes(b"thumb")
            store.record_artifact(
                youtube_id,
                role="thumbnail",
                path=thumbnail_path,
                size_bytes=5,
            )
            delivery = store.claim_next_job(
                ("telegram_delivery",),
                owner="delivery",
                lease_seconds=60,
            )
            store.complete_delivery(
                delivery,
                artifact_id=youtube_artifact_id,
                destination_key="telegram:@archive",
                remote_id="100",
            )
            store.ensure_delivery_job(youtube_id, "telegram:@other")

            twitch_path = twitch_dir / "second.m4a"
            twitch_path.write_bytes(b"two-two")
            twitch_job = store.claim_next_job(
                ("download",),
                owner="twitch",
                lease_seconds=60,
            )
            self.assertEqual(twitch_job.media_id, twitch_id)
            twitch_artifact_id = store.complete_download(
                twitch_job,
                path=twitch_path,
                size_bytes=7,
            )
            store.conn.execute(
                "UPDATE artifacts SET state='suppressed' WHERE id=?",
                (twitch_artifact_id,),
            )
            store.conn.commit()

            first_page = store.list_disk_resources(root, limit=1)
            self.assertEqual(first_page["total"], 2)
            self.assertEqual(len(first_page["items"]), 1)
            self.assertEqual(
                first_page["items"][0]["artifact_id"],
                twitch_artifact_id,
            )
            search = store.list_disk_resources(
                root,
                query="quiet first",
            )
            self.assertEqual(search["total"], 1)
            self.assertEqual(
                search["items"][0]["artifact_id"],
                youtube_artifact_id,
            )

            detail = store.get_disk_resource(youtube_artifact_id, root)
            self.assertEqual(detail["existing_file_count"], 2)
            self.assertEqual(detail["actual_bytes"], 8)
            self.assertTrue(detail["delivered"])
            self.assertEqual(detail["relative_path"], "youtube/channel/first.m4a")

            untracked = youtube_dir / "first.info.json"
            untracked.write_text("{}")
            result = store.purge_disk_resource(
                youtube_artifact_id,
                root,
                expected_revision=str(detail["resource_revision"]),
            )

            self.assertTrue(result["completed"])
            self.assertEqual(result["deleted_files"], 2)
            self.assertEqual(result["freed_bytes"], 8)
            self.assertFalse(youtube_path.exists())
            self.assertFalse(thumbnail_path.exists())
            self.assertTrue(untracked.exists())
            self.assertEqual(
                {
                    str(row["state"])
                    for row in store.conn.execute(
                        "SELECT state FROM artifacts WHERE media_id=?",
                        (youtube_id,),
                    )
                },
                {"purged"},
            )
            self.assertEqual(
                store.conn.execute(
                    "SELECT COUNT(*) FROM media_items WHERE id=?",
                    (youtube_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                store.conn.execute(
                    "SELECT COUNT(*) FROM deliveries WHERE media_id=?",
                    (youtube_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                store.conn.execute(
                    """
                    SELECT state FROM jobs
                    WHERE media_id=? AND target_key='telegram:@other'
                    """,
                    (youtube_id,),
                ).fetchone()["state"],
                "cancelled",
            )
            remaining = store.list_disk_resources(root)
            self.assertEqual(remaining["total"], 1)
            self.assertEqual(
                remaining["items"][0]["artifact_id"],
                twitch_artifact_id,
            )

    def test_disk_resource_purge_rejects_running_and_unsafe_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "busy"),
            )
            path = root / "busy.m4a"
            path.write_bytes(b"audio")
            download = store.claim_next_job(
                ("download",),
                owner="download",
                lease_seconds=60,
            )
            artifact_id = store.complete_download(
                download,
                path=path,
                size_bytes=5,
            )
            store.ensure_delivery_job(media_id, "telegram:@archive")
            delivery = store.claim_next_job(
                ("telegram_delivery",),
                owner="delivery",
                lease_seconds=60,
            )
            detail = store.get_disk_resource(artifact_id, root)

            with self.assertRaisesRegex(ValueError, "currently"):
                store.purge_disk_resource(
                    artifact_id,
                    root,
                    expected_revision=str(detail["resource_revision"]),
                )
            self.assertTrue(path.exists())
            self.assertEqual(
                store.get_artifact(media_id)["state"],
                "ready",
            )

            store.cancel_job(
                delivery,
                reason_code="test",
                error="test cleanup",
            )
            changed_at = (
                datetime.now(timezone.utc) + timedelta(seconds=1)
            ).isoformat()
            store.conn.execute(
                "UPDATE artifacts SET updated_at=? WHERE id=?",
                (changed_at, artifact_id),
            )
            store.conn.commit()
            with self.assertRaisesRegex(ValueError, "changed after confirmation"):
                store.purge_disk_resource(
                    artifact_id,
                    root,
                    expected_revision=str(detail["resource_revision"]),
                )
            self.assertTrue(path.exists())

            outside = Path(tmp) / "outside.m4a"
            outside.write_bytes(b"outside")
            changed_at = (
                datetime.now(timezone.utc) + timedelta(seconds=2)
            ).isoformat()
            store.conn.execute(
                "UPDATE artifacts SET path=?, updated_at=? WHERE id=?",
                (str(outside), changed_at, artifact_id),
            )
            store.conn.commit()
            unsafe_detail = store.get_disk_resource(artifact_id, root)
            with self.assertRaisesRegex(ValueError, "unsafe tracked path"):
                store.purge_disk_resource(
                    artifact_id,
                    root,
                    expected_revision=str(unsafe_detail["resource_revision"]),
                )
            self.assertTrue(outside.exists())

    def test_disk_resource_purge_failure_can_be_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "retry-purge"),
            )
            path = root / "retry-purge.m4a"
            path.write_bytes(b"audio")
            download = store.claim_next_job(
                ("download",),
                owner="download",
                lease_seconds=60,
            )
            artifact_id = store.complete_download(
                download,
                path=path,
                size_bytes=5,
            )
            detail = store.get_disk_resource(artifact_id, root)

            with mock.patch.object(
                store_module,
                "_unlink_tracked_file",
                side_effect=PermissionError("read-only filesystem"),
            ):
                failed = store.purge_disk_resource(
                    artifact_id,
                    root,
                    expected_revision=str(detail["resource_revision"]),
                )

            self.assertFalse(failed["completed"])
            self.assertEqual(failed["failed_files"], 1)
            self.assertTrue(path.exists())
            self.assertEqual(
                store.get_artifact(media_id)["state"],
                "purge_failed",
            )
            retried = store.purge_disk_resource(
                artifact_id,
                root,
                expected_revision=str(failed["resource_revision"]),
            )
            self.assertTrue(retried["completed"])
            self.assertFalse(path.exists())
            self.assertEqual(
                store.get_artifact(media_id)["state"],
                "purged",
            )

    def test_disk_resource_confirmation_covers_new_related_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "revision"),
            )
            master_path = root / "revision.m4a"
            master_path.write_bytes(b"master")
            download = store.claim_next_job(
                ("download",),
                owner="download",
                lease_seconds=60,
            )
            artifact_id = store.complete_download(
                download,
                path=master_path,
                size_bytes=6,
            )
            confirmed = store.get_disk_resource(artifact_id, root)

            thumbnail = root / "revision.jpg"
            thumbnail.write_bytes(b"thumbnail")
            store.record_artifact(
                media_id,
                role="thumbnail",
                path=thumbnail,
                size_bytes=9,
            )

            with self.assertRaisesRegex(ValueError, "changed after confirmation"):
                store.purge_disk_resource(
                    artifact_id,
                    root,
                    expected_revision=str(confirmed["resource_revision"]),
                )

            self.assertTrue(master_path.exists())
            self.assertTrue(thumbnail.exists())
            self.assertEqual(
                {
                    str(row["state"])
                    for row in store.conn.execute(
                        "SELECT state FROM artifacts WHERE media_id=?",
                        (media_id,),
                    )
                },
                {"ready"},
            )

            refreshed = store.get_disk_resource(artifact_id, root)
            replacement = root / "replacement.m4a"
            replacement.write_bytes(b"replacement master")
            replacement.replace(master_path)
            with self.assertRaisesRegex(ValueError, "changed after confirmation"):
                store.purge_disk_resource(
                    artifact_id,
                    root,
                    expected_revision=str(refreshed["resource_revision"]),
                )
            self.assertEqual(master_path.read_bytes(), b"replacement master")
            self.assertTrue(thumbnail.exists())

    def test_disk_resource_purge_rejects_path_shared_by_another_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            first_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "shared-first"),
            )
            second_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "shared-second"),
            )
            shared = root / "shared.m4a"
            shared.write_bytes(b"shared")
            first_job = store.claim_next_job(
                ("download",),
                owner="first",
                lease_seconds=60,
            )
            first_artifact = store.complete_download(
                first_job,
                path=shared,
                size_bytes=6,
            )
            second_job = store.claim_next_job(
                ("download",),
                owner="second",
                lease_seconds=60,
            )
            second_artifact = store.complete_download(
                second_job,
                path=shared,
                size_bytes=6,
            )
            detail = store.get_disk_resource(first_artifact, root)

            with self.assertRaisesRegex(ValueError, "another media item"):
                store.purge_disk_resource(
                    first_artifact,
                    root,
                    expected_revision=str(detail["resource_revision"]),
                )

            self.assertTrue(shared.exists())
            self.assertEqual(store.get_artifact(first_id)["state"], "ready")
            self.assertEqual(store.get_artifact(second_id)["state"], "ready")

            alias = root / "shared-alias.m4a"
            alias.symlink_to(shared)
            store.conn.execute(
                "UPDATE artifacts SET path=?, updated_at=? WHERE id=?",
                (str(alias), now_iso(), second_artifact),
            )
            store.conn.commit()
            with self.assertRaisesRegex(ValueError, "another media item"):
                store.purge_disk_resource(
                    first_artifact,
                    root,
                    expected_revision=str(detail["resource_revision"]),
                )
            self.assertTrue(shared.exists())
            self.assertTrue(alias.is_symlink())

    def test_disk_resource_reservation_blocks_late_shared_path_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            db_path = Path(tmp) / "state.db"
            store = Store(db_path)
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            first_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "reserved-first"),
            )
            shared = root / "reserved-shared.m4a"
            shared.write_bytes(b"shared")
            first_job = store.claim_next_job(
                ("download",),
                owner="first",
                lease_seconds=60,
            )
            first_artifact = store.complete_download(
                first_job,
                path=shared,
                size_bytes=6,
            )
            second_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "reserved-second"),
            )
            confirmed = store.get_disk_resource(first_artifact, root)
            second_store = Store(db_path)
            second_store.initialize()
            original_purge = Store._purge_reserved_files
            late_registration_attempted = False

            def inject_late_registration(
                active_store: Store,
                **kwargs,
            ):
                nonlocal late_registration_attempted
                late_registration_attempted = True
                with self.assertRaisesRegex(
                    RuntimeError,
                    "reserved for deletion",
                ):
                    second_store.record_artifact(
                        second_id,
                        role="master",
                        path=shared,
                        size_bytes=6,
                    )
                return original_purge(active_store, **kwargs)

            with mock.patch.object(
                Store,
                "_purge_reserved_files",
                new=inject_late_registration,
            ):
                result = store.purge_disk_resource(
                    first_artifact,
                    root,
                    expected_revision=str(
                        confirmed["resource_revision"]
                    ),
                )

            self.assertTrue(late_registration_attempted)
            self.assertTrue(result["completed"])
            self.assertFalse(shared.exists())
            self.assertEqual(store.get_artifact(first_id)["state"], "purged")
            self.assertIsNone(second_store.get_artifact(second_id))
            tombstone = store.conn.execute(
                """
                SELECT state, owner_pid, owner_start_id, finished_at
                FROM purge_path_reservations
                WHERE storage_key=?
                """,
                (str(shared.resolve()),),
            ).fetchone()
            self.assertIsNotNone(tombstone)
            self.assertEqual(
                tuple(tombstone)[:3],
                ("purged", 0, ""),
            )
            self.assertIsNotNone(tombstone["finished_at"])

    def test_disk_resource_purge_blocks_concurrent_writer_until_tombstoned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            db_path = Path(tmp) / "state.db"
            origin = Origin(
                "yt",
                "youtube",
                "uploads",
                "YT",
                "UC-1",
            )
            store = Store(db_path)
            store.initialize()
            store.upsert_origin(origin)
            first_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "concurrent-purge-first"),
            )
            shared = root / "concurrent-purge.m4a"
            shared.write_bytes(b"shared")
            first_job = store.claim_next_job(
                ("download",),
                owner="first",
                lease_seconds=60,
            )
            first_artifact = store.complete_download(
                first_job,
                path=shared,
                size_bytes=6,
            )
            second_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "concurrent-purge-second"),
            )
            confirmed = store.get_disk_resource(first_artifact, root)
            store.close()

            phase_two_paused = threading.Event()
            allow_purge = threading.Event()
            writer_ready = threading.Event()
            writer_started = threading.Event()
            writer_finished = threading.Event()
            purge_results: list[dict[str, object]] = []
            purge_errors: list[BaseException] = []
            writer_errors: list[BaseException] = []
            original_unlink = store_module._unlink_tracked_file

            def pause_before_unlink(
                path: Path,
                download_root: Path,
                expected: dict[str, object],
            ) -> int:
                phase_two_paused.set()
                if not allow_purge.wait(timeout=5):
                    raise TimeoutError("test did not release purge")
                return original_unlink(path, download_root, expected)

            def purge_worker() -> None:
                worker_store = Store(db_path)
                worker_store.initialize()
                try:
                    purge_results.append(
                        worker_store.purge_disk_resource(
                            first_artifact,
                            root,
                            expected_revision=str(
                                confirmed["resource_revision"]
                            ),
                        )
                    )
                except BaseException as exc:
                    purge_errors.append(exc)
                finally:
                    worker_store.close()

            def writer_worker() -> None:
                writer_store = Store(db_path)
                writer_store.initialize()
                writer_ready.set()
                try:
                    if not phase_two_paused.wait(timeout=5):
                        raise TimeoutError(
                            "purge did not reach phase two"
                        )
                    writer_started.set()
                    writer_store.record_artifact(
                        second_id,
                        role="master",
                        path=shared,
                        size_bytes=6,
                    )
                except BaseException as exc:
                    writer_errors.append(exc)
                finally:
                    writer_finished.set()
                    writer_store.close()

            writer_thread = threading.Thread(
                target=writer_worker,
                name="late-artifact-writer",
            )
            purge_thread = threading.Thread(
                target=purge_worker,
                name="disk-resource-purge",
            )
            writer_thread.start()
            self.assertTrue(writer_ready.wait(timeout=5))
            with mock.patch.object(
                store_module,
                "_unlink_tracked_file",
                side_effect=pause_before_unlink,
            ):
                purge_thread.start()
                phase_two_seen = phase_two_paused.wait(timeout=5)
                writer_seen = (
                    writer_started.wait(timeout=5)
                    if phase_two_seen
                    else False
                )
                writer_was_blocked = (
                    writer_seen
                    and not writer_finished.wait(timeout=0.2)
                )
                allow_purge.set()
                purge_thread.join(timeout=5)
                writer_thread.join(timeout=5)

            self.assertTrue(phase_two_seen)
            self.assertTrue(writer_seen)
            self.assertTrue(writer_was_blocked)
            self.assertFalse(purge_thread.is_alive())
            self.assertFalse(writer_thread.is_alive())
            self.assertEqual(purge_errors, [])
            self.assertEqual(len(purge_results), 1)
            self.assertTrue(purge_results[0]["completed"])
            self.assertEqual(len(writer_errors), 1)
            self.assertIsInstance(writer_errors[0], RuntimeError)
            self.assertIn(
                "reserved for deletion",
                str(writer_errors[0]),
            )
            self.assertFalse(shared.exists())

            verify_store = Store(db_path)
            verify_store.initialize()
            self.assertEqual(
                verify_store.get_artifact(first_id)["state"],
                "purged",
            )
            self.assertIsNone(verify_store.get_artifact(second_id))
            tombstone = verify_store.conn.execute(
                """
                SELECT state, owner_pid, owner_start_id, finished_at
                FROM purge_path_reservations
                WHERE storage_key=?
                """,
                (str(shared.resolve()),),
            ).fetchone()
            self.assertIsNotNone(tombstone)
            self.assertEqual(
                tuple(tombstone)[:3],
                ("purged", 0, ""),
            )
            self.assertIsNotNone(tombstone["finished_at"])

    def test_disk_resource_purge_rejects_parent_symlink_swap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            managed_dir = root / "managed"
            managed_dir.mkdir(parents=True)
            outside_dir = Path(tmp) / "outside"
            outside_dir.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "parent-swap"),
            )
            tracked = managed_dir / "parent-swap.m4a"
            tracked.write_bytes(b"tracked")
            outside = outside_dir / tracked.name
            outside.write_bytes(b"outside")
            download = store.claim_next_job(
                ("download",),
                owner="download",
                lease_seconds=60,
            )
            artifact_id = store.complete_download(
                download,
                path=tracked,
                size_bytes=7,
            )
            confirmed = store.get_disk_resource(artifact_id, root)
            renamed_dir = root / "managed-before-swap"
            original_unlink = store_module._unlink_tracked_file
            swapped = False

            def swap_parent_then_unlink(
                path: Path,
                download_root: Path,
                expected: dict[str, object],
            ) -> int:
                nonlocal swapped
                swapped = True
                managed_dir.rename(renamed_dir)
                managed_dir.symlink_to(
                    outside_dir,
                    target_is_directory=True,
                )
                return original_unlink(path, download_root, expected)

            with mock.patch.object(
                store_module,
                "_unlink_tracked_file",
                side_effect=swap_parent_then_unlink,
            ):
                result = store.purge_disk_resource(
                    artifact_id,
                    root,
                    expected_revision=str(
                        confirmed["resource_revision"]
                    ),
                )

            self.assertTrue(swapped)
            self.assertFalse(result["completed"])
            self.assertEqual(result["deleted_files"], 0)
            self.assertEqual(result["failed_files"], 1)
            self.assertEqual(outside.read_bytes(), b"outside")
            self.assertEqual(
                (renamed_dir / tracked.name).read_bytes(),
                b"tracked",
            )
            self.assertEqual(
                store.get_artifact(media_id)["state"],
                "purge_failed",
            )

    def test_live_segment_only_resource_is_listed_detailed_and_purged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("tw", "twitch", "vods", "Live ASMR", "42")
            )
            media_id, _ = store.upsert_discovered(
                "tw",
                candidate(
                    "twitch",
                    "live-segments-only",
                    kind="live_stream",
                ),
            )
            first_segment = root / "live-segment-1.ts"
            second_segment = root / "live-segment-2.ts"
            first_segment.write_bytes(b"one")
            second_segment.write_bytes(b"four")
            store.record_live_segment(
                media_id,
                path=first_segment,
                size_bytes=3,
            )
            store.record_live_segment(
                media_id,
                path=second_segment,
                size_bytes=4,
            )
            self.assertIsNone(store.get_artifact(media_id, "master"))
            segment_rows = store.conn.execute(
                """
                SELECT id, part_no
                FROM artifacts
                WHERE media_id=? AND role='live_segment'
                ORDER BY part_no
                """,
                (media_id,),
            ).fetchall()
            anchor_id = int(segment_rows[0]["id"])

            library = store.list_disk_resources(root)
            self.assertEqual(library["total"], 1)
            self.assertEqual(library["recorded_bytes"], 7)
            self.assertEqual(
                library["items"][0]["artifact_id"],
                anchor_id,
            )
            self.assertEqual(
                library["items"][0]["anchor_role"],
                "live_segment",
            )
            self.assertTrue(library["items"][0]["irreplaceable_live"])

            detail = store.get_disk_resource(anchor_id, root)
            self.assertIsNotNone(detail)
            self.assertEqual(detail["existing_file_count"], 2)
            self.assertEqual(detail["actual_bytes"], 7)
            self.assertEqual(
                [str(item["role"]) for item in detail["files"]],
                ["live_segment", "live_segment"],
            )

            result = store.purge_disk_resource(
                anchor_id,
                root,
                expected_revision=str(detail["resource_revision"]),
            )

            self.assertTrue(result["completed"])
            self.assertEqual(result["deleted_files"], 2)
            self.assertEqual(result["freed_bytes"], 7)
            self.assertFalse(first_segment.exists())
            self.assertFalse(second_segment.exists())
            self.assertEqual(
                {
                    str(row["state"])
                    for row in store.conn.execute(
                        "SELECT state FROM artifacts WHERE media_id=?",
                        (media_id,),
                    )
                },
                {"purged"},
            )
            self.assertIsNone(store.get_disk_resource(anchor_id, root))
            self.assertEqual(store.list_disk_resources(root)["total"], 0)

    def test_purged_download_job_stays_cancelled_after_rediscovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            db_path = Path(tmp) / "state.db"
            origin = Origin(
                "yt",
                "youtube",
                "uploads",
                "YT",
                "UC-1",
            )
            discovered = candidate("youtube", "purged-rediscovery")
            store = Store(db_path)
            store.initialize()
            store.upsert_origin(origin)
            media_id, _ = store.upsert_discovered("yt", discovered)
            path = root / "purged-rediscovery.m4a"
            path.write_bytes(b"audio")
            download = store.claim_next_job(
                ("download",),
                owner="download",
                lease_seconds=60,
            )
            artifact_id = store.complete_download(
                download,
                path=path,
                size_bytes=5,
            )
            store.requeue_download(
                media_id,
                reason="test pending redownload",
            )
            detail = store.get_disk_resource(artifact_id, root)
            result = store.purge_disk_resource(
                artifact_id,
                root,
                expected_revision=str(detail["resource_revision"]),
            )
            self.assertTrue(result["completed"])
            cancelled = store.conn.execute(
                """
                SELECT state, reason_code
                FROM jobs
                WHERE media_id=? AND job_type='download' AND target_key=''
                """,
                (media_id,),
            ).fetchone()
            self.assertEqual(
                tuple(cancelled),
                ("cancelled", "resource_purged"),
            )
            store.close()

            reopened = Store(db_path)
            reopened.initialize()
            reopened.upsert_origin(origin)
            rediscovered_id, created = reopened.upsert_discovered(
                "yt",
                discovered,
            )

            self.assertEqual(rediscovered_id, media_id)
            self.assertFalse(created)
            still_cancelled = reopened.conn.execute(
                """
                SELECT state, reason_code
                FROM jobs
                WHERE media_id=? AND job_type='download' AND target_key=''
                """,
                (media_id,),
            ).fetchone()
            self.assertEqual(
                tuple(still_cancelled),
                ("cancelled", "resource_purged"),
            )
            self.assertIsNone(
                reopened.claim_next_job(
                    ("download",),
                    owner="after-restart",
                    lease_seconds=60,
                )
            )
            self.assertEqual(
                reopened.get_artifact(media_id)["state"],
                "purged",
            )
            self.assertEqual(
                reopened.list_disk_resources(root)["total"],
                0,
            )

    def test_initialize_recovers_dead_owner_disk_purge_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            db_path = Path(tmp) / "state.db"
            store = Store(db_path)
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "dead-purge-owner"),
            )
            path = root / "dead-purge-owner.m4a"
            path.write_bytes(b"audio")
            download = store.claim_next_job(
                ("download",),
                owner="download",
                lease_seconds=60,
            )
            artifact_id = store.complete_download(
                download,
                path=path,
                size_bytes=5,
            )
            operation_id = "dead-purge-operation"
            requested_at = now_iso()
            metadata = {
                "local_purge": {
                    "operation_id": operation_id,
                    "requested_at": requested_at,
                    "previous_state": "ready",
                    "source": "telegram_panel",
                }
            }
            store.conn.execute(
                """
                UPDATE artifacts
                SET state='purging', metadata_json=?, updated_at=?
                WHERE id=?
                """,
                (
                    json.dumps(metadata, sort_keys=True),
                    requested_at,
                    artifact_id,
                ),
            )
            store.conn.execute(
                """
                INSERT INTO purge_path_reservations(
                  storage_key, media_id, operation_id, owner_pid,
                  owner_start_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(path.resolve()),
                    media_id,
                    operation_id,
                    0,
                    "dead",
                    requested_at,
                ),
            )
            store.conn.commit()
            store.close()

            reopened = Store(db_path)
            reopened.initialize()

            artifact = reopened.get_artifact(media_id)
            self.assertEqual(artifact["state"], "purge_failed")
            recovered_metadata = json.loads(
                str(artifact["metadata_json"])
            )["local_purge"]
            self.assertEqual(recovered_metadata["result"], "interrupted")
            self.assertIn(
                "service stopped",
                recovered_metadata["error"],
            )
            self.assertEqual(
                reopened.conn.execute(
                    "SELECT COUNT(*) FROM purge_path_reservations"
                ).fetchone()[0],
                0,
            )
            self.assertTrue(path.exists())
            detail = reopened.get_disk_resource(artifact_id, root)
            self.assertIsNotNone(detail)

            retried = reopened.purge_disk_resource(
                artifact_id,
                root,
                expected_revision=str(detail["resource_revision"]),
            )
            self.assertTrue(retried["completed"])
            self.assertFalse(path.exists())

    def test_initialize_recovers_crash_after_unlink_with_reusable_tombstone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            db_path = Path(tmp) / "state.db"
            store = Store(db_path)
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            resources: dict[str, dict[str, object]] = {}
            for scenario in ("missing", "restored"):
                media_id, _ = store.upsert_discovered(
                    "yt",
                    candidate(
                        "youtube",
                        f"crash-after-unlink-{scenario}",
                    ),
                )
                path = root / f"crash-after-unlink-{scenario}.m4a"
                path.write_bytes(b"audio")
                download = store.claim_next_job(
                    ("download",),
                    owner=f"download-{scenario}",
                    lease_seconds=60,
                )
                artifact_id = store.complete_download(
                    download,
                    path=path,
                    size_bytes=5,
                )
                operation_id = f"dead-after-unlink-{scenario}"
                requested_at = now_iso()
                metadata = {
                    "local_purge": {
                        "operation_id": operation_id,
                        "requested_at": requested_at,
                        "previous_state": "ready",
                        "source": "telegram_panel",
                    }
                }
                storage_key = str(path.resolve())
                store.conn.execute(
                    """
                    UPDATE artifacts
                    SET state='purging', metadata_json=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        json.dumps(metadata, sort_keys=True),
                        requested_at,
                        artifact_id,
                    ),
                )
                store.conn.execute(
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
                        0,
                        "dead",
                        requested_at,
                    ),
                )
                resources[scenario] = {
                    "artifact_id": artifact_id,
                    "media_id": media_id,
                    "operation_id": operation_id,
                    "path": path,
                    "storage_key": storage_key,
                }
            store.conn.commit()
            for resource in resources.values():
                resource["path"].unlink()
            store.close()

            reopened = Store(db_path)
            reopened.initialize()

            for scenario, resource in resources.items():
                with self.subTest(
                    scenario=scenario,
                    phase="recovery",
                ):
                    artifact = reopened.get_artifact(
                        int(resource["media_id"])
                    )
                    self.assertEqual(artifact["state"], "purge_failed")
                    tombstone = reopened.conn.execute(
                        """
                        SELECT operation_id, state, owner_pid,
                          owner_start_id, finished_at
                        FROM purge_path_reservations
                        WHERE storage_key=?
                        """,
                        (str(resource["storage_key"]),),
                    ).fetchone()
                    self.assertIsNotNone(tombstone)
                    self.assertEqual(
                        tuple(tombstone)[:4],
                        (
                            resource["operation_id"],
                            "purged",
                            0,
                            "",
                        ),
                    )
                    self.assertIsNotNone(tombstone["finished_at"])
                    self.assertFalse(resource["path"].exists())

            missing = resources["missing"]
            missing_detail = reopened.get_disk_resource(
                int(missing["artifact_id"]),
                root,
            )
            self.assertIsNotNone(missing_detail)
            missing_retry = reopened.purge_disk_resource(
                int(missing["artifact_id"]),
                root,
                expected_revision=str(
                    missing_detail["resource_revision"]
                ),
            )
            self.assertTrue(missing_retry["completed"])
            self.assertEqual(missing_retry["deleted_files"], 0)
            self.assertEqual(missing_retry["missing_files"], 1)

            restored = resources["restored"]
            restored_bytes = b"restored after recovery"
            restored["path"].write_bytes(restored_bytes)
            restored_detail = reopened.get_disk_resource(
                int(restored["artifact_id"]),
                root,
            )
            self.assertIsNotNone(restored_detail)
            self.assertEqual(restored_detail["existing_file_count"], 1)
            self.assertEqual(
                restored_detail["actual_bytes"],
                len(restored_bytes),
            )
            restored_retry = reopened.purge_disk_resource(
                int(restored["artifact_id"]),
                root,
                expected_revision=str(
                    restored_detail["resource_revision"]
                ),
            )
            self.assertTrue(restored_retry["completed"])
            self.assertEqual(restored_retry["deleted_files"], 1)
            self.assertEqual(restored_retry["missing_files"], 0)
            self.assertEqual(
                restored_retry["freed_bytes"],
                len(restored_bytes),
            )

            for scenario, resource in resources.items():
                with self.subTest(
                    scenario=scenario,
                    phase="retry",
                ):
                    self.assertFalse(resource["path"].exists())
                    self.assertEqual(
                        reopened.get_artifact(
                            int(resource["media_id"])
                        )["state"],
                        "purged",
                    )
                    final_tombstone = reopened.conn.execute(
                        """
                        SELECT state, owner_pid, owner_start_id,
                          finished_at
                        FROM purge_path_reservations
                        WHERE storage_key=?
                        """,
                        (str(resource["storage_key"]),),
                    ).fetchone()
                    self.assertIsNotNone(final_tombstone)
                    self.assertEqual(
                        tuple(final_tombstone)[:3],
                        ("purged", 0, ""),
                    )
                    self.assertIsNotNone(
                        final_tombstone["finished_at"]
                    )
                    self.assertEqual(
                        reopened.conn.execute(
                            """
                            SELECT COUNT(*)
                            FROM purge_path_reservations
                            WHERE storage_key=?
                            """,
                            (str(resource["storage_key"]),),
                        ).fetchone()[0],
                        1,
                    )

    def test_partial_purge_keeps_target_anchor_until_other_paths_succeed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UC-1")
            )
            media_id, _ = store.upsert_discovered(
                "yt",
                candidate("youtube", "anchor-last"),
            )
            anchor_path = root / "anchor-last.m4a"
            anchor_path.write_bytes(b"anchor")
            download = store.claim_next_job(
                ("download",),
                owner="download",
                lease_seconds=60,
            )
            anchor_id = store.complete_download(
                download,
                path=anchor_path,
                size_bytes=6,
            )
            extra_master_path = root / "anchor-last-part-1.m4a"
            extra_master_path.write_bytes(b"extra")
            store.record_artifact(
                media_id,
                role="master",
                part_no=1,
                path=extra_master_path,
                size_bytes=5,
            )
            detail = store.get_disk_resource(anchor_id, root)
            original_unlink = store_module._unlink_tracked_file

            def fail_extra_master(
                path: Path,
                download_root: Path,
                expected: dict[str, object],
            ) -> int:
                if path == extra_master_path.resolve():
                    raise PermissionError("extra master is read-only")
                return original_unlink(path, download_root, expected)

            with mock.patch.object(
                store_module,
                "_unlink_tracked_file",
                side_effect=fail_extra_master,
            ):
                failed = store.purge_disk_resource(
                    anchor_id,
                    root,
                    expected_revision=str(
                        detail["resource_revision"]
                    ),
                )

            self.assertFalse(failed["completed"])
            self.assertEqual(failed["deleted_files"], 0)
            self.assertEqual(failed["failed_files"], 2)
            self.assertTrue(anchor_path.exists())
            self.assertTrue(extra_master_path.exists())
            self.assertEqual(
                {
                    str(row["state"])
                    for row in store.conn.execute(
                        "SELECT state FROM artifacts WHERE media_id=?",
                        (media_id,),
                    )
                },
                {"purge_failed"},
            )
            self.assertIsNotNone(
                store.get_disk_resource(anchor_id, root)
            )

            retried = store.purge_disk_resource(
                anchor_id,
                root,
                expected_revision=str(failed["resource_revision"]),
            )
            self.assertTrue(retried["completed"])
            self.assertFalse(anchor_path.exists())
            self.assertFalse(extra_master_path.exists())

    def test_v1_migration_preserves_jobs_artifacts_and_deliveries(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "state.db"
            downloaded = tmp_path / "downloaded.m4a"
            uploaded = tmp_path / "uploaded.m4a"
            downloaded.write_bytes(b"one")
            uploaded.write_bytes(b"two")
            conn = sqlite3.connect(db_path)
            conn.executescript(LEGACY_SCHEMA)
            stamp = now_iso()
            rows = (
                ("stuck", "downloading", None, None, None),
                ("local", "downloaded", str(downloaded), 3, None),
                ("sent", "uploaded", str(uploaded), 3, 99),
                ("missing", "downloaded", str(tmp_path / "missing.m4a"), 3, None),
            )
            for video_id, status, file_path, file_size, message_id in rows:
                conn.execute(
                    """
                    INSERT INTO videos(
                      video_id, feed_id, feed_name, title, url, first_seen_at,
                      last_seen_at, status, attempts, file_path, file_size, telegram_message_id
                    ) VALUES (?, 'feed', 'Feed', ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (video_id, video_id, f"https://youtu.be/{video_id}", stamp, stamp, status, file_path, file_size, message_id),
                )
            conn.commit()
            conn.close()

            store = Store(db_path)
            store.initialize()
            self.assertEqual(store.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 2)
            self.assertIsNotNone(
                store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='videos_v1'").fetchone()
            )
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0], 4)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 3)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 1)
            stuck = store.conn.execute(
                """
                SELECT j.state, j.reason_code FROM jobs j JOIN media_items mi ON mi.id=j.media_id
                WHERE mi.external_id='stuck' AND j.job_type='download'
                """
            ).fetchone()
            self.assertEqual(tuple(stuck), ("retry", "worker_recovered"))
            missing = store.conn.execute(
                """
                SELECT j.state, a.state FROM jobs j
                JOIN media_items mi ON mi.id=j.media_id
                JOIN artifacts a ON a.media_id=mi.id AND a.role='master'
                WHERE mi.external_id='missing' AND j.job_type='download'
                """
            ).fetchone()
            self.assertEqual(tuple(missing), ("queued", "missing"))
            backups_before = list(tmp_path.glob("state.db.bak-v1-*"))
            self.assertEqual(len(backups_before), 1)
            self.assertEqual(backups_before[0].stat().st_mode & 0o777, 0o600)
            backup = sqlite3.connect(backups_before[0])
            try:
                self.assertIsNone(
                    backup.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
                    ).fetchone()
                )
                self.assertEqual(backup.execute("SELECT COUNT(*) FROM videos").fetchone()[0], 4)
            finally:
                backup.close()
            store.initialize()
            self.assertEqual(list(tmp_path.glob("state.db.bak-v1-*")), backups_before)

    def test_removed_config_origins_are_disabled_without_touching_control_origins(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(Origin("keep", "youtube", "uploads", "Keep", "UC-keep"))
            store.upsert_origin(Origin("removed", "youtube", "uploads", "Removed", "UC-removed"))
            store.upsert_origin(
                Origin("db:dynamic", "youtube", "uploads", "Dynamic", "UC-dynamic"),
                managed_by="control",
            )

            store.disable_missing_config_origins({"keep"})

            states = {
                row["id"]: bool(row["enabled"])
                for row in store.conn.execute("SELECT id, enabled FROM origins")
            }
            self.assertEqual(states, {"keep": True, "removed": False, "db:dynamic": True})

    def test_origin_source_identity_is_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(Origin("origin", "twitch", "vods", "A", "100"))

            with self.assertRaisesRegex(ValueError, "source identity is immutable"):
                store.upsert_origin(Origin("origin", "twitch", "vods", "B", "200"))

            row = store.conn.execute(
                "SELECT provider, kind, external_id FROM origins WHERE id='origin'"
            ).fetchone()
            self.assertEqual(tuple(row), ("twitch", "vods", "100"))

    def test_twitch_identity_allows_case_normalization_and_mode_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(
                Origin(
                    "origin",
                    "twitch",
                    "vods",
                    "A",
                    "ExampleStreamer",
                    options={"recording_mode": "vod"},
                )
            )

            store.upsert_origin(
                Origin(
                    "origin",
                    "TWITCH",
                    "VODS",
                    "A",
                    "examplestreamer",
                    options={"recording_mode": "live"},
                )
            )

            row = store.conn.execute(
                "SELECT provider, kind, external_id, options_json "
                "FROM origins WHERE id='origin'"
            ).fetchone()
            self.assertEqual(
                (row["provider"], row["kind"], row["external_id"]),
                ("TWITCH", "VODS", "examplestreamer"),
            )
            self.assertEqual(json.loads(row["options_json"])["recording_mode"], "live")

    def test_youtube_external_id_remains_case_sensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(Origin("origin", "youtube", "uploads", "A", "UCabc"))

            with self.assertRaisesRegex(ValueError, "source identity is immutable"):
                store.upsert_origin(
                    Origin("origin", "youtube", "uploads", "A", "UCABC")
                )

    def test_v1_initial_seed_ignored_rows_remain_ignored_after_rediscovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(LEGACY_SCHEMA)
            stamp = now_iso()
            for index, reason in enumerate(
                (
                    "initial feed seed ignored; kept latest entry only",
                    "initial official feed seed ignored",
                )
            ):
                conn.execute(
                    """
                    INSERT INTO videos(
                      video_id, feed_id, feed_name, title, url, first_seen_at,
                      last_seen_at, status, attempts, last_error
                    ) VALUES (?, 'feed', 'Feed', ?, ?, ?, ?, 'ignored', 0, ?)
                    """,
                    (f"old-{index}", f"Old {index}", f"https://youtu.be/old-{index}", stamp, stamp, reason),
                )
            conn.commit()
            conn.close()

            store = Store(db_path)
            store.initialize()
            for index in range(2):
                store.upsert_discovered("feed", candidate("youtube", f"old-{index}"))

            rows = store.conn.execute(
                "SELECT disposition, decision_code FROM origin_items ORDER BY media_id"
            ).fetchall()
            self.assertEqual([tuple(row) for row in rows], [("ignored", "initial_seed")] * 2)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
