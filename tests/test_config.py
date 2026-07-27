from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

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
        self.assertEqual(
            config.app.data_dir,
            Path.home() / ".local/share/asmr-tg-backup",
        )
        self.assertEqual(config.download.format, "bestaudio/best")
        self.assertTrue(config.download.extract_audio)
        self.assertTrue(config.download.provider_profiles["twitch"].extract_audio)
        self.assertEqual(
            config.download.provider_profiles["twitch"].format,
            "bestaudio/best",
        )
        self.assertEqual(config.telegram.media_type, "audio")
        self.assertFalse(config.control.enabled)
        self.assertEqual(config.control.panel_idle_timeout_seconds, 3600)
        self.assertTrue(config.control.delete_webhook_on_startup)

    def test_twitch_credentials_load_from_default_environment_variables(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
[twitch]
api_base = "https://twitch.example/helix/"
oauth_base = "https://auth.example/oauth2/"
request_timeout_seconds = 12
max_pages_per_poll = 4
""".strip()
            )

            with mock.patch.dict(
                os.environ,
                {
                    "TWITCH_CLIENT_ID": "env-client-id",
                    "TWITCH_ACCESS_TOKEN": "env-access-token",
                    "TWITCH_CLIENT_SECRET": "env-client-secret",
                },
                clear=True,
            ):
                config = load_config(path)

        self.assertEqual(config.twitch.client_id, "env-client-id")
        self.assertEqual(config.twitch.access_token, "env-access-token")
        self.assertEqual(config.twitch.client_secret, "env-client-secret")
        self.assertEqual(config.twitch.api_base, "https://twitch.example/helix")
        self.assertEqual(config.twitch.oauth_base, "https://auth.example/oauth2")
        self.assertEqual(config.twitch.request_timeout_seconds, 12)
        self.assertEqual(config.twitch.max_pages_per_poll, 4)

    def test_explicit_provider_neutral_origins_are_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
[[origins]]
id = "youtube-main"
provider = "YouTube"
name = "YouTube Main"
external_id = "UCabcdefghij1234567890"
routes = ["channel", "live"]

[[origins]]
id = "twitch-main"
provider = "TWITCH"
kind = "highlights"
name = "Twitch Main"
external_id = "example_streamer"
enabled = false
bootstrap = "all"
credential_ref = "twitch-app"
language = "en"
""".strip()
            )

            config = load_config(path)

        self.assertEqual([origin.id for origin in config.origins], ["youtube-main", "twitch-main"])
        youtube, twitch = config.origins
        self.assertEqual(youtube.provider, "youtube")
        self.assertEqual(youtube.kind, "uploads")
        self.assertEqual(youtube.external_id, "UCabcdefghij1234567890")
        self.assertEqual(youtube.options, {"routes": ["channel", "live"]})

        self.assertEqual(twitch.provider, "twitch")
        self.assertEqual(twitch.kind, "highlights")
        self.assertEqual(twitch.external_id, "example_streamer")
        self.assertFalse(twitch.enabled)
        self.assertEqual(twitch.bootstrap, "all")
        self.assertEqual(twitch.credential_ref, "twitch-app")
        self.assertEqual(twitch.options, {"language": "en"})


if __name__ == "__main__":
    unittest.main()
