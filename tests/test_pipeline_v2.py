from dataclasses import replace
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from ytb_tg_backup.config import load_config
from ytb_tg_backup.downloader import DownloadResult, ProbeResult
from ytb_tg_backup.models import MediaCandidate, Origin
from ytb_tg_backup.service import BackupService
from ytb_tg_backup.store import Store
from ytb_tg_backup.telegram import TelegramUploadError


DESTINATION = "telegram:@archive"


def candidate(external_id: str) -> MediaCandidate:
    return MediaCandidate(
        provider="youtube",
        content_kind="video",
        external_id=external_id,
        title=f"ASMR {external_id}",
        url=f"https://www.youtube.com/watch?v={external_id}",
        published_at=None,
    )


class PipelineV2Test(unittest.TestCase):
    def test_upcoming_youtube_probe_defers_without_consuming_failure_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            service, media_id = self._service_with_media(tmp_path, "upcoming")
            artifact_path = tmp_path / "unused.m4a"
            artifact_path.write_bytes(b"unused")
            downloader = self._downloader(artifact_path)
            downloader.probe.return_value = ProbeResult(
                live_status="is_upcoming",
                title="Scheduled ASMR",
            )
            telegram = mock.Mock()

            self.assertEqual(
                self._run_one(service, downloader, telegram, owner="download"),
                1,
            )

            job = service.store.conn.execute(
                """
                SELECT state, failure_count, reason_code
                FROM jobs
                WHERE media_id=? AND job_type='download'
                """,
                (media_id,),
            ).fetchone()
            self.assertEqual(tuple(job), ("retry", 0, "not_ready"))
            downloader.download.assert_not_called()
            telegram.upload.assert_not_called()
            service.store.close()

    def test_complete_download_atomically_creates_artifact_and_delivery_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = Store(tmp_path / "state.db")
            store.initialize()
            store.upsert_origin(Origin("yt", "youtube", "uploads", "ASMR", "UC-test"))

            media_id, _ = store.upsert_discovered("yt", candidate("success"))
            job = store.claim_next_job(("download",), owner="worker", lease_seconds=60)
            artifact_path = tmp_path / "success.m4a"
            artifact_path.write_bytes(b"audio")
            artifact_id = store.complete_download(
                job,
                path=artifact_path,
                size_bytes=5,
                delivery_targets=(DESTINATION,),
                delivery_max_failures=4,
            )

            artifact = store.conn.execute(
                "SELECT id, path, state FROM artifacts WHERE media_id=?",
                (media_id,),
            ).fetchone()
            download = store.conn.execute(
                "SELECT state FROM jobs WHERE id=?",
                (job.id,),
            ).fetchone()
            delivery = store.conn.execute(
                """
                SELECT state, max_failures, target_key FROM jobs
                WHERE media_id=? AND job_type='telegram_delivery'
                """,
                (media_id,),
            ).fetchone()
            self.assertEqual(tuple(artifact), (artifact_id, str(artifact_path), "ready"))
            self.assertEqual(download["state"], "succeeded")
            self.assertEqual(tuple(delivery), ("queued", 4, DESTINATION))

            rollback_media_id, _ = store.upsert_discovered("yt", candidate("rollback"))
            rollback_job = store.claim_next_job(("download",), owner="worker", lease_seconds=60)
            self.assertEqual(rollback_job.media_id, rollback_media_id)
            store.conn.executescript(
                """
                CREATE TRIGGER reject_delivery_job
                BEFORE INSERT ON jobs
                WHEN NEW.job_type='telegram_delivery'
                BEGIN
                  SELECT RAISE(ABORT, 'forced delivery insert failure');
                END;
                """
            )
            rollback_path = tmp_path / "rollback.m4a"
            rollback_path.write_bytes(b"audio")

            with self.assertRaises(sqlite3.DatabaseError):
                store.complete_download(
                    rollback_job,
                    path=rollback_path,
                    size_bytes=5,
                    delivery_targets=(DESTINATION,),
                    delivery_max_failures=4,
                )

            self.assertIsNone(store.get_artifact(rollback_media_id))
            rollback_state = store.conn.execute(
                "SELECT state FROM jobs WHERE id=?",
                (rollback_job.id,),
            ).fetchone()["state"]
            self.assertEqual(rollback_state, "running")
            store.close()

    def test_delivery_failure_and_retry_do_not_redownload(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, media_id = self._service_with_media(Path(tmp), "retry")
            artifact_path = Path(tmp) / "retry.m4a"
            artifact_path.write_bytes(b"audio")
            downloader = self._downloader(artifact_path)
            telegram = mock.Mock()
            telegram.upload.side_effect = [TelegramUploadError("temporary Telegram failure"), 321]

            self.assertEqual(self._run_one(service, downloader, telegram, owner="download"), 1)
            self.assertEqual(self._run_one(service, downloader, telegram, owner="delivery-1"), 1)
            failed_delivery = service.store.conn.execute(
                "SELECT state, failure_count FROM jobs WHERE media_id=? AND job_type='telegram_delivery'",
                (media_id,),
            ).fetchone()
            self.assertEqual(tuple(failed_delivery), ("retry", 1))
            self.assertEqual(downloader.download.call_count, 1)

            self.assertEqual(self._run_one(service, downloader, telegram, owner="delivery-2"), 1)
            completed_delivery = service.store.conn.execute(
                "SELECT state, failure_count FROM jobs WHERE media_id=? AND job_type='telegram_delivery'",
                (media_id,),
            ).fetchone()
            remote_id = service.store.conn.execute(
                "SELECT remote_id FROM deliveries WHERE media_id=? AND destination_key=?",
                (media_id, DESTINATION),
            ).fetchone()["remote_id"]
            self.assertEqual(tuple(completed_delivery), ("succeeded", 1))
            self.assertEqual(remote_id, "321")
            self.assertEqual(downloader.probe.call_count, 1)
            self.assertEqual(downloader.download.call_count, 1)
            self.assertEqual(telegram.upload.call_count, 2)
            service.store.close()

    def test_missing_artifact_requeues_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, media_id = self._service_with_media(Path(tmp), "missing")
            artifact_path = Path(tmp) / "missing.m4a"
            artifact_path.write_bytes(b"audio")
            downloader = self._downloader(artifact_path)
            telegram = mock.Mock()

            self.assertEqual(self._run_one(service, downloader, telegram, owner="download"), 1)
            artifact_path.unlink()
            self.assertEqual(self._run_one(service, downloader, telegram, owner="delivery"), 1)

            download = service.store.conn.execute(
                "SELECT state, reason_code FROM jobs WHERE media_id=? AND job_type='download'",
                (media_id,),
            ).fetchone()
            delivery = service.store.conn.execute(
                "SELECT state, reason_code FROM jobs WHERE media_id=? AND job_type='telegram_delivery'",
                (media_id,),
            ).fetchone()
            self.assertEqual(tuple(download), ("retry", "artifact_missing"))
            self.assertEqual(tuple(delivery), ("retry", "artifact_missing"))
            self.assertEqual(downloader.download.call_count, 1)
            telegram.upload.assert_not_called()

            requeued = service.store.claim_next_job(("download",), owner="redownload", lease_seconds=60)
            self.assertIsNotNone(requeued)
            self.assertEqual(requeued.media_id, media_id)
            service.store.close()

    def test_destination_change_migrates_pending_job_without_duplicate_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, media_id = self._service_with_media(Path(tmp), "destination")
            artifact_path = Path(tmp) / "destination.m4a"
            artifact_path.write_bytes(b"audio")
            download = service.store.claim_next_job(("download",), owner="seed", lease_seconds=60)
            old_destination = service.telegram_destination_key
            service.store.complete_download(
                download,
                path=artifact_path,
                size_bytes=5,
                delivery_targets=(old_destination,),
                delivery_max_failures=3,
            )
            new_config = replace(
                service.config,
                telegram=replace(service.config.telegram, chat_id="@new-archive"),
            )
            service.store.close()

            upgraded = BackupService(new_config)
            upgraded.initialize()
            telegram = mock.Mock()
            telegram.upload.return_value = 456
            downloader = self._downloader(artifact_path)

            self.assertEqual(self._run_one(upgraded, downloader, telegram, owner="delivery"), 1)
            self.assertEqual(self._run_one(upgraded, downloader, telegram, owner="no-duplicate"), 0)
            telegram.upload.assert_called_once()
            target = upgraded.store.conn.execute(
                "SELECT target_key, state FROM jobs WHERE media_id=? AND job_type='telegram_delivery'",
                (media_id,),
            ).fetchone()
            delivered = upgraded.store.conn.execute(
                "SELECT destination_key FROM deliveries WHERE media_id=?",
                (media_id,),
            ).fetchone()
            self.assertEqual(tuple(target), ("telegram:@new-archive", "succeeded"))
            self.assertEqual(delivered["destination_key"], "telegram:@new-archive")
            upgraded.store.close()

    def test_timeout_and_non_json_delivery_results_become_unclaimable_uncertain_jobs(self):
        failures = {
            "timeout": subprocess.TimeoutExpired(cmd="curl", timeout=7200),
            "non-json": subprocess.CompletedProcess(
                ["curl"],
                0,
                stdout="upstream returned HTML",
                stderr="",
            ),
        }
        for label, curl_result in failures.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                service, media_id = self._service_with_media(Path(tmp), label)
                artifact_path = Path(tmp) / f"{label}.m4a"
                artifact_path.write_bytes(b"audio")
                download = service.store.claim_next_job(("download",), owner="seed", lease_seconds=60)
                service.store.complete_download(
                    download,
                    path=artifact_path,
                    size_bytes=5,
                    delivery_targets=(DESTINATION,),
                    delivery_max_failures=3,
                )
                downloader = self._downloader(artifact_path)

                patch_value = {"side_effect": curl_result} if isinstance(curl_result, BaseException) else {"return_value": curl_result}
                with (
                    mock.patch("ytb_tg_backup.telegram.shutil.which", return_value="/usr/bin/curl"),
                    mock.patch("ytb_tg_backup.telegram.subprocess.run", **patch_value),
                ):
                    self.assertEqual(
                        self._run_one(service, downloader, service.telegram, owner=f"delivery-{label}"),
                        1,
                    )

                delivery = service.store.conn.execute(
                    "SELECT state, reason_code, failure_count FROM jobs WHERE media_id=? AND job_type='telegram_delivery'",
                    (media_id,),
                ).fetchone()
                self.assertEqual(tuple(delivery), ("uncertain", "delivery_uncertain", 0))
                self.assertIsNone(
                    service.store.claim_next_job(
                        ("telegram_delivery",),
                        owner="automatic-retry",
                        lease_seconds=60,
                    )
                )
                self.assertEqual(
                    service.store.conn.execute("SELECT COUNT(*) FROM deliveries WHERE media_id=?", (media_id,)).fetchone()[0],
                    0,
                )
                service.store.close()

    def test_post_send_persistence_failure_never_requeues_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, media_id = self._service_with_media(Path(tmp), "persist-failure")
            artifact_path = Path(tmp) / "persist-failure.m4a"
            artifact_path.write_bytes(b"audio")
            downloader = self._downloader(artifact_path)
            telegram = mock.Mock()
            telegram.upload.return_value = 777

            self.assertEqual(self._run_one(service, downloader, telegram, owner="download"), 1)
            with mock.patch.object(
                service.store,
                "complete_delivery",
                side_effect=sqlite3.OperationalError("simulated persistence failure"),
            ):
                self.assertEqual(self._run_one(service, downloader, telegram, owner="delivery"), 1)

            row = service.store.conn.execute(
                "SELECT state, reason_code FROM jobs WHERE media_id=? AND job_type='telegram_delivery'",
                (media_id,),
            ).fetchone()
            self.assertEqual(tuple(row), ("uncertain", "delivery_uncertain"))
            self.assertIsNone(
                service.store.claim_next_job(
                    ("telegram_delivery",), owner="no-resend", lease_seconds=60
                )
            )
            telegram.upload.assert_called_once()
            service.store.close()

    def test_video_delivery_keeps_video_master_instead_of_deriving_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = self._service_with_media(Path(tmp), "twitch-video")
            service.config = replace(
                service.config,
                telegram=replace(service.config.telegram, media_type="video"),
            )
            master_path = Path(tmp) / "twitch-video.mp4"
            master_path.write_bytes(b"video")
            downloader = self._downloader(master_path)
            telegram = mock.Mock()
            telegram.upload.return_value = 888

            self.assertEqual(self._run_one(service, downloader, telegram, owner="download"), 1)
            self.assertEqual(self._run_one(service, downloader, telegram, owner="delivery"), 1)

            downloader.shrink_audio_for_upload.assert_not_called()
            self.assertEqual(telegram.upload.call_args.args[0], master_path)
            service.store.close()

    def test_delivery_passes_media_published_at_to_telegram(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, media_id = self._service_with_media(Path(tmp), "dated-live")
            service.store.conn.execute(
                "UPDATE media_items SET published_at=? WHERE id=?",
                ("2026-07-29T14:08:51Z", media_id),
            )
            master_path = Path(tmp) / "twitch_316244257650.live-merged.mp4"
            master_path.write_bytes(b"video")
            downloader = self._downloader(master_path)
            telegram = mock.Mock()
            telegram.upload.return_value = 889

            self.assertEqual(self._run_one(service, downloader, telegram, owner="download"), 1)
            self.assertEqual(self._run_one(service, downloader, telegram, owner="delivery"), 1)

            self.assertEqual(
                telegram.upload.call_args.kwargs["published_at"],
                "2026-07-29T14:08:51Z",
            )
            service.store.close()

    def test_worker_uses_its_thread_local_store_for_source_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            service, media_id = self._service_with_media(tmp_path, "thread-local")
            artifact_path = tmp_path / "thread-local.m4a"
            artifact_path.write_bytes(b"audio")
            downloader = self._downloader(artifact_path)
            errors: list[BaseException] = []

            def process_in_worker_thread() -> None:
                worker_store = Store(service.config.db_path)
                worker_store.initialize()
                try:
                    service._process_available(
                        worker_store,
                        downloader,
                        mock.Mock(),
                        owner="thread-worker",
                        limit=1,
                    )
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    worker_store.close()

            worker = threading.Thread(target=process_in_worker_thread)
            worker.start()
            worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            row = service.store.conn.execute(
                "SELECT state, failure_count FROM jobs WHERE media_id=? AND job_type='download'",
                (media_id,),
            ).fetchone()
            self.assertEqual(tuple(row), ("succeeded", 0))
            service.store.close()

    def _service_with_media(self, tmp_path: Path, external_id: str) -> tuple[BackupService, int]:
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            f"""
[app]
data_dir = "{tmp_path}"
download_delay_seconds = 0
retry_seconds = 0
max_attempts = 3
job_lease_seconds = 60

[download]
write_thumbnail = false

[telegram]
enabled = true
bot_token = "token"
chat_id = "@archive"
max_upload_bytes = 1000000

[[origins]]
id = "yt"
provider = "youtube"
kind = "uploads"
name = "ASMR"
external_id = "UC-test"
bootstrap = "all"
""".strip()
        )
        service = BackupService(load_config(config_path))
        service.initialize()
        media_id, _ = service.store.upsert_discovered("yt", candidate(external_id), max_failures=3)
        return service, media_id

    @staticmethod
    def _downloader(artifact_path: Path) -> mock.Mock:
        downloader = mock.Mock()
        downloader.probe.return_value = ProbeResult(live_status=None, title="ASMR archive")
        downloader.download.return_value = DownloadResult(
            file_path=artifact_path,
            file_size=artifact_path.stat().st_size,
        )
        downloader.shrink_audio_for_upload.return_value = DownloadResult(
            file_path=artifact_path,
            file_size=artifact_path.stat().st_size,
        )
        downloader.prepare_thumbnail_for_upload.return_value = None
        return downloader

    @staticmethod
    def _run_one(service: BackupService, downloader: mock.Mock, telegram: object, *, owner: str) -> int:
        return service._process_available(
            service.store,
            downloader,
            telegram,
            owner=owner,
            limit=1,
        )


if __name__ == "__main__":
    unittest.main()
