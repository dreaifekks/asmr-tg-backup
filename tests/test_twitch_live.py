from dataclasses import replace
import json
from pathlib import Path
import signal
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from ytb_tg_backup.config import TwitchConfig, load_config
from ytb_tg_backup.downloader import (
    DownloadCancelled,
    DownloadResult,
    Downloader,
    LiveDownloadError,
    ProbeResult,
)
from ytb_tg_backup.models import DiscoveryResult, MediaCandidate, Origin
from ytb_tg_backup.service import BackupService, _LeaseHeartbeat
from ytb_tg_backup.sources import SourceError, TwitchHelixSource
from ytb_tg_backup.store import Store


def _live_origin() -> Origin:
    return Origin(
        id="twitch-live",
        provider="twitch",
        kind="vods",
        name="ASMR Twitch",
        external_id="12345",
        bootstrap="all",
        options={"recording_mode": "live"},
    )


def _live_stream(stream_id: str = "98765") -> dict[str, object]:
    return {
        "id": stream_id,
        "user_id": "12345",
        "user_login": "example_streamer",
        "user_name": "YumeNikkiCirus",
        "game_id": "509658",
        "game_name": "Just Chatting",
        "title": "ASMR live",
        "viewer_count": 42,
        "started_at": "2026-07-25T12:34:56Z",
        "language": "ja",
        "thumbnail_url": "https://static-cdn.example/live.jpg",
        "tag_ids": [],
        "tags": ["ASMR"],
        "is_mature": False,
    }


def _live_candidate(stream_id: str = "98765") -> MediaCandidate:
    return MediaCandidate(
        provider="twitch",
        content_kind="live_stream",
        external_id=stream_id,
        title="ASMR live",
        url="https://www.twitch.tv/example_streamer",
        published_at="2026-07-25T12:34:56Z",
        live_status="is_live",
        metadata={
            "recording_mode": "live",
            "stream_id": stream_id,
            "broadcaster_id": "12345",
            "user_login": "example_streamer",
        },
    )


def _vod_candidate(stream_id: str = "98765") -> MediaCandidate:
    return MediaCandidate(
        provider="twitch",
        content_kind="vod",
        external_id=f"vod-{stream_id}",
        title="ASMR archived VOD",
        url=f"https://www.twitch.tv/videos/vod-{stream_id}",
        published_at="2026-07-25T14:00:00Z",
        metadata={"stream_id": stream_id},
    )


class TwitchLiveConfigTest(unittest.TestCase):
    def test_live_recording_defaults_to_vod_with_safe_polling_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("")

            config = load_config(path)

        self.assertEqual(config.twitch.recording_mode, "vod")
        self.assertEqual(config.twitch.live_poll_interval_seconds, 30)
        self.assertEqual(config.twitch.live_retry_seconds, 15)
        self.assertEqual(config.twitch.live_worker_count, 1)
        self.assertEqual(config.twitch.live_download_timeout_seconds, 0)

    def test_live_recording_parameters_and_origin_override_are_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
[twitch]
recording_mode = "LIVE"
live_poll_interval_seconds = 12
live_retry_seconds = 7
live_worker_count = 2
live_download_timeout_seconds = 14400

[[origins]]
id = "twitch-live"
provider = "twitch"
kind = "vods"
name = "ASMR Twitch"
external_id = "example_streamer"
recording_mode = " LIVE "
""".strip()
            )

            config = load_config(path)

        self.assertEqual(config.twitch.recording_mode, "live")
        self.assertEqual(config.twitch.live_poll_interval_seconds, 12)
        self.assertEqual(config.twitch.live_retry_seconds, 7)
        self.assertEqual(config.twitch.live_worker_count, 2)
        self.assertEqual(config.twitch.live_download_timeout_seconds, 14400)
        self.assertEqual(config.origins[0].options["recording_mode"], "live")

    def test_invalid_global_or_origin_recording_mode_is_rejected(self):
        cases = {
            "global": """
[twitch]
recording_mode = "both"
""",
            "origin": """
[[origins]]
id = "twitch-live"
provider = "twitch"
kind = "vods"
external_id = "example_streamer"
recording_mode = "archive"
""",
            "non-vod-origin": """
[[origins]]
id = "twitch-highlights"
provider = "twitch"
kind = "highlights"
external_id = "example_streamer"
recording_mode = "live"
""",
        }
        for label, content in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.toml"
                path.write_text(content.strip())

                with self.assertRaisesRegex(ValueError, "recording_mode"):
                    load_config(path)


class TwitchLiveSourceTest(unittest.TestCase):
    def setUp(self):
        self.source = TwitchHelixSource(
            TwitchConfig(
                client_id="client",
                access_token="token",
                recording_mode="live",
            )
        )

    def test_online_stream_is_discovered_from_helix_streams(self):
        payload = {"data": [_live_stream()], "pagination": {}}

        with mock.patch.object(self.source, "_api_json", return_value=payload) as api_json:
            result = self.source.discover(_live_origin())

        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertEqual(item.provider, "twitch")
        self.assertEqual(item.content_kind, "live_stream")
        self.assertEqual(item.external_id, "98765")
        self.assertEqual(item.url, "https://www.twitch.tv/example_streamer")
        self.assertEqual(item.live_status, "is_live")
        self.assertEqual(item.metadata["recording_mode"], "live")
        self.assertEqual(item.metadata["stream_id"], "98765")
        self.assertEqual(
            json.loads(result.cursor),
            {
                "external_id": "98765",
                "published_at": "2026-07-25T12:34:56Z",
                "version": 1,
            },
        )
        api_json.assert_called_once_with("streams", {"user_id": "12345", "first": "1"})

    def test_offline_stream_returns_no_items_and_keeps_checkpoint(self):
        checkpoint = json.dumps(
            {
                "external_id": "98765",
                "published_at": "2026-07-25T12:34:56Z",
                "version": 1,
            }
        )

        with mock.patch.object(
            self.source,
            "_api_json",
            return_value={"data": [], "pagination": {}},
        ) as api_json:
            result = self.source.discover(_live_origin(), checkpoint)

        self.assertEqual(result.items, [])
        self.assertEqual(result.cursor, checkpoint)
        api_json.assert_called_once_with("streams", {"user_id": "12345", "first": "1"})

    def test_same_stream_is_rechecked_but_media_and_job_remain_deduplicated(self):
        payload = {"data": [_live_stream()], "pagination": {}}

        with mock.patch.object(self.source, "_api_json", return_value=payload):
            first = self.source.discover(_live_origin())
            second = self.source.discover(_live_origin(), first.cursor)

        self.assertEqual([item.external_id for item in first.items], ["98765"])
        self.assertEqual([item.external_id for item in second.items], ["98765"])
        self.assertEqual(second.cursor, first.cursor)

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(_live_origin())
            for item in (*first.items, *second.items):
                store.upsert_discovered(
                    "twitch-live",
                    item,
                    job_payload={
                        "download_lane": "live",
                        "recording_mode": "live",
                    },
                )

            media_count = store.conn.execute(
                """
                SELECT COUNT(*) FROM media_items
                WHERE provider='twitch' AND content_kind='live_stream'
                  AND external_id='98765'
                """
            ).fetchone()[0]
            jobs = store.conn.execute(
                "SELECT payload_json FROM jobs WHERE job_type='download'"
            ).fetchall()
            store.close()

        self.assertEqual(media_count, 1)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(json.loads(jobs[0]["payload_json"])["download_lane"], "live")


class TwitchLiveStoreTest(unittest.TestCase):
    def test_recording_mode_change_resets_checkpoint_and_retry_backoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(_live_origin())
            store.record_origin_poll_success(
                "twitch-live",
                cursor='{"external_id":"old-vod"}',
            )
            store.record_origin_poll_failure(
                "twitch-live",
                error_code="temporary",
                error="retry later",
                retry_seconds=900,
            )

            self.assertFalse(store.reconcile_origin_poll_mode("twitch-live", "vod"))
            self.assertEqual(
                store.get_origin_checkpoint("twitch-live"),
                '{"external_id":"old-vod"}',
            )
            self.assertFalse(store.origin_poll_due("twitch-live"))

            self.assertTrue(store.reconcile_origin_poll_mode("twitch-live", "live"))
            self.assertIsNone(store.get_origin_checkpoint("twitch-live"))
            self.assertTrue(store.origin_poll_due("twitch-live"))
            self.assertFalse(store.reconcile_origin_poll_mode("twitch-live", "live"))

            store.record_origin_poll_success(
                "twitch-live",
                cursor='{"external_id":"live-stream"}',
            )
            self.assertTrue(store.reconcile_origin_poll_mode("twitch-live", "vod"))
            self.assertIsNone(store.get_origin_checkpoint("twitch-live"))
            self.assertTrue(store.origin_poll_due("twitch-live"))
            store.close()

    def test_live_segment_allocation_is_atomic_across_store_connections(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "state.db"
            first_path = tmp_path / "first.mp4"
            second_path = tmp_path / "second.mp4"
            first_path.write_bytes(b"first")
            second_path.write_bytes(b"second")
            writer = Store(db_path)
            writer.initialize()
            writer.upsert_origin(_live_origin())
            media_id, _ = writer.upsert_discovered(
                "twitch-live",
                _live_candidate(),
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            ready = threading.Event()
            start = threading.Event()
            finished = threading.Event()
            errors: list[BaseException] = []

            def write_second_segment() -> None:
                contender = Store(db_path)
                try:
                    contender.initialize()
                    ready.set()
                    start.wait(timeout=5)
                    contender.record_live_segment(
                        media_id,
                        path=second_path,
                        size_bytes=second_path.stat().st_size,
                        metadata={"attempt_order": 200},
                    )
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    contender.close()
                    finished.set()

            thread = threading.Thread(target=write_second_segment)
            thread.start()
            self.assertTrue(ready.wait(timeout=5))
            writer.conn.execute("BEGIN IMMEDIATE")
            writer.conn.execute(
                """
                INSERT INTO artifacts(
                  media_id, role, part_no, path, size_bytes, state,
                  metadata_json, created_at, updated_at
                ) VALUES (?, 'live_segment', 0, ?, ?, 'ready', ?, ?, ?)
                """,
                (
                    media_id,
                    str(first_path),
                    first_path.stat().st_size,
                    json.dumps({"attempt_order": 100}),
                    "2026-07-25T12:00:00+00:00",
                    "2026-07-25T12:00:00+00:00",
                ),
            )
            start.set()
            time.sleep(0.05)
            writer.conn.commit()
            self.assertTrue(finished.wait(timeout=5))
            thread.join(timeout=5)

            rows = writer.conn.execute(
                """
                SELECT part_no, path FROM artifacts
                WHERE media_id=? AND role='live_segment'
                ORDER BY part_no
                """,
                (media_id,),
            ).fetchall()
            self.assertEqual(errors, [])
            self.assertEqual(
                [(row["part_no"], row["path"]) for row in rows],
                [(0, str(first_path)), (1, str(second_path))],
            )
            writer.close()

    def test_live_segment_paths_follow_attempt_order_not_registration_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = Store(tmp_path / "state.db")
            store.initialize()
            store.upsert_origin(_live_origin())
            media_id, _ = store.upsert_discovered(
                "twitch-live",
                _live_candidate(),
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            later = tmp_path / "later.mp4"
            earlier = tmp_path / "earlier.mp4"
            later.write_bytes(b"later")
            earlier.write_bytes(b"earlier")

            self.assertEqual(
                store.record_live_segment(
                    media_id,
                    path=later,
                    size_bytes=later.stat().st_size,
                    metadata={"attempt_order": 200},
                ),
                0,
            )
            self.assertEqual(
                store.record_live_segment(
                    media_id,
                    path=earlier,
                    size_bytes=earlier.stat().st_size,
                    metadata={"attempt_order": 100},
                ),
                1,
            )
            self.assertEqual(
                store.live_segment_paths(media_id),
                [earlier, later],
            )
            store.close()

    def test_standard_and_live_download_lanes_only_claim_their_own_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(_live_origin())
            live_media_id, _ = store.upsert_discovered(
                "twitch-live",
                _live_candidate(),
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            vod_media_id, _ = store.upsert_discovered(
                "twitch-live",
                MediaCandidate(
                    provider="twitch",
                    content_kind="vod",
                    external_id="vod-1",
                    title="ASMR VOD",
                    url="https://www.twitch.tv/videos/vod-1",
                    published_at="2026-07-25T14:00:00Z",
                ),
            )

            standard_job = store.claim_next_job(
                ("download",),
                owner="standard-worker",
                lease_seconds=60,
                download_lane="standard",
            )
            self.assertIsNotNone(standard_job)
            self.assertEqual(standard_job.media_id, vod_media_id)
            self.assertIsNone(
                store.claim_next_job(
                    ("download",),
                    owner="another-standard-worker",
                    lease_seconds=60,
                    download_lane="standard",
                )
            )

            live_job = store.claim_next_job(
                ("download",),
                owner="live-worker",
                lease_seconds=60,
                download_lane="live",
            )
            self.assertIsNotNone(live_job)
            self.assertEqual(live_job.media_id, live_media_id)
            self.assertEqual(live_job.payload["download_lane"], "live")
            self.assertIsNone(
                store.claim_next_job(
                    ("download",),
                    owner="another-live-worker",
                    lease_seconds=60,
                    download_lane="live",
                )
            )
            store.close()


class TwitchLiveDownloaderTest(unittest.TestCase):
    def test_live_download_forces_current_position_and_resilient_hls_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.toml"
            config_path.write_text(f'[app]\ndata_dir = "{tmp_path}"')
            downloader = Downloader(load_config(config_path), mock.Mock())
            media_path = tmp_path / "recording.m4a"
            media_path.write_bytes(b"audio")
            completed = subprocess.CompletedProcess(
                ["yt-dlp"],
                0,
                stdout=f"{media_path}\n",
                stderr="",
            )

            with mock.patch.object(
                downloader,
                "_run_live_download",
                return_value=completed,
            ) as run_live:
                downloader.download(
                    "98765",
                    "https://www.twitch.tv/example_streamer",
                    provider="twitch",
                    live=True,
                )

        command = run_live.call_args.args[0]
        self.assertIn("--no-live-from-start", command)
        self.assertIn("--hls-use-mpegts", command)
        self.assertIn("--no-part", command)
        self.assertIn("--no-download-archive", command)
        self.assertIn("--no-progress", command)
        self.assertIn("--no-match-filters", command)
        self.assertEqual(
            command[command.index("--match-filters") + 1],
            "id = 98765",
        )
        self.assertEqual(command[command.index("--retries") + 1], "infinite")
        self.assertEqual(command[command.index("--fragment-retries") + 1], "infinite")
        downloader_args = command[command.index("--downloader-args") + 1]
        self.assertIn("ffmpeg_i:-reconnect 1", downloader_args)
        self.assertIn("-reconnect_on_network_error 1", downloader_args)
        output_template = command[command.index("--output") + 1]
        self.assertIn(".live-", output_template)

    def test_cancel_event_terminates_the_live_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.toml"
            config_path.write_text(f'[app]\ndata_dir = "{tmp_path}"')
            downloader = Downloader(load_config(config_path), mock.Mock())
            cancelled = threading.Event()
            cancelled.set()
            process = mock.Mock()
            process.pid = 4321
            process.poll.return_value = None
            process.communicate.side_effect = [
                subprocess.TimeoutExpired(cmd=["yt-dlp"], timeout=1),
                ("", ""),
            ]
            with (
                mock.patch(
                    "ytb_tg_backup.downloader.subprocess.Popen",
                    return_value=process,
                ),
                mock.patch("ytb_tg_backup.downloader.os.killpg") as killpg,
            ):
                with self.assertRaises(DownloadCancelled):
                    downloader._run_live_download(
                        ["yt-dlp", "https://www.twitch.tv/example_streamer"],
                        cancel_events=(cancelled,),
                    )

        killpg.assert_called_once_with(4321, signal.SIGINT)
        self.assertEqual(
            process.communicate.call_args_list,
            [mock.call(timeout=1), mock.call(timeout=12)],
        )

    def test_twitch_live_probe_classifies_offline_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.toml"
            config_path.write_text(f'[app]\ndata_dir = "{tmp_path}"')
            downloader = Downloader(load_config(config_path), mock.Mock())
            error = subprocess.CalledProcessError(
                1,
                ["yt-dlp"],
                stderr="ERROR: [twitch:stream] example_streamer is offline",
            )

            with mock.patch(
                "ytb_tg_backup.downloader.subprocess.run",
                side_effect=error,
            ):
                result = downloader.probe(
                    "https://www.twitch.tv/example_streamer",
                    provider="twitch",
                    live=True,
                )

        self.assertEqual(result.live_status, "not_live")
        self.assertIsNone(result.external_id)

    def test_merge_live_audio_normalizes_interrupted_ts_and_clean_m4a(self):
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if ffmpeg is None or ffprobe is None:
            self.skipTest("ffmpeg and ffprobe are required")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.toml"
            config_path.write_text(
                f"""
[app]
data_dir = "{tmp_path}"

[download.provider_profiles.twitch]
extract_audio = true
""".strip()
            )
            raw_ts = tmp_path / "interrupted-audio.mp4"
            clean_m4a = tmp_path / "clean-audio.m4a"
            for output, output_args in (
                (raw_ts, ["-f", "mpegts"]),
                (clean_m4a, ["-movflags", "+faststart"]),
            ):
                subprocess.run(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        "sine=frequency=440:sample_rate=48000",
                        "-t",
                        "0.4",
                        "-c:a",
                        "aac",
                        *output_args,
                        str(output),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            raw_ts.write_bytes(raw_ts.read_bytes()[:-188])
            downloader = Downloader(load_config(config_path), mock.Mock())

            result = downloader.merge_live_segments(
                "audio-stream",
                [raw_ts, clean_m4a],
                provider="twitch",
            )

            streams = json.loads(
                subprocess.run(
                    [
                        ffprobe,
                        "-v",
                        "error",
                        "-show_entries",
                        "stream=codec_type,codec_name",
                        "-of",
                        "json",
                        str(result.file_path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )["streams"]
            self.assertEqual(result.file_path.suffix, ".m4a")
            self.assertGreater(result.file_size, 0)
            self.assertEqual(
                [(stream["codec_type"], stream["codec_name"]) for stream in streams],
                [("audio", "aac")],
            )

    def test_merge_live_video_normalizes_interrupted_ts_and_clean_mp4(self):
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if ffmpeg is None or ffprobe is None:
            self.skipTest("ffmpeg and ffprobe are required")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.toml"
            config_path.write_text(
                f"""
[app]
data_dir = "{tmp_path}"

[download.provider_profiles.twitch]
format = "bestvideo+bestaudio/best"
merge_output_format = "mp4"
extract_audio = false
""".strip()
            )
            raw_ts = tmp_path / "interrupted-video.mp4"
            clean_mp4 = tmp_path / "clean-video.mp4"
            base_command = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=160x90:rate=10",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=880:sample_rate=48000",
                "-t",
                "0.4",
                "-shortest",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
            ]
            try:
                subprocess.run(
                    [*base_command, "-f", "mpegts", str(raw_ts)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                subprocess.run(
                    [*base_command, "-movflags", "+faststart", str(clean_mp4)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as exc:
                self.skipTest(f"ffmpeg H.264 fixture unavailable: {exc.stderr}")
            raw_ts.write_bytes(raw_ts.read_bytes()[:-188])
            downloader = Downloader(load_config(config_path), mock.Mock())

            result = downloader.merge_live_segments(
                "video-stream",
                [raw_ts, clean_mp4],
                provider="twitch",
            )

            streams = json.loads(
                subprocess.run(
                    [
                        ffprobe,
                        "-v",
                        "error",
                        "-show_entries",
                        "stream=codec_type",
                        "-of",
                        "json",
                        str(result.file_path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )["streams"]
            self.assertEqual(result.file_path.suffix, ".mp4")
            self.assertGreater(result.file_size, 0)
            self.assertEqual(
                {stream["codec_type"] for stream in streams},
                {"video", "audio"},
            )


class TwitchLiveServiceTest(unittest.TestCase):
    def test_lease_heartbeat_renews_with_lightweight_sqlite_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            media_id, _ = service.store.upsert_discovered(
                "twitch-live",
                _live_candidate(),
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            job = service.store.claim_next_job(
                ("download",),
                owner="heartbeat-worker",
                lease_seconds=60,
                download_lane="live",
            )
            self.assertIsNotNone(job)
            lease_before = service.store.conn.execute(
                "SELECT lease_until FROM jobs WHERE media_id=?",
                (media_id,),
            ).fetchone()["lease_until"]
            heartbeat = _LeaseHeartbeat(
                service.config.db_path,
                job,
                60,
                service.logger,
            )

            with (
                mock.patch.object(
                    Store,
                    "initialize",
                    side_effect=AssertionError("heartbeat must not initialize Store"),
                ) as initialize,
                mock.patch.object(
                    Store,
                    "renew_lease",
                    side_effect=AssertionError("heartbeat must use lightweight SQL"),
                ) as renew_lease,
            ):
                self.assertTrue(heartbeat.start())
                heartbeat.stop()

            lease_after = service.store.conn.execute(
                "SELECT lease_until FROM jobs WHERE media_id=?",
                (media_id,),
            ).fetchone()["lease_until"]
            self.assertGreater(lease_after, lease_before)
            self.assertFalse(heartbeat.lost_event.is_set())
            initialize.assert_not_called()
            renew_lease.assert_not_called()
            service.store.close()

    def test_lease_heartbeat_returns_false_when_sqlite_cannot_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            _, _ = service.store.upsert_discovered(
                "twitch-live",
                _live_candidate(),
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            job = service.store.claim_next_job(
                ("download",),
                owner="heartbeat-worker",
                lease_seconds=60,
                download_lane="live",
            )
            self.assertIsNotNone(job)
            heartbeat = _LeaseHeartbeat(
                service.config.db_path,
                job,
                60,
                service.logger,
            )

            with mock.patch(
                "ytb_tg_backup.service.sqlite3.connect",
                side_effect=sqlite3.OperationalError("database unavailable"),
            ):
                self.assertFalse(heartbeat.start())

            self.assertTrue(heartbeat.ready_event.is_set())
            self.assertTrue(heartbeat.lost_event.is_set())
            heartbeat.stop()
            service.store.close()

    def test_run_forever_uses_independent_threads_and_stores_for_source_pollers(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            standard_store = mock.Mock(name="standard_poll_store")
            live_store = mock.Mock(name="live_poll_store")
            standard_sources = mock.Mock(name="standard_sources")
            live_sources = mock.Mock(name="live_sources")
            started = threading.Barrier(2)
            calls: list[tuple[bool, object, object, str, int]] = []
            calls_lock = threading.Lock()

            def record_poll(
                *,
                live_recording: bool,
                store: object,
                sources: object,
            ) -> None:
                started.wait(timeout=5)
                with calls_lock:
                    calls.append(
                        (
                            live_recording,
                            store,
                            sources,
                            threading.current_thread().name,
                            threading.get_ident(),
                        )
                    )
                    if len(calls) == 2:
                        service.stop()

            with (
                mock.patch.object(service, "initialize"),
                mock.patch.object(service, "_log_startup_warnings"),
                mock.patch.object(service, "_worker_loop"),
                mock.patch(
                    "ytb_tg_backup.service.Store",
                    side_effect=(standard_store, live_store),
                ) as store_factory,
                mock.patch.object(
                    service,
                    "_new_source_registry",
                    side_effect=(standard_sources, live_sources),
                ),
                mock.patch.object(
                    service,
                    "_poll_origins",
                    side_effect=record_poll,
                ),
            ):
                service.run_forever()

            self.assertEqual(store_factory.call_count, 2)
            self.assertEqual({call[0] for call in calls}, {False, True})
            self.assertEqual({id(call[1]) for call in calls}, {id(standard_store), id(live_store)})
            self.assertEqual(
                {id(call[2]) for call in calls},
                {id(standard_sources), id(live_sources)},
            )
            self.assertEqual(
                {call[3] for call in calls},
                {"source-poll-worker", "twitch-live-poll-worker"},
            )
            self.assertEqual(len({call[4] for call in calls}), 2)
            standard_store.initialize.assert_called_once_with()
            live_store.initialize.assert_called_once_with()
            standard_store.close.assert_called_once_with()
            live_store.close.assert_called_once_with()
            service.store.close()

    def test_live_lane_downloads_is_live_stream_immediately_and_standard_lane_does_not_steal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            service = self._service(tmp_path)
            media_id, _ = service.store.upsert_discovered(
                "twitch-live",
                _live_candidate(),
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            artifact_path = tmp_path / "live.ts"
            artifact_path.write_bytes(b"live recording")
            downloader = mock.Mock()
            downloader.probe.side_effect = [
                ProbeResult(
                    live_status="is_live",
                    title="ASMR live now",
                    external_id="98765",
                ),
                ProbeResult(
                    live_status="not_live",
                    title=None,
                ),
            ]
            downloader.download.return_value = DownloadResult(
                file_path=artifact_path,
                file_size=artifact_path.stat().st_size,
            )
            downloader.merge_live_segments.return_value = DownloadResult(
                file_path=artifact_path,
                file_size=artifact_path.stat().st_size,
            )

            standard_processed = service._process_available(
                service.store,
                downloader,
                mock.Mock(),
                owner="standard-worker",
                limit=1,
                job_types=("download",),
                download_lane="standard",
            )
            live_processed = service._process_available(
                service.store,
                downloader,
                mock.Mock(),
                owner="live-worker",
                limit=1,
                job_types=("download",),
                download_lane="live",
            )

            self.assertEqual(standard_processed, 0)
            self.assertEqual(live_processed, 1)
            self.assertEqual(
                downloader.probe.call_args_list,
                [
                    mock.call(
                        "https://www.twitch.tv/example_streamer",
                        provider="twitch",
                        live=True,
                    ),
                    mock.call(
                        "https://www.twitch.tv/example_streamer",
                        provider="twitch",
                        live=True,
                    ),
                ],
            )
            downloader.download.assert_called_once()
            args = downloader.download.call_args.args
            kwargs = downloader.download.call_args.kwargs
            self.assertEqual(
                args,
                ("98765", "https://www.twitch.tv/example_streamer"),
            )
            self.assertEqual(kwargs["provider"], "twitch")
            self.assertFalse(kwargs["ignore_archive"])
            self.assertTrue(kwargs["live"])
            self.assertEqual(len(kwargs["cancel_events"]), 2)
            self.assertIs(kwargs["cancel_events"][0], service._stop_event)
            self.assertFalse(kwargs["cancel_events"][1].is_set())
            job_state = service.store.conn.execute(
                """
                SELECT state FROM jobs
                WHERE media_id=? AND job_type='download'
                """,
                (media_id,),
            ).fetchone()["state"]
            self.assertEqual(job_state, "succeeded")
            service.store.close()

    def test_live_download_reconnect_does_not_consume_failure_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            media_id, _ = service.store.upsert_discovered(
                "twitch-live",
                _live_candidate(),
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            downloader = mock.Mock()
            downloader.probe.side_effect = [
                ProbeResult(
                    live_status="is_live",
                    title="ASMR live",
                    external_id="98765",
                ),
                ProbeResult(
                    live_status="is_live",
                    title="ASMR live",
                    external_id="98765",
                ),
            ]
            downloader.download.side_effect = LiveDownloadError(
                subprocess.CalledProcessError(
                    1,
                    ["yt-dlp"],
                    stderr="connection reset by peer",
                ),
            )

            processed = service._process_available(
                service.store,
                downloader,
                mock.Mock(),
                owner="live-worker",
                limit=1,
                job_types=("download",),
                download_lane="live",
            )

            job = service.store.conn.execute(
                """
                SELECT state, reason_code, failure_count
                FROM jobs WHERE media_id=?
                """,
                (media_id,),
            ).fetchone()
            self.assertEqual(processed, 1)
            self.assertEqual(tuple(job), ("retry", "live_interrupted", 0))
            self.assertEqual(downloader.probe.call_count, 2)
            service.store.close()

    def test_retryable_live_error_does_not_consume_budget_when_status_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            media_id, _ = service.store.upsert_discovered(
                "twitch-live",
                _live_candidate(),
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            downloader = mock.Mock()
            downloader.probe.side_effect = [
                ProbeResult(
                    live_status="is_live",
                    title="ASMR live",
                    external_id="98765",
                ),
                subprocess.CalledProcessError(
                    1,
                    ["yt-dlp"],
                    stderr="temporary network outage",
                ),
            ]
            downloader.download.side_effect = LiveDownloadError(
                subprocess.CalledProcessError(
                    1,
                    ["yt-dlp"],
                    stderr="connection reset by peer",
                ),
            )

            processed = service._process_available(
                service.store,
                downloader,
                mock.Mock(),
                owner="live-worker",
                limit=1,
                job_types=("download",),
                download_lane="live",
            )

            job = service.store.conn.execute(
                """
                SELECT state, reason_code, failure_count
                FROM jobs WHERE media_id=?
                """,
                (media_id,),
            ).fetchone()
            self.assertEqual(processed, 1)
            self.assertEqual(tuple(job), ("retry", "live_interrupted", 0))
            self.assertEqual(downloader.probe.call_count, 2)
            service.store.close()

    def test_replaced_stream_finalizes_segments_after_download_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            service = self._service(tmp_path)
            media_id, _ = service.store.upsert_discovered(
                "twitch-live",
                _live_candidate(),
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            segment = tmp_path / "old-stream.live-a.mp4"
            merged = tmp_path / "old-stream.merged.mp4"
            segment.write_bytes(b"old stream segment")
            merged.write_bytes(b"merged old stream")
            downloader = mock.Mock()
            downloader.probe.side_effect = [
                ProbeResult(
                    live_status="is_live",
                    title="Old stream",
                    external_id="98765",
                ),
                ProbeResult(
                    live_status="is_live",
                    title="New stream",
                    external_id="new-stream",
                ),
            ]
            downloader.download.side_effect = LiveDownloadError(
                subprocess.CalledProcessError(
                    1,
                    ["yt-dlp"],
                    stderr="connection reset by peer",
                ),
                partial_result=DownloadResult(
                    file_path=segment,
                    file_size=segment.stat().st_size,
                    attempt_order=100,
                ),
            )
            downloader.merge_live_segments.return_value = DownloadResult(
                file_path=merged,
                file_size=merged.stat().st_size,
            )

            processed = service._process_available(
                service.store,
                downloader,
                mock.Mock(),
                owner="live-worker",
                limit=1,
                job_types=("download",),
                download_lane="live",
            )

            job = service.store.conn.execute(
                "SELECT state, failure_count FROM jobs WHERE media_id=?",
                (media_id,),
            ).fetchone()
            master = service.store.get_artifact(media_id)
            self.assertEqual(processed, 1)
            self.assertEqual(tuple(job), ("succeeded", 0))
            self.assertEqual(master["path"], str(merged))
            downloader.merge_live_segments.assert_called_once_with(
                "98765",
                [segment],
                provider="twitch",
            )
            service.store.close()

    def test_clean_live_eof_reconnects_when_same_stream_is_still_online(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            service = self._service(tmp_path)
            media_id, _ = service.store.upsert_discovered(
                "twitch-live",
                _live_candidate(),
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            segment = tmp_path / "clean-eof.live-a.mp4"
            segment.write_bytes(b"partial live recording")
            downloader = mock.Mock()
            downloader.probe.side_effect = [
                ProbeResult(
                    live_status="is_live",
                    title="ASMR live",
                    external_id="98765",
                ),
                ProbeResult(
                    live_status="is_live",
                    title="ASMR live",
                    external_id="98765",
                ),
            ]
            downloader.download.return_value = DownloadResult(
                file_path=segment,
                file_size=segment.stat().st_size,
                attempt_order=100,
            )

            processed = service._process_available(
                service.store,
                downloader,
                mock.Mock(),
                owner="live-worker",
                limit=1,
                job_types=("download",),
                download_lane="live",
            )

            job = service.store.conn.execute(
                """
                SELECT state, reason_code, failure_count
                FROM jobs WHERE media_id=?
                """,
                (media_id,),
            ).fetchone()
            self.assertEqual(processed, 1)
            self.assertEqual(tuple(job), ("retry", "live_interrupted", 0))
            self.assertEqual(
                service.store.live_segment_paths(media_id),
                [segment],
            )
            downloader.merge_live_segments.assert_not_called()
            service.store.close()

    def test_live_and_running_vod_complete_without_duplicate_delivery(self):
        for completion_order in ("live-first", "vod-first"):
            with self.subTest(completion_order=completion_order), tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                service = self._service(
                    tmp_path,
                    include_vod_origin=True,
                    download_delay_seconds=0,
                )
                live_media_id, _ = service.store.upsert_discovered(
                    "twitch-live",
                    _live_candidate(),
                    job_payload={
                        "download_lane": "live",
                        "recording_mode": "live",
                    },
                )
                vod_media_id, _ = service.store.upsert_discovered(
                    "twitch-vod",
                    _vod_candidate(),
                )
                live_job = service.store.claim_next_job(
                    ("download",),
                    owner="live-worker",
                    lease_seconds=60,
                    download_lane="live",
                )
                vod_job = service.store.claim_next_job(
                    ("download",),
                    owner="standard-worker",
                    lease_seconds=60,
                    download_lane="standard",
                )
                self.assertIsNotNone(live_job)
                self.assertIsNotNone(vod_job)
                live_path = tmp_path / "live.ts"
                vod_path = tmp_path / "vod.mp4"
                live_path.write_bytes(b"live")
                vod_path.write_bytes(b"vod")

                def finish_live() -> None:
                    service.store.complete_download(
                        live_job,
                        path=live_path,
                        size_bytes=live_path.stat().st_size,
                        delivery_targets=("telegram:test",),
                    )

                def finish_vod() -> None:
                    service.store.complete_download(
                        vod_job,
                        path=vod_path,
                        size_bytes=vod_path.stat().st_size,
                        delivery_targets=("telegram:test",),
                    )

                if completion_order == "live-first":
                    finish_live()
                    finish_vod()
                else:
                    finish_vod()
                    pending_vod = service.store.conn.execute(
                        "SELECT state, reason_code FROM jobs WHERE id=?",
                        (vod_job.id,),
                    ).fetchone()
                    self.assertEqual(
                        tuple(pending_vod),
                        ("retry", "live_recording_pending"),
                    )
                    staged = service.store.conn.execute(
                        """
                        SELECT state FROM artifacts
                        WHERE media_id=? AND role='master' AND part_no=0
                        """,
                        (vod_media_id,),
                    ).fetchone()["state"]
                    self.assertEqual(staged, "staged")
                    service.store.ensure_delivery_jobs_for_ready_artifacts(
                        "telegram:test",
                    )
                    self.assertEqual(
                        service.store.conn.execute(
                            """
                            SELECT COUNT(*) FROM jobs
                            WHERE media_id=? AND job_type='telegram_delivery'
                            """,
                            (vod_media_id,),
                        ).fetchone()[0],
                        0,
                    )
                    finish_live()

                live_state = service.store.conn.execute(
                    "SELECT state FROM jobs WHERE id=?",
                    (live_job.id,),
                ).fetchone()["state"]
                vod_state = service.store.conn.execute(
                    "SELECT state, reason_code FROM jobs WHERE id=?",
                    (vod_job.id,),
                ).fetchone()
                vod_artifact_state = service.store.conn.execute(
                    """
                    SELECT state FROM artifacts
                    WHERE media_id=? AND role='master' AND part_no=0
                    """,
                    (vod_media_id,),
                ).fetchone()["state"]
                vod_deliveries = service.store.conn.execute(
                    """
                    SELECT COUNT(*) FROM jobs
                    WHERE media_id=? AND job_type='telegram_delivery'
                      AND state!='cancelled'
                    """,
                    (vod_media_id,),
                ).fetchone()[0]
                live_deliveries = service.store.conn.execute(
                    """
                    SELECT COUNT(*) FROM jobs
                    WHERE media_id=? AND job_type='telegram_delivery'
                      AND state='queued'
                    """,
                    (live_media_id,),
                ).fetchone()[0]
                self.assertEqual(live_state, "succeeded")
                self.assertEqual(
                    tuple(vod_state),
                    ("cancelled", "live_recording_exists"),
                )
                self.assertEqual(vod_artifact_state, "suppressed")
                self.assertEqual(vod_deliveries, 0)
                self.assertEqual(live_deliveries, 1)
                service.store.close()

    def test_matching_live_completion_does_not_revive_panel_purged_vod(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            service = self._service(
                tmp_path,
                include_vod_origin=True,
                download_delay_seconds=0,
            )
            download_root = service.config.download_dir
            download_root.mkdir(parents=True, exist_ok=True)
            stream_id = "purged-vod-stream"
            vod_media_id, _ = service.store.upsert_discovered(
                "twitch-vod",
                _vod_candidate(stream_id),
            )
            vod_job = service.store.claim_next_job(
                ("download",),
                owner="standard-worker",
                lease_seconds=60,
                download_lane="standard",
            )
            self.assertIsNotNone(vod_job)
            self.assertEqual(vod_job.media_id, vod_media_id)
            vod_path = download_root / "purged-vod.mp4"
            vod_path.write_bytes(b"vod archive")
            vod_artifact_id = service.store.complete_download(
                vod_job,
                path=vod_path,
                size_bytes=vod_path.stat().st_size,
            )
            vod_detail = service.store.get_disk_resource(
                vod_artifact_id,
                download_root,
            )
            self.assertIsNotNone(vod_detail)

            purge = service.store.purge_disk_resource(
                vod_artifact_id,
                download_root,
                expected_revision=str(
                    vod_detail["resource_revision"]
                ),
            )

            self.assertTrue(purge["completed"])
            self.assertFalse(vod_path.exists())
            self.assertEqual(
                service.store.get_artifact(vod_media_id)["state"],
                "purged",
            )
            self.assertEqual(
                service.store.list_disk_resources(download_root)["total"],
                0,
            )

            live_media_id, _ = service.store.upsert_discovered(
                "twitch-live",
                _live_candidate(stream_id),
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            live_job = service.store.claim_next_job(
                ("download",),
                owner="live-worker",
                lease_seconds=60,
                download_lane="live",
            )
            self.assertIsNotNone(live_job)
            self.assertEqual(live_job.media_id, live_media_id)
            live_path = download_root / "matching-live.ts"
            live_path.write_bytes(b"live archive")
            live_artifact_id = service.store.complete_download(
                live_job,
                path=live_path,
                size_bytes=live_path.stat().st_size,
            )

            self.assertEqual(
                service.store.get_artifact(vod_media_id)["state"],
                "purged",
            )
            self.assertIsNone(
                service.store.get_disk_resource(
                    vod_artifact_id,
                    download_root,
                )
            )
            library = service.store.list_disk_resources(download_root)
            self.assertEqual(library["total"], 1)
            self.assertEqual(len(library["items"]), 1)
            self.assertEqual(
                library["items"][0]["artifact_id"],
                live_artifact_id,
            )
            self.assertEqual(
                library["items"][0]["media_id"],
                live_media_id,
            )
            self.assertEqual(
                library["items"][0]["content_kind"],
                "live_stream",
            )
            service.store.close()

    def test_delivered_vod_suppresses_later_live_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            service = self._service(
                tmp_path,
                include_vod_origin=True,
                download_delay_seconds=0,
            )
            vod_media_id, _ = service.store.upsert_discovered(
                "twitch-vod",
                _vod_candidate(),
            )
            vod_job = service.store.claim_next_job(
                ("download",),
                owner="standard-worker",
                lease_seconds=60,
                download_lane="standard",
            )
            vod_path = tmp_path / "vod.mp4"
            vod_path.write_bytes(b"vod")
            vod_artifact_id = service.store.complete_download(
                vod_job,
                path=vod_path,
                size_bytes=vod_path.stat().st_size,
                delivery_targets=("telegram:test",),
            )
            delivery_job = service.store.claim_next_job(
                ("telegram_delivery",),
                owner="delivery-worker",
                lease_seconds=60,
                download_lane="standard",
            )
            self.assertIsNotNone(delivery_job)
            service.store.complete_delivery(
                delivery_job,
                artifact_id=vod_artifact_id,
                destination_key="telegram:test",
                remote_id="message-1",
            )

            live_media_id, _ = service.store.upsert_discovered(
                "twitch-live",
                _live_candidate(),
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            live_job = service.store.claim_next_job(
                ("download",),
                owner="live-worker",
                lease_seconds=60,
                download_lane="live",
            )
            live_path = tmp_path / "live.mp4"
            live_path.write_bytes(b"live")
            service.store.complete_download(
                live_job,
                path=live_path,
                size_bytes=live_path.stat().st_size,
                delivery_targets=("telegram:test",),
            )

            vod_job_state = service.store.conn.execute(
                "SELECT state FROM jobs WHERE id=?",
                (vod_job.id,),
            ).fetchone()["state"]
            vod_artifact_state = service.store.conn.execute(
                "SELECT state FROM artifacts WHERE id=?",
                (vod_artifact_id,),
            ).fetchone()["state"]
            live_artifact_state = service.store.conn.execute(
                """
                SELECT state FROM artifacts
                WHERE media_id=? AND role='master' AND part_no=0
                """,
                (live_media_id,),
            ).fetchone()["state"]
            live_delivery_count = service.store.conn.execute(
                """
                SELECT COUNT(*) FROM jobs
                WHERE media_id=? AND job_type='telegram_delivery'
                """,
                (live_media_id,),
            ).fetchone()[0]
            self.assertEqual(vod_job_state, "succeeded")
            self.assertEqual(vod_artifact_state, "ready")
            self.assertEqual(live_artifact_state, "suppressed")
            self.assertEqual(live_delivery_count, 0)
            self.assertEqual(
                service.store.conn.execute(
                    "SELECT COUNT(*) FROM deliveries WHERE media_id=?",
                    (vod_media_id,),
                ).fetchone()[0],
                1,
            )
            service.store.close()

    def test_live_retry_records_segments_and_merges_them_on_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            service = self._service(tmp_path)
            media_id, _ = service.store.upsert_discovered(
                "twitch-live",
                _live_candidate(),
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            first_segment = tmp_path / "first.live-a.mp4"
            second_segment = tmp_path / "second.live-b.mp4"
            merged = tmp_path / "merged.mp4"
            first_segment.write_bytes(b"first")
            second_segment.write_bytes(b"second")
            merged.write_bytes(b"firstsecond")
            downloader = mock.Mock()
            downloader.probe.side_effect = [
                ProbeResult(
                    live_status="is_live",
                    title="ASMR live",
                    external_id="98765",
                ),
                ProbeResult(
                    live_status="is_live",
                    title="ASMR live",
                    external_id="98765",
                ),
                ProbeResult(
                    live_status="is_live",
                    title="ASMR live",
                    external_id="98765",
                ),
                ProbeResult(
                    live_status="not_live",
                    title=None,
                ),
            ]
            downloader.download.side_effect = [
                LiveDownloadError(
                    subprocess.CalledProcessError(
                        1,
                        ["yt-dlp"],
                        stderr="connection reset by peer",
                    ),
                    partial_result=DownloadResult(
                        file_path=first_segment,
                        file_size=first_segment.stat().st_size,
                    ),
                ),
                DownloadResult(
                    file_path=second_segment,
                    file_size=second_segment.stat().st_size,
                ),
            ]
            downloader.merge_live_segments.return_value = DownloadResult(
                file_path=merged,
                file_size=merged.stat().st_size,
            )

            self.assertEqual(
                service._process_available(
                    service.store,
                    downloader,
                    mock.Mock(),
                    owner="live-worker-1",
                    limit=1,
                    job_types=("download",),
                    download_lane="live",
                ),
                1,
            )
            service.store.conn.execute(
                "UPDATE jobs SET available_at='1970-01-01T00:00:00+00:00' WHERE media_id=?",
                (media_id,),
            )
            service.store.conn.commit()
            self.assertEqual(
                service._process_available(
                    service.store,
                    downloader,
                    mock.Mock(),
                    owner="live-worker-2",
                    limit=1,
                    job_types=("download",),
                    download_lane="live",
                ),
                1,
            )

            segments = service.store.conn.execute(
                """
                SELECT part_no, path FROM artifacts
                WHERE media_id=? AND role='live_segment'
                ORDER BY part_no
                """,
                (media_id,),
            ).fetchall()
            master = service.store.get_artifact(media_id)
            job = service.store.conn.execute(
                "SELECT state, failure_count FROM jobs WHERE media_id=?",
                (media_id,),
            ).fetchone()
            self.assertEqual(
                [(row["part_no"], row["path"]) for row in segments],
                [(0, str(first_segment)), (1, str(second_segment))],
            )
            self.assertEqual(master["path"], str(merged))
            self.assertEqual(tuple(job), ("succeeded", 0))
            downloader.merge_live_segments.assert_called_once_with(
                "98765",
                [first_segment, second_segment],
                provider="twitch",
            )
            service.store.close()

    def test_first_vod_poll_after_live_mode_seeds_only_latest_vod(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            service.store.upsert_discovered(
                "twitch-live",
                _live_candidate(),
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            vod_origin = replace(
                _live_origin(),
                bootstrap="latest",
                options={"recording_mode": "vod"},
            )
            service.store.upsert_origin(vod_origin)
            candidates = [
                replace(
                    _vod_candidate(str(index)),
                    published_at=f"2026-07-2{6 - index}T14:00:00Z",
                )
                for index in range(1, 4)
            ]
            adapter = mock.Mock()
            adapter.discover.return_value = DiscoveryResult(
                items=candidates,
                cursor='{"external_id":"vod-1"}',
            )

            with mock.patch.object(service.sources, "get", return_value=adapter):
                service._poll_origin(vod_origin, None, None)

            rows = service.store.conn.execute(
                """
                SELECT mi.external_id, oi.disposition, oi.decision_code,
                  EXISTS(
                    SELECT 1 FROM jobs j
                    WHERE j.media_id=mi.id AND j.job_type='download'
                  ) AS has_job
                FROM media_items mi
                JOIN origin_items oi ON oi.media_id=mi.id
                WHERE oi.origin_id='twitch-live' AND mi.content_kind='vod'
                ORDER BY mi.external_id
                """
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in rows],
                [
                    ("vod-1", "eligible", None, 1),
                    ("vod-2", "ignored", "initial_seed", 0),
                    ("vod-3", "ignored", "initial_seed", 0),
                ],
            )
            service.store.close()

    def test_inflight_live_poll_is_discarded_after_origin_switches_to_vod(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            polled_origin = next(
                origin
                for origin in service.store.list_origins()
                if origin.id == "twitch-live"
            )
            adapter = mock.Mock()

            def switch_mode_during_discovery(*_args):
                service.store.upsert_origin(
                    replace(
                        polled_origin,
                        options={"recording_mode": "vod"},
                    )
                )
                return DiscoveryResult(
                    items=[_live_candidate()],
                    cursor='{"external_id":"98765"}',
                )

            adapter.discover.side_effect = switch_mode_during_discovery
            with mock.patch.object(service.sources, "get", return_value=adapter):
                service._poll_origin(polled_origin, None, None)

            self.assertEqual(
                service.store.conn.execute(
                    """
                    SELECT COUNT(*) FROM media_items
                    WHERE provider='twitch' AND content_kind='live_stream'
                    """
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                service.store.conn.execute(
                    "SELECT COUNT(*) FROM jobs"
                ).fetchone()[0],
                0,
            )
            service.store.close()

    def test_inflight_live_poll_failure_does_not_delay_new_vod_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            polled_origin = next(
                origin
                for origin in service.store.list_origins()
                if origin.id == "twitch-live"
            )
            adapter = mock.Mock()

            def switch_mode_then_fail(*_args):
                service.store.upsert_origin(
                    replace(
                        polled_origin,
                        options={"recording_mode": "vod"},
                    )
                )
                raise SourceError(
                    "old live request failed",
                    code="old_live_request",
                    retry_after=300,
                )

            adapter.discover.side_effect = switch_mode_then_fail
            with mock.patch.object(service.sources, "get", return_value=adapter):
                service._poll_origin(polled_origin, None, None)

            self.assertTrue(service.store.origin_poll_due(polled_origin.id))
            checkpoint = service.store.conn.execute(
                """
                SELECT last_error_code, next_poll_at
                FROM origin_poll_state
                WHERE origin_id=?
                """,
                (polled_origin.id,),
            ).fetchone()
            self.assertIsNone(checkpoint)
            service.store.close()

    def test_mode_switch_finalizes_existing_live_segments_before_cancelling(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            service = self._service(tmp_path)
            media_id, _ = service.store.upsert_discovered(
                "twitch-live",
                _live_candidate(),
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            segment = tmp_path / "before-mode-switch.mp4"
            merged = tmp_path / "before-mode-switch.merged.mp4"
            segment.write_bytes(b"recorded before switch")
            merged.write_bytes(b"merged recording")
            service.store.record_live_segment(
                media_id,
                path=segment,
                size_bytes=segment.stat().st_size,
                metadata={"attempt_order": 100},
            )
            origin = next(
                origin
                for origin in service.store.list_origins()
                if origin.id == "twitch-live"
            )
            service.store.upsert_origin(
                replace(origin, options={"recording_mode": "vod"})
            )
            downloader = mock.Mock()
            downloader.merge_live_segments.return_value = DownloadResult(
                file_path=merged,
                file_size=merged.stat().st_size,
            )

            processed = service._process_available(
                service.store,
                downloader,
                mock.Mock(),
                owner="live-worker",
                limit=1,
                job_types=("download",),
                download_lane="live",
            )

            job = service.store.conn.execute(
                "SELECT state, reason_code FROM jobs WHERE media_id=?",
                (media_id,),
            ).fetchone()
            self.assertEqual(processed, 1)
            self.assertEqual(tuple(job), ("succeeded", None))
            self.assertEqual(
                service.store.get_artifact(media_id)["path"],
                str(merged),
            )
            downloader.probe.assert_not_called()
            downloader.download.assert_not_called()
            service.store.close()

    def test_live_origin_disabled_job_revives_but_stream_replaced_job_stays_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            candidate = _live_candidate()
            media_id, _ = service.store.upsert_discovered(
                "twitch-live",
                candidate,
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            service.store.upsert_origin(replace(_live_origin(), enabled=False))
            downloader = mock.Mock()

            self.assertEqual(
                service._process_available(
                    service.store,
                    downloader,
                    mock.Mock(),
                    owner="disabled-live-worker",
                    limit=1,
                    job_types=("download",),
                    download_lane="live",
                ),
                1,
            )
            disabled = service.store.conn.execute(
                "SELECT state, reason_code FROM jobs WHERE media_id=?",
                (media_id,),
            ).fetchone()
            self.assertEqual(tuple(disabled), ("cancelled", "live_origin_disabled"))
            downloader.probe.assert_not_called()

            service.store.upsert_origin(_live_origin())
            service.store.upsert_discovered(
                "twitch-live",
                candidate,
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            revived = service.store.conn.execute(
                "SELECT state, reason_code, failure_count FROM jobs WHERE media_id=?",
                (media_id,),
            ).fetchone()
            self.assertEqual(tuple(revived), ("queued", None, 0))

            downloader.probe.return_value = ProbeResult(
                live_status="is_live",
                title="A newer live",
                external_id="new-stream",
            )
            self.assertEqual(
                service._process_available(
                    service.store,
                    downloader,
                    mock.Mock(),
                    owner="replacement-check-worker",
                    limit=1,
                    job_types=("download",),
                    download_lane="live",
                ),
                1,
            )
            replaced = service.store.conn.execute(
                "SELECT state, reason_code FROM jobs WHERE media_id=?",
                (media_id,),
            ).fetchone()
            self.assertEqual(tuple(replaced), ("cancelled", "stream_replaced"))

            service.store.upsert_discovered(
                "twitch-live",
                candidate,
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            rediscovered = service.store.conn.execute(
                "SELECT state, reason_code FROM jobs WHERE media_id=?",
                (media_id,),
            ).fetchone()
            self.assertEqual(tuple(rediscovered), ("cancelled", "stream_replaced"))
            downloader.download.assert_not_called()
            service.store.close()

    def test_vod_waits_while_matching_live_job_is_queued_running_or_retrying(self):
        for live_state in ("queued", "running", "retry"):
            with self.subTest(live_state=live_state), tempfile.TemporaryDirectory() as tmp:
                service = self._service(
                    Path(tmp),
                    include_vod_origin=True,
                    download_delay_seconds=0,
                )
                service.store.upsert_discovered(
                    "twitch-live",
                    _live_candidate(),
                    job_payload={
                        "download_lane": "live",
                        "recording_mode": "live",
                    },
                )
                if live_state != "queued":
                    live_job = service.store.claim_next_job(
                        ("download",),
                        owner=f"{live_state}-live-worker",
                        lease_seconds=60,
                        download_lane="live",
                    )
                    self.assertIsNotNone(live_job)
                    if live_state == "retry":
                        service.store.defer_job(
                            live_job,
                            reason_code="download_failed",
                            error="retry live recording",
                            retry_seconds=60,
                        )

                vod_media_id, _ = service.store.upsert_discovered(
                    "twitch-vod",
                    _vod_candidate(),
                )
                downloader = mock.Mock()
                downloader.probe.return_value = ProbeResult(
                    live_status=None,
                    title="ASMR archived VOD",
                )
                vod_path = Path(tmp) / f"{live_state}-vod.mp4"
                vod_path.write_bytes(b"vod")
                downloader.download.return_value = DownloadResult(
                    file_path=vod_path,
                    file_size=vod_path.stat().st_size,
                )

                processed = service._process_available(
                    service.store,
                    downloader,
                    mock.Mock(),
                    owner=f"{live_state}-standard-worker",
                    limit=1,
                    job_types=("download",),
                    download_lane="standard",
                )

                vod_job = service.store.conn.execute(
                    """
                    SELECT state, reason_code, failure_count
                    FROM jobs WHERE media_id=? AND job_type='download'
                    """,
                    (vod_media_id,),
                ).fetchone()
                self.assertEqual(processed, 1)
                self.assertEqual(
                    tuple(vod_job),
                    ("retry", "live_recording_pending", 0),
                )
                downloader.probe.assert_not_called()
                downloader.download.assert_not_called()
                service.store.close()

    def test_deferred_vod_is_cancelled_when_matching_live_artifact_becomes_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            service = self._service(
                tmp_path,
                include_vod_origin=True,
                download_delay_seconds=0,
            )
            live_media_id, _ = service.store.upsert_discovered(
                "twitch-live",
                _live_candidate(),
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            vod_media_id, _ = service.store.upsert_discovered(
                "twitch-vod",
                _vod_candidate(),
            )
            downloader = mock.Mock()
            downloader.probe.return_value = ProbeResult(
                live_status=None,
                title="ASMR archived VOD",
            )
            vod_path = tmp_path / "vod.mp4"
            vod_path.write_bytes(b"vod")
            downloader.download.return_value = DownloadResult(
                file_path=vod_path,
                file_size=vod_path.stat().st_size,
            )

            self.assertEqual(
                service._process_available(
                    service.store,
                    downloader,
                    mock.Mock(),
                    owner="standard-worker",
                    limit=1,
                    job_types=("download",),
                    download_lane="standard",
                ),
                1,
            )
            deferred = service.store.conn.execute(
                "SELECT state, reason_code FROM jobs WHERE media_id=?",
                (vod_media_id,),
            ).fetchone()
            self.assertEqual(tuple(deferred), ("retry", "live_recording_pending"))

            live_job = service.store.claim_next_job(
                ("download",),
                owner="live-worker",
                lease_seconds=60,
                download_lane="live",
            )
            self.assertIsNotNone(live_job)
            artifact_path = tmp_path / "live.ts"
            artifact_path.write_bytes(b"live recording")
            service.store.complete_download(
                live_job,
                path=artifact_path,
                size_bytes=artifact_path.stat().st_size,
            )

            live_state = service.store.conn.execute(
                "SELECT state FROM jobs WHERE media_id=?",
                (live_media_id,),
            ).fetchone()["state"]
            vod_state = service.store.conn.execute(
                "SELECT state, reason_code FROM jobs WHERE media_id=?",
                (vod_media_id,),
            ).fetchone()
            self.assertEqual(live_state, "succeeded")
            self.assertEqual(
                tuple(vod_state),
                ("cancelled", "live_recording_exists"),
            )
            downloader.probe.assert_not_called()
            downloader.download.assert_not_called()
            service.store.close()

    def test_suppressed_vod_artifact_revives_if_live_artifact_is_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            service = self._service(
                tmp_path,
                include_vod_origin=True,
                download_delay_seconds=0,
            )
            live_media_id, _ = service.store.upsert_discovered(
                "twitch-live",
                _live_candidate(),
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            vod_media_id, _ = service.store.upsert_discovered(
                "twitch-vod",
                _vod_candidate(),
            )
            live_job = service.store.claim_next_job(
                ("download",),
                owner="live-worker",
                lease_seconds=60,
                download_lane="live",
            )
            vod_job = service.store.claim_next_job(
                ("download",),
                owner="standard-worker",
                lease_seconds=60,
                download_lane="standard",
            )
            live_path = tmp_path / "live.mp4"
            vod_path = tmp_path / "vod.mp4"
            live_path.write_bytes(b"live")
            vod_path.write_bytes(b"vod")
            service.store.complete_download(
                vod_job,
                path=vod_path,
                size_bytes=vod_path.stat().st_size,
            )
            service.store.complete_download(
                live_job,
                path=live_path,
                size_bytes=live_path.stat().st_size,
            )
            live_path.unlink()

            service.store.upsert_discovered(
                "twitch-vod",
                _vod_candidate(),
            )
            revived = service.store.conn.execute(
                "SELECT state, reason_code FROM jobs WHERE media_id=?",
                (vod_media_id,),
            ).fetchone()
            self.assertEqual(tuple(revived), ("queued", None))
            downloader = mock.Mock()
            self.assertEqual(
                service._process_available(
                    service.store,
                    downloader,
                    mock.Mock(),
                    owner="fallback-worker",
                    limit=1,
                    job_types=("download",),
                    download_lane="standard",
                ),
                1,
            )

            vod_state = service.store.conn.execute(
                "SELECT state FROM jobs WHERE media_id=?",
                (vod_media_id,),
            ).fetchone()["state"]
            vod_artifact_state = service.store.get_artifact(vod_media_id)["state"]
            self.assertEqual(vod_state, "succeeded")
            self.assertEqual(vod_artifact_state, "ready")
            self.assertFalse(service.store.has_ready_twitch_live_recording("98765"))
            downloader.probe.assert_not_called()
            downloader.download.assert_not_called()
            self.assertIsNotNone(service.store.get_artifact(live_media_id))
            service.store.close()

    def test_process_pending_leaves_live_lane_job_queued(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            media_id, _ = service.store.upsert_discovered(
                "twitch-live",
                _live_candidate(),
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            downloader = mock.Mock()
            service.downloader = downloader

            service.process_pending()

            job = service.store.conn.execute(
                "SELECT state, reason_code FROM jobs WHERE media_id=?",
                (media_id,),
            ).fetchone()
            self.assertEqual(tuple(job), ("queued", None))
            downloader.probe.assert_not_called()
            downloader.download.assert_not_called()
            service.store.close()

    def test_stale_live_job_does_not_record_a_newer_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            media_id, _ = service.store.upsert_discovered(
                "twitch-live",
                _live_candidate("old-stream"),
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            downloader = mock.Mock()
            downloader.probe.return_value = ProbeResult(
                live_status="is_live",
                title="A newer live",
                external_id="new-stream",
            )

            processed = service._process_available(
                service.store,
                downloader,
                mock.Mock(),
                owner="live-worker",
                limit=1,
                job_types=("download",),
                download_lane="live",
            )

            row = service.store.conn.execute(
                "SELECT state, reason_code FROM jobs WHERE media_id=?",
                (media_id,),
            ).fetchone()
            self.assertEqual(processed, 1)
            self.assertEqual(tuple(row), ("cancelled", "stream_replaced"))
            downloader.download.assert_not_called()
            service.store.close()

    def test_vod_is_ignored_when_matching_live_stream_artifact_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            service = self._service(tmp_path, include_vod_origin=True)
            live_media_id, _ = service.store.upsert_discovered(
                "twitch-live",
                _live_candidate(),
                job_payload={
                    "download_lane": "live",
                    "recording_mode": "live",
                },
            )
            live_job = service.store.claim_next_job(
                ("download",),
                owner="live-worker",
                lease_seconds=60,
                download_lane="live",
            )
            artifact_path = tmp_path / "live.ts"
            artifact_path.write_bytes(b"live recording")
            service.store.complete_download(
                live_job,
                path=artifact_path,
                size_bytes=artifact_path.stat().st_size,
            )
            self.assertTrue(service.store.has_ready_twitch_live_recording("98765"))

            vod = _vod_candidate()
            adapter = mock.Mock()
            adapter.discover.return_value = DiscoveryResult(
                items=[vod],
                cursor='{"external_id":"vod-98765"}',
            )
            vod_origin = next(
                origin
                for origin in service.store.list_origins()
                if origin.id == "twitch-vod"
            )

            with mock.patch.object(service.sources, "get", return_value=adapter):
                service._poll_origin(vod_origin, None, None)

            vod_media = service.store.conn.execute(
                """
                SELECT mi.id, oi.disposition, oi.decision_code
                FROM media_items mi
                JOIN origin_items oi ON oi.media_id=mi.id
                WHERE mi.provider='twitch' AND mi.content_kind='vod'
                  AND mi.external_id='vod-98765'
                  AND oi.origin_id='twitch-vod'
                """
            ).fetchone()
            self.assertIsNotNone(vod_media)
            self.assertEqual(
                (vod_media["disposition"], vod_media["decision_code"]),
                ("ignored", "live_recording_exists"),
            )
            self.assertEqual(
                service.store.conn.execute(
                    """
                    SELECT COUNT(*) FROM jobs
                    WHERE media_id=? AND job_type='download'
                    """,
                    (vod_media["id"],),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                service.store.conn.execute(
                    "SELECT state FROM jobs WHERE media_id=? AND job_type='download'",
                    (live_media_id,),
                ).fetchone()["state"],
                "succeeded",
            )
            adapter.discover.assert_called_once_with(vod_origin, None)
            service.store.close()

    @staticmethod
    def _service(
        tmp_path: Path,
        *,
        include_vod_origin: bool = False,
        download_delay_seconds: int = 86400,
    ) -> BackupService:
        vod_origin = """

[[origins]]
id = "twitch-vod"
provider = "twitch"
kind = "vods"
name = "ASMR Twitch VOD"
external_id = "12345"
bootstrap = "all"
recording_mode = "vod"
""" if include_vod_origin else ""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            f"""
[app]
data_dir = "{tmp_path}"
download_delay_seconds = {download_delay_seconds}
max_attempts = 3
job_lease_seconds = 60

[twitch]
recording_mode = "vod"
live_retry_seconds = 1

[[origins]]
id = "twitch-live"
provider = "twitch"
kind = "vods"
name = "ASMR Twitch"
external_id = "12345"
bootstrap = "all"
recording_mode = "live"
{vod_origin}
""".strip()
        )
        service = BackupService(load_config(config_path))
        service.initialize()
        return service


if __name__ == "__main__":
    unittest.main()
