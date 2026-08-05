from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

from ytb_tg_backup.config import load_config


class ConfigTest(unittest.TestCase):
    def _load_text(self, text: str):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(text.strip())
            return load_config(path)

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
        self.assertFalse(config.control.allow_disk_delete)
        self.assertTrue(config.control.delete_webhook_on_startup)
        self.assertFalse(config.dev.youtube_membership.enabled)
        self.assertFalse(config.dev.youtube_membership.notify)
        self.assertEqual(config.dev.youtube_membership.origin_ids, [])
        self.assertEqual(config.dev.youtube_membership.yt_dlp, "yt-dlp")
        self.assertEqual(config.dev.youtube_membership.poll_interval_seconds, 1800)
        self.assertEqual(config.dev.youtube_membership.request_timeout_seconds, 180)
        self.assertEqual(config.dev.youtube_membership.request_spacing_seconds, 5.0)
        self.assertEqual(config.dev.youtube_membership.tab_limit, 30)
        self.assertEqual(config.dev.youtube_membership.chat_id, "")

    def test_dev_youtube_membership_loads_strict_anonymous_notification_config(self):
        config = self._load_text(
            """
[[origins]]
id = "youtube-uploads"
provider = "youtube"
kind = "uploads"
name = "Uploads"
external_id = "UC1111111111111111111111"
enabled = false

[[origins]]
id = "youtube-vod-after-live"
provider = "youtube"
kind = "vod_after_live"
name = "VOD after live"
external_id = "UC2222222222222222222222"
enabled = false

[telegram]
bot_token = "test-token"

[dev.youtube_membership]
enabled = true
notify = true
origin_ids = ["youtube-uploads", "youtube-vod-after-live"]
yt_dlp = "/opt/bin/yt-dlp"
poll_interval_seconds = 1
request_timeout_seconds = 1
request_spacing_seconds = -2
tab_limit = 999
chat_id = "  @member-notices  "
"""
        )

        membership = config.dev.youtube_membership
        self.assertTrue(membership.enabled)
        self.assertTrue(membership.notify)
        self.assertEqual(
            membership.origin_ids,
            ["youtube-uploads", "youtube-vod-after-live"],
        )
        self.assertEqual(membership.yt_dlp, "/opt/bin/yt-dlp")
        self.assertEqual(membership.poll_interval_seconds, 300)
        self.assertEqual(membership.request_timeout_seconds, 30)
        self.assertEqual(membership.request_spacing_seconds, 0.0)
        self.assertEqual(membership.tab_limit, 100)
        self.assertEqual(membership.chat_id, "@member-notices")

    def test_dev_youtube_membership_sensitive_switches_require_real_booleans(self):
        for key in ("enabled", "notify"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(
                    ValueError,
                    rf"dev\.youtube_membership\.{key} must be true or false",
                ):
                    self._load_text(
                        f"""
[dev.youtube_membership]
{key} = "false"
"""
                    )

    def test_dev_youtube_membership_rejects_unknown_or_auth_related_fields(self):
        with self.assertRaisesRegex(ValueError, "unsupported dev option.*cookies"):
            self._load_text(
                """
[dev]
cookies = "/tmp/cookies.txt"
"""
            )

        values = {
            "auth": "true",
            "cookies": '"/tmp/cookies.txt"',
            "extra_args": "[]",
            "download": "true",
        }
        for key, value in values.items():
            with self.subTest(key=key):
                with self.assertRaisesRegex(
                    ValueError,
                    rf"unsupported dev\.youtube_membership option.*{key}",
                ):
                    self._load_text(
                        f"""
[dev.youtube_membership]
{key} = {value}
"""
                    )

        base = """
[[origins]]
id = "youtube-main"
provider = "youtube"
external_id = "UC3333333333333333333333"
enabled = false
{origin_option}

[dev.youtube_membership]
enabled = true
origin_ids = ["youtube-main"]
"""
        for option in (
            'cookies = "/tmp/cookies.txt"',
            'auth = "browser"',
            'extra_args = ["--cookies", "/tmp/cookies.txt"]',
            'credential_ref = "youtube-account"',
        ):
            with self.subTest(origin_option=option):
                with self.assertRaisesRegex(ValueError, "anonymous-only"):
                    self._load_text(base.format(origin_option=option))

        with self.assertRaisesRegex(ValueError, "top-level.*auth"):
            self._load_text(
                base.format(origin_option="")
                + "\n[auth]\nenabled = false\n"
            )

    def test_origin_enabled_requires_a_real_boolean(self):
        with self.assertRaisesRegex(
            ValueError,
            "origin 'youtube-main' enabled must be true or false",
        ):
            self._load_text(
                """
[[origins]]
id = "youtube-main"
provider = "youtube"
external_id = "UC3333333333333333333333"
enabled = "false"
"""
            )

    def test_dev_youtube_membership_requires_explicit_valid_origin_whitelist(self):
        invalid_configs = {
            "empty": """
[dev.youtube_membership]
enabled = true
""",
            "missing": """
[[origins]]
id = "youtube-main"
provider = "youtube"
external_id = "UC3333333333333333333333"

[dev.youtube_membership]
enabled = true
origin_ids = ["missing"]
""",
            "enabled-production-origin": """
[[origins]]
id = "youtube-main"
provider = "youtube"
external_id = "UC3333333333333333333333"
enabled = true

[dev.youtube_membership]
enabled = true
origin_ids = ["youtube-main"]
""",
            "wrong-provider": """
[[origins]]
id = "twitch-main"
provider = "twitch"
kind = "vods"
external_id = "streamer"
enabled = false

[dev.youtube_membership]
enabled = true
origin_ids = ["twitch-main"]
""",
            "wrong-kind": """
[[origins]]
id = "youtube-members"
provider = "youtube"
kind = "members"
external_id = "UC3333333333333333333333"
enabled = false

[dev.youtube_membership]
enabled = true
origin_ids = ["youtube-members"]
""",
        }
        expected_errors = {
            "empty": "requires at least one origin_id",
            "missing": "origin_id does not exist: missing",
            "enabled-production-origin": "must reference a disabled dev-only YouTube",
            "wrong-provider": "must reference a disabled dev-only YouTube",
            "wrong-kind": "must reference a disabled dev-only YouTube",
        }
        for case, text in invalid_configs.items():
            with self.subTest(case=case):
                with self.assertRaisesRegex(ValueError, expected_errors[case]):
                    self._load_text(text)

        with self.assertRaisesRegex(ValueError, "resolved UC channel ID"):
            self._load_text(
                """
[[origins]]
id = "youtube-handle"
provider = "youtube"
external_id = "@not-resolved"
enabled = false

[dev.youtube_membership]
enabled = true
origin_ids = ["youtube-handle"]
"""
            )

    def test_dev_youtube_membership_origin_ids_are_strict_and_unique(self):
        for value, expected in (
            ('"youtube-main"', "must be an array"),
            ('["youtube-main", "youtube-main"]', "must not contain duplicate"),
            ('["youtube-main", ""]', "must be an array of non-empty strings"),
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, expected):
                    self._load_text(
                        f"""
[dev.youtube_membership]
origin_ids = {value}
"""
                    )

        with self.assertRaisesRegex(ValueError, "unique YouTube channels"):
            self._load_text(
                """
[[origins]]
id = "dev-one"
provider = "youtube"
external_id = "UC4444444444444444444444"
enabled = false

[[origins]]
id = "dev-two"
provider = "youtube"
external_id = "UC4444444444444444444444"
enabled = false

[dev.youtube_membership]
enabled = true
origin_ids = ["dev-one", "dev-two"]
"""
            )

        with self.assertRaisesRegex(ValueError, "enabled production YouTube"):
            self._load_text(
                """
[[origins]]
id = "dev-only"
provider = "youtube"
external_id = "UC4444444444444444444444"
enabled = false

[[origins]]
id = "production"
provider = "youtube"
external_id = "UC4444444444444444444444"
enabled = true

[dev.youtube_membership]
enabled = true
origin_ids = ["dev-only"]
"""
            )

    def test_dev_youtube_membership_notify_requires_enable_token_and_chat(self):
        with self.assertRaisesRegex(ValueError, "notify=true requires enabled=true"):
            self._load_text(
                """
[dev.youtube_membership]
notify = true
"""
            )

        enabled_origin = """
[[origins]]
id = "youtube-main"
provider = "youtube"
external_id = "UC3333333333333333333333"
enabled = false

[dev.youtube_membership]
enabled = true
notify = true
origin_ids = ["youtube-main"]
chat_id = "@member-notices"
"""
        with self.assertRaisesRegex(ValueError, "requires telegram.bot_token"):
            self._load_text(enabled_origin)

        with self.assertRaisesRegex(ValueError, "requires.*chat_id"):
            self._load_text(
                """
[[origins]]
id = "youtube-main"
provider = "youtube"
external_id = "UC3333333333333333333333"
enabled = false

[telegram]
bot_token = "test-token"

[dev.youtube_membership]
enabled = true
notify = true
origin_ids = ["youtube-main"]
"""
            )

        config = self._load_text(
            """
[[origins]]
id = "youtube-main"
provider = "youtube"
external_id = "UC3333333333333333333333"
enabled = false

[telegram]
bot_token = "test-token"
chat_id = "@default-notices"

[dev.youtube_membership]
enabled = true
notify = true
origin_ids = ["youtube-main"]
"""
        )
        self.assertEqual(config.dev.youtube_membership.chat_id, "")

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
