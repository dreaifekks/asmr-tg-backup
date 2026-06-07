import subprocess
import unittest
from unittest import mock

from ytb_tg_backup.youtube import resolve_channel_id, youtube_channel_feed_url


class YouTubeTest(unittest.TestCase):
    def test_youtube_channel_feed_url(self):
        self.assertEqual(
            youtube_channel_feed_url("UC123"),
            "https://www.youtube.com/feeds/videos.xml?channel_id=UC123",
        )

    def test_resolve_channel_id_keeps_uc_id(self):
        channel_id = "UCabcdefghij1234567890"
        self.assertEqual(resolve_channel_id(channel_id, "yt-dlp"), channel_id)

    def test_resolve_channel_id_uses_yt_dlp_for_handle(self):
        completed = subprocess.CompletedProcess(
            ["yt-dlp"],
            0,
            stdout="UCabcdefghij1234567890\n",
            stderr="",
        )
        with (
            mock.patch("ytb_tg_backup.youtube._resolve_channel_id_from_html", return_value=None),
            mock.patch("ytb_tg_backup.youtube.subprocess.run", return_value=completed) as run,
        ):
            self.assertEqual(resolve_channel_id("@rnqqU", "/opt/yt-dlp"), "UCabcdefghij1234567890")

        args = run.call_args.args[0]
        self.assertEqual(args[0], "/opt/yt-dlp")
        self.assertEqual(args[-1], "https://www.youtube.com/@rnqqU")

    def test_resolve_channel_id_prefers_html_feed_link(self):
        with (
            mock.patch("ytb_tg_backup.youtube._resolve_channel_id_from_html", return_value="UCabcdefghij1234567890"),
            mock.patch("ytb_tg_backup.youtube.subprocess.run") as run,
        ):
            self.assertEqual(resolve_channel_id("@rnqqU", "/opt/yt-dlp"), "UCabcdefghij1234567890")
        run.assert_not_called()

    def test_resolve_channel_id_rejects_plain_handle_without_at(self):
        with self.assertRaises(ValueError):
            resolve_channel_id("rnqqU", "yt-dlp")


if __name__ == "__main__":
    unittest.main()
