import unittest
from pathlib import Path
from unittest import mock

from ytb_tg_backup.downloader import _bitrate_candidates, _looks_like_thumbnail, _target_audio_bitrate_kbps


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


if __name__ == "__main__":
    unittest.main()
