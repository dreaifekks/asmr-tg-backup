import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from ytb_tg_backup.config import load_config
from ytb_tg_backup.downloader import Downloader, _bitrate_candidates, _looks_like_thumbnail, _target_audio_bitrate_kbps


class DownloaderHelpersTest(unittest.TestCase):
    def test_probe_passes_proxy_in_an_isolated_child_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.toml"
            config_path.write_text(
                f"""
[app]
data_dir = "{tmp_path}"

[proxy]
url = "http://127.0.0.1:7890"
sources = false
downloads = true
""".strip()
            )
            payload = json.dumps(
                {"id": "proxy123", "title": "Proxy test", "live_status": "not_live"}
            )
            parent_env = {"PRESERVED_FOR_CHILD": "yes"}

            with mock.patch.dict(os.environ, parent_env, clear=True):
                downloader = Downloader(load_config(config_path), logging.getLogger("test"))
                with mock.patch(
                    "ytb_tg_backup.downloader.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, payload, ""),
                ) as run:
                    downloader.probe("https://www.youtube.com/watch?v=proxy123")

                child_env = run.call_args.kwargs["env"]
                self.assertIsNot(child_env, os.environ)
                self.assertEqual(child_env["PRESERVED_FOR_CHILD"], "yes")
                for name in (
                    "http_proxy",
                    "https_proxy",
                    "all_proxy",
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "ALL_PROXY",
                ):
                    self.assertEqual(child_env[name], "http://127.0.0.1:7890")
                self.assertEqual(dict(os.environ), parent_env)

    def test_download_proxy_can_be_disabled_without_exporting_proxy_variables(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.toml"
            config_path.write_text(
                f"""
[app]
data_dir = "{tmp_path}"

[proxy]
url = "http://127.0.0.1:7890"
sources = true
downloads = false
""".strip()
            )
            payload = json.dumps(
                {"id": "direct123", "title": "Direct test", "live_status": "not_live"}
            )

            with mock.patch.dict(os.environ, {"PARENT_ONLY": "yes"}, clear=True):
                downloader = Downloader(load_config(config_path), logging.getLogger("test"))
                with mock.patch(
                    "ytb_tg_backup.downloader.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, payload, ""),
                ) as run:
                    downloader.probe("https://www.youtube.com/watch?v=direct123")

                self.assertIsNone(run.call_args.kwargs.get("env"))
                self.assertEqual(dict(os.environ), {"PARENT_ONLY": "yes"})

    def test_download_passes_the_same_proxy_environment_to_yt_dlp(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.toml"
            config_path.write_text(
                f"""
[app]
data_dir = "{tmp_path}"

[proxy]
url = "socks5://127.0.0.1:7891"
sources = false
downloads = true
""".strip()
            )
            media_path = tmp_path / "downloaded.m4a"
            media_path.write_bytes(b"audio")

            with mock.patch.dict(os.environ, {"PARENT_ONLY": "yes"}, clear=True):
                downloader = Downloader(load_config(config_path), logging.getLogger("test"))
                with mock.patch(
                    "ytb_tg_backup.downloader.subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        ["yt-dlp"],
                        0,
                        stdout=f"{media_path}\n",
                        stderr="",
                    ),
                ) as run:
                    downloader.download(
                        "proxy-download",
                        "https://www.youtube.com/watch?v=proxy-download",
                    )

                child_env = run.call_args.kwargs["env"]
                self.assertEqual(child_env["PARENT_ONLY"], "yes")
                self.assertEqual(child_env["all_proxy"], "socks5://127.0.0.1:7891")
                self.assertEqual(child_env["ALL_PROXY"], "socks5://127.0.0.1:7891")
                self.assertEqual(dict(os.environ), {"PARENT_ONLY": "yes"})

    def test_live_download_passes_proxy_environment_to_yt_dlp_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.toml"
            config_path.write_text(
                f"""
[app]
data_dir = "{tmp_path}"

[proxy]
url = "http://127.0.0.1:7890"
downloads = true
""".strip()
            )
            process = mock.Mock()
            process.communicate.return_value = ("media.m4a\n", "")
            process.returncode = 0

            with mock.patch.dict(os.environ, {"PARENT_ONLY": "yes"}, clear=True):
                downloader = Downloader(load_config(config_path), logging.getLogger("test"))
                with mock.patch(
                    "ytb_tg_backup.downloader.subprocess.Popen",
                    return_value=process,
                ) as popen:
                    completed = downloader._run_live_download(
                        ["yt-dlp", "https://www.twitch.tv/example"],
                        cancel_events=(),
                    )

        self.assertEqual(completed.returncode, 0)
        child_env = popen.call_args.kwargs["env"]
        self.assertEqual(child_env["PARENT_ONLY"], "yes")
        self.assertEqual(child_env["https_proxy"], "http://127.0.0.1:7890")
        self.assertEqual(child_env["ALL_PROXY"], "http://127.0.0.1:7890")

    def test_youtube_probe_keeps_upcoming_metadata_without_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.toml"
            config_path.write_text(f'[app]\ndata_dir = "{tmp_path}"')
            downloader = Downloader(load_config(config_path), logging.getLogger("test"))
            payload = json.dumps(
                {
                    "id": "upcoming123",
                    "title": "Scheduled ASMR",
                    "live_status": "is_upcoming",
                    "formats": [],
                }
            )

            with mock.patch(
                "ytb_tg_backup.downloader.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, payload, ""),
            ) as run:
                result = downloader.probe("https://www.youtube.com/watch?v=upcoming123")

            command = run.call_args.args[0]
            self.assertIn("--ignore-no-formats-error", command)
            self.assertEqual(result.live_status, "is_upcoming")
            self.assertEqual(result.title, "Scheduled ASMR")
            self.assertEqual(result.external_id, "upcoming123")

    def test_twitch_probe_does_not_ignore_missing_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.toml"
            config_path.write_text(f'[app]\ndata_dir = "{tmp_path}"')
            downloader = Downloader(load_config(config_path), logging.getLogger("test"))
            payload = json.dumps(
                {
                    "id": "stream123",
                    "title": "Live stream",
                    "live_status": "is_live",
                }
            )

            with mock.patch(
                "ytb_tg_backup.downloader.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, payload, ""),
            ) as run:
                downloader.probe(
                    "https://www.twitch.tv/example",
                    provider="twitch",
                    live=True,
                )

            self.assertNotIn("--ignore-no-formats-error", run.call_args.args[0])

    def test_target_bitrate_for_long_audio(self):
        bitrate = _target_audio_bitrate_kbps(max_bytes=52_428_800, duration_seconds=10_000)
        self.assertLessEqual(bitrate, 40)
        self.assertGreaterEqual(bitrate, 24)

    def test_target_bitrate_uses_high_quality_when_limit_allows(self):
        bitrate = _target_audio_bitrate_kbps(max_bytes=1_990_000_000, duration_seconds=7_200)
        self.assertEqual(bitrate, 256)

    def test_bitrate_candidates_end_at_floor(self):
        self.assertEqual(_bitrate_candidates(40)[-1], 24)

    def test_force_audio_prefers_lossless_stream_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.toml"
            config_path.write_text(f'[app]\ndata_dir = "{tmp_path}"')
            downloader = Downloader(load_config(config_path), logging.getLogger("test"))
            media_path = tmp_path / "recording.mp4"
            media_path.write_bytes(b"video")

            def complete_copy(command, **_kwargs):
                Path(command[-1]).write_bytes(b"copied audio")
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch(
                "ytb_tg_backup.downloader.subprocess.run",
                side_effect=complete_copy,
            ) as run, mock.patch.object(downloader, "_duration_seconds") as duration:
                result = downloader.shrink_audio_for_upload(
                    media_path,
                    max_bytes=1_000,
                    force_audio=True,
                )

            command = run.call_args.args[0]
            self.assertEqual(run.call_count, 1)
            self.assertEqual(command[command.index("-c:a") + 1], "copy")
            self.assertEqual(command[command.index("-map") + 1], "0:a:0")
            self.assertNotIn("-b:a", command)
            self.assertEqual(result.file_path, tmp_path / "recording.tgaudio.m4a")
            self.assertEqual(result.file_size, len(b"copied audio"))
            duration.assert_not_called()

    def test_audio_master_under_limit_is_reused_without_ffmpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.toml"
            config_path.write_text(f'[app]\ndata_dir = "{tmp_path}"')
            downloader = Downloader(load_config(config_path), logging.getLogger("test"))
            media_path = tmp_path / "youtube-master.m4a"
            media_path.write_bytes(b"audio")

            with mock.patch("ytb_tg_backup.downloader.subprocess.run") as run:
                result = downloader.shrink_audio_for_upload(
                    media_path,
                    max_bytes=1_000,
                    force_audio=True,
                )

            self.assertEqual(result.file_path, media_path)
            self.assertEqual(result.file_size, len(b"audio"))
            run.assert_not_called()

    def test_oversize_stream_copy_falls_back_to_aac_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.toml"
            config_path.write_text(f'[app]\ndata_dir = "{tmp_path}"')
            downloader = Downloader(load_config(config_path), logging.getLogger("test"))
            media_path = tmp_path / "recording.mp4"
            media_path.write_bytes(b"video")

            def complete_commands(command, **_kwargs):
                codec = command[command.index("-c:a") + 1]
                Path(command[-1]).write_bytes(
                    b"oversize copied audio" if codec == "copy" else b"aac"
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch(
                "ytb_tg_backup.downloader.subprocess.run",
                side_effect=complete_commands,
            ) as run, mock.patch.object(
                downloader,
                "_duration_seconds",
                return_value=60,
            ):
                result = downloader.shrink_audio_for_upload(
                    media_path,
                    max_bytes=10,
                    force_audio=True,
                )

            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(commands[0][commands[0].index("-c:a") + 1], "copy")
            self.assertEqual(commands[1][commands[1].index("-c:a") + 1], "aac")
            self.assertEqual(result.file_path, tmp_path / "recording.tg24k.m4a")
            self.assertEqual(result.file_size, len(b"aac"))

    def test_incompatible_stream_copy_falls_back_to_aac_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.toml"
            config_path.write_text(f'[app]\ndata_dir = "{tmp_path}"')
            downloader = Downloader(load_config(config_path), logging.getLogger("test"))
            media_path = tmp_path / "recording.webm"
            media_path.write_bytes(b"audio")

            def complete_commands(command, **_kwargs):
                codec = command[command.index("-c:a") + 1]
                if codec == "copy":
                    raise subprocess.CalledProcessError(1, command, stderr="unsupported codec")
                Path(command[-1]).write_bytes(b"aac")
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch(
                "ytb_tg_backup.downloader.subprocess.run",
                side_effect=complete_commands,
            ) as run, mock.patch.object(
                downloader,
                "_duration_seconds",
                return_value=60,
            ):
                result = downloader.shrink_audio_for_upload(
                    media_path,
                    max_bytes=1_000,
                    force_audio=True,
                )

            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(commands[0][commands[0].index("-c:a") + 1], "copy")
            self.assertEqual(commands[1][commands[1].index("-c:a") + 1], "aac")
            self.assertEqual(result.file_path, tmp_path / "recording.tg24k.m4a")
            self.assertEqual(result.file_size, len(b"aac"))

    def test_looks_like_thumbnail_excludes_generated_thumb(self):
        self.assertFalse(_looks_like_thumbnail(Path("missing.webp")))
        with mock.patch.object(Path, "exists", return_value=True), mock.patch.object(Path, "is_file", return_value=True):
            self.assertTrue(_looks_like_thumbnail(Path("video.webp")))
            self.assertFalse(_looks_like_thumbnail(Path("video.tgthumb.jpg")))

    def test_twitch_extracts_audio_and_uses_provider_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.toml"
            config_path.write_text(f'[app]\ndata_dir = "{tmp_path}"')
            config = load_config(config_path)
            downloader = Downloader(config, logging.getLogger("test"))
            media_path = tmp_path / "twitch-vod.m4a"
            media_path.write_bytes(b"audio")

            with mock.patch(
                "ytb_tg_backup.downloader.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["yt-dlp"], 0, stdout=f"{media_path}\n", stderr=""
                ),
            ) as run:
                downloader.download("123", "https://www.twitch.tv/videos/123", provider="twitch")

            command = run.call_args.args[0]
            self.assertIn("--extract-audio", command)
            self.assertEqual(command[command.index("--format") + 1], "bestaudio/best")
            self.assertEqual(command[command.index("--audio-format") + 1], "m4a")
            archive = Path(command[command.index("--download-archive") + 1])
            self.assertEqual(archive.name, "download-archive.twitch.txt")

    def test_missing_artifact_repair_bypasses_download_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.toml"
            config_path.write_text(f'[app]\ndata_dir = "{tmp_path}"')
            downloader = Downloader(load_config(config_path), logging.getLogger("test"))
            media_path = tmp_path / "repaired.m4a"
            media_path.write_bytes(b"audio")

            with mock.patch(
                "ytb_tg_backup.downloader.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["yt-dlp"], 0, stdout=f"{media_path}\n", stderr=""
                ),
            ) as run:
                downloader.download(
                    "missing",
                    "https://www.youtube.com/watch?v=missing",
                    ignore_archive=True,
                )

            command = run.call_args.args[0]
            self.assertIn("--no-download-archive", command)
            self.assertNotIn("--download-archive", command)


if __name__ == "__main__":
    unittest.main()
