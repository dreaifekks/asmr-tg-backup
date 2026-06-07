from pathlib import Path
import tempfile
import unittest

from ytb_tg_backup.config import load_config


class ConfigTest(unittest.TestCase):
    def test_channels_expand_to_youtube_official_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
[[channels]]
id = "asmr"
name = "ASMR"
channel_id = "UC123"
routes = ["channel", "live"]
enabled = true
""".strip()
            )

            config = load_config(path)

        self.assertEqual(len(config.channels), 1)
        self.assertEqual([feed.id for feed in config.feeds], ["asmr"])
        self.assertEqual([feed.name for feed in config.feeds], ["ASMR (channel,live)"])
        self.assertEqual(
            [feed.url for feed in config.feeds],
            ["https://www.youtube.com/feeds/videos.xml?channel_id=UC123"],
        )
        self.assertEqual(config.app.poll_interval_seconds, 1800)
        self.assertEqual(config.download.format, "bestaudio/best")
        self.assertTrue(config.download.extract_audio)
        self.assertEqual(config.telegram.media_type, "audio")
        self.assertFalse(config.control.enabled)
        self.assertTrue(config.control.delete_webhook_on_startup)


if __name__ == "__main__":
    unittest.main()
