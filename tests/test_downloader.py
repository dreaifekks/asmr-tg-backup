import unittest
import logging
from pathlib import Path
import subprocess
import tempfile
from unittest import mock

from ytb_tg_backup.config import load_config
from ytb_tg_backup.downloader import Downloader, _bitrate_candidates, _looks_like_thumbnail, _target_audio_bitrate_kbps


class DownloaderHelpersTest(unittest.TestCase):
    def test_target_bitrate_for_long_audio(self):
        bitrate = _target_audio_bitrate_kbps(max_bytes=52_428_800, duration_seconds=10_000)
        self.assertLessEqual(bitrate, 40)
        self.assertGreaterEqual(bitrate, 24)

    def test_bitrate_candidates_end_at_floor(self):
        self.assertEqual(_bitrate_candidates(40)[-1], 24)

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
