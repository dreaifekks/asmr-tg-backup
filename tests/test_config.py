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
        self.assertEqual(config.proxy.url, "")
        self.assertEqual(config.proxy.url_env, "")
        self.assertFalse(config.proxy.sources)
        self.assertFalse(config.proxy.downloads)
        self.assertFalse(config.control.enabled)
        self.assertEqual(config.control.panel_idle_timeout_seconds, 3600)
        self.assertFalse(config.control.allow_disk_delete)
        self.assertTrue(config.control.delete_webhook_on_startup)

    def test_proxy_can_load_inline_http_url_and_scope_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
[proxy]
url = "http://127.0.0.1:7890"
sources = true
downloads = false
""".strip()
            )

            config = load_config(path)

        self.assertEqual(config.proxy.url, "http://127.0.0.1:7890")
        self.assertEqual(config.proxy.url_env, "")
        self.assertTrue(config.proxy.sources)
        self.assertFalse(config.proxy.downloads)

    def test_proxy_url_can_be_loaded_from_environment_without_mutating_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
[proxy]
url_env = "ASMR_TG_BACKUP_PROXY"
sources = false
downloads = true
""".strip()
            )
            original = {"ASMR_TG_BACKUP_PROXY": "socks5://127.0.0.1:7891"}

            with mock.patch.dict(os.environ, original, clear=True):
                config = load_config(path)
                self.assertEqual(dict(os.environ), original)

        self.assertEqual(config.proxy.url, "socks5://127.0.0.1:7891")
        self.assertEqual(config.proxy.url_env, "ASMR_TG_BACKUP_PROXY")
        self.assertFalse(config.proxy.sources)
        self.assertTrue(config.proxy.downloads)

    def test_proxy_url_and_url_env_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
[proxy]
url = "http://127.0.0.1:7890"
url_env = "ASMR_TG_BACKUP_PROXY"
""".strip()
            )

            with mock.patch.dict(
                os.environ,
                {"ASMR_TG_BACKUP_PROXY": "http://127.0.0.1:7890"},
                clear=True,
            ), self.assertRaisesRegex(ValueError, "mutually exclusive"):
                load_config(path)

    def test_proxy_url_env_must_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('[proxy]\nurl_env = "ASMR_TG_BACKUP_PROXY"')

            with mock.patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(
                ValueError,
                "ASMR_TG_BACKUP_PROXY.*not set",
            ):
                load_config(path)

    def test_proxy_url_env_must_not_resolve_to_an_empty_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('[proxy]\nurl_env = "ASMR_TG_BACKUP_PROXY"')

            with mock.patch.dict(
                os.environ,
                {"ASMR_TG_BACKUP_PROXY": "   "},
                clear=True,
            ), self.assertRaises(ValueError):
                load_config(path)

    def test_source_proxy_requires_http_while_download_only_accepts_socks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
[proxy]
url = "socks5://127.0.0.1:7891"
sources = true
""".strip()
            )
            with self.assertRaisesRegex(ValueError, "sources=true requires an http or https"):
                load_config(path)

            path.write_text(
                """
[proxy]
url = "socks5://127.0.0.1:7891"
sources = false
downloads = true
""".strip()
            )
            config = load_config(path)

        self.assertEqual(config.proxy.url, "socks5://127.0.0.1:7891")

    def test_proxy_rejects_unusable_port_and_whitespace_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            for proxy_url, message in (
                ("http://127.0.0.1:0", "port"),
                ("http://proxy host:7890", "whitespace"),
            ):
                with self.subTest(proxy_url=proxy_url):
                    path.write_text(f'[proxy]\nurl = "{proxy_url}"')
                    with self.assertRaisesRegex(ValueError, message):
                        load_config(path)

    def test_disk_delete_requires_explicit_control_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
[control]
allow_disk_delete = true
""".strip()
            )

            config = load_config(path)

        self.assertTrue(config.control.allow_disk_delete)

        with tempfile.TemporaryDirectory() as tmp:
            invalid_path = Path(tmp) / "config.toml"
            invalid_path.write_text(
                """
[control]
allow_disk_delete = "false"
""".strip()
            )
            with self.assertRaisesRegex(
                ValueError,
                "control.allow_disk_delete must be true or false",
            ):
                load_config(invalid_path)

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
