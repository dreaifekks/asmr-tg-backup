from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

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
