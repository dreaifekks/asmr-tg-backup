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


def candidate(provider: str, external_id: str, *, kind: str = "video") -> MediaCandidate:
    return MediaCandidate(
        provider=provider,
        content_kind=kind,
        external_id=external_id,
        title=f"{provider} {external_id}",
        url=f"https://example.invalid/{provider}/{external_id}",
        published_at=None,
    )


class StoreV2Test(unittest.TestCase):
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

    def test_deleting_last_eligible_origin_cancels_its_pending_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            origin = Origin(
                "db:youtube:uploads:member",
                "youtube",
                "uploads",
                "Member",
                "UC-member",
            )
            store.upsert_control_origin(origin, created_by="123")
            store.upsert_discovered(
                origin.id,
                candidate("youtube", "member-video"),
            )

            self.assertTrue(store.delete_control_origin(origin.id))
            row = store.conn.execute(
                "SELECT state, reason_code FROM jobs WHERE job_type='download'"
            ).fetchone()

            self.assertEqual(tuple(row), ("cancelled", "origin_deleted"))
            self.assertEqual(store.media_origins(1), [])

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
