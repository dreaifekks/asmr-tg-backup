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
        self.assertEqual(config.telegram.upload_transport, "mtproto")
        self.assertEqual(config.telegram.mtproto.max_upload_bytes, 1_990_000_000)
        self.assertTrue(config.telegram.bot_api.split_large_audio)
        self.assertEqual(config.telegram.bot_api.max_upload_parts, 10)
        self.assertEqual(
            config.telegram.mtproto.session_path,
            config.app.data_dir / "telegram-mtproto.session",
        )
        self.assertFalse(config.control.enabled)
        self.assertEqual(config.control.panel_idle_timeout_seconds, 3600)
        self.assertFalse(config.control.allow_disk_delete)
        self.assertTrue(config.control.delete_webhook_on_startup)

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

    def test_container_environment_overrides_runtime_paths_and_telegram_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            path = tmp_path / "config.toml"
            path.write_text(
                """
[app]
data_dir = "/from-file"

[telegram]
enabled = true
bot_token = "file-token"
chat_id = "@file-channel"
upload_transport = "bot_api"

[telegram.bot_api]
api_base = "https://api.telegram.org"
max_upload_bytes = 52428800
split_large_audio = true
max_upload_parts = 6
""".strip()
            )

            with mock.patch.dict(
                os.environ,
                {
                    "ASMR_TG_BACKUP_DATA_DIR": str(tmp_path / "data"),
                    "TELEGRAM_BOT_TOKEN": "env-token",
                    "TELEGRAM_CHAT_ID": "@env-channel",
                    "TELEGRAM_API_BASE": "http://telegram-bot-api:8081/",
                    "TELEGRAM_MAX_UPLOAD_BYTES": "1990000000",
                },
                clear=True,
            ):
                config = load_config(path)

        self.assertEqual(config.app.data_dir, tmp_path / "data")
        self.assertEqual(config.telegram.bot_token, "env-token")
        self.assertEqual(config.telegram.chat_id, "@env-channel")
        self.assertEqual(config.telegram.api_base, "http://telegram-bot-api:8081/")
        self.assertEqual(config.telegram.max_upload_bytes, 1_990_000_000)
        self.assertEqual(config.telegram.bot_api.max_upload_parts, 6)

    def test_telegram_upload_parts_must_fit_one_media_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("[telegram.bot_api]\nmax_upload_parts = 11")

            with self.assertRaisesRegex(
                ValueError,
                "telegram.bot_api.max_upload_parts must be between 1 and 10",
            ):
                load_config(path)

    def test_mtproto_environment_pair_wins_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
[telegram.mtproto]
api_id = 111
api_hash = "11111111111111111111111111111111"
""".strip()
            )
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "ASMR_TG_MTPROTO_API_ID": "222",
                        "ASMR_TG_MTPROTO_API_HASH": "22222222222222222222222222222222",
                    },
                    clear=True,
                ),
                mock.patch(
                    "ytb_tg_backup.config.official_mtproto_credentials",
                    return_value=(333, "33333333333333333333333333333333"),
                ),
            ):
                config = load_config(path)

        self.assertEqual(config.telegram.mtproto.api_id, 222)
        self.assertEqual(
            config.telegram.mtproto.api_hash,
            "22222222222222222222222222222222",
        )

    def test_mtproto_private_config_pair_wins_over_official_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
[telegram.mtproto]
api_id = 111
api_hash = "11111111111111111111111111111111"
""".strip()
            )
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch(
                    "ytb_tg_backup.config.official_mtproto_credentials",
                    return_value=(333, "33333333333333333333333333333333"),
                ),
            ):
                config = load_config(path)

        self.assertEqual(config.telegram.mtproto.api_id, 111)
        self.assertEqual(
            config.telegram.mtproto.api_hash,
            "11111111111111111111111111111111",
        )

    def test_mtproto_official_defaults_are_last_resort(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("[telegram.mtproto]\n")
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch(
                    "ytb_tg_backup.config.official_mtproto_credentials",
                    return_value=(333, "official-hash"),
                ),
            ):
                config = load_config(path)

        self.assertEqual(config.telegram.mtproto.api_id, 333)
        self.assertEqual(config.telegram.mtproto.api_hash, "official-hash")

    def test_mtproto_environment_half_pair_is_rejected_without_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
[telegram.mtproto]
api_id = 111
api_hash = "file-hash"
""".strip()
            )
            with (
                mock.patch.dict(
                    os.environ,
                    {"ASMR_TG_MTPROTO_API_ID": "222"},
                    clear=True,
                ),
                self.assertRaisesRegex(ValueError, "must provide both values together"),
            ):
                load_config(path)

    def test_mtproto_config_half_pair_is_rejected_without_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("[telegram.mtproto]\napi_hash = \"only-hash\"")
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch(
                    "ytb_tg_backup.config.official_mtproto_credentials",
                    return_value=(333, "official-hash"),
                ),
                self.assertRaisesRegex(ValueError, "must provide both values together"),
            ):
                load_config(path)

    def test_mtproto_api_hash_must_be_exactly_32_hex_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                '[telegram.mtproto]\napi_id = 111\napi_hash = "abcd"'
            )
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                self.assertRaisesRegex(
                    ValueError,
                    "API hash must be a 32-character hexadecimal value",
                ),
            ):
                load_config(path)

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
