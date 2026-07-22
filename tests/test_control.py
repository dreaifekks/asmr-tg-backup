from pathlib import Path
import logging
import tempfile
import unittest
from unittest import mock

from ytb_tg_backup.config import load_config
from ytb_tg_backup.control import ControlBot, _origin_token
from ytb_tg_backup.source_filter import SOURCE_FILTER_STATE_KEY
from ytb_tg_backup.store import Store


class ControlBotTest(unittest.TestCase):
    def test_get_updates_uses_long_poll_and_a_longer_http_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f"""
[app]
data_dir = "{tmp}"

[telegram]
bot_token = "test-token"

[control]
enabled = true
poll_interval_seconds = 10
allowed_user_ids = ["123"]
""".strip()
            )
            config = load_config(config_path)
            store = Store(config.db_path)
            store.initialize()
            bot = ControlBot(config, store, logging.getLogger("test"))

            with mock.patch.object(
                bot,
                "_api",
                return_value={"ok": True, "result": []},
            ) as api:
                bot.process_once()

            api.assert_called_once_with(
                "getUpdates",
                {
                    "timeout": 10,
                    "limit": 20,
                    "allowed_updates": ["message", "callback_query"],
                },
                request_timeout_seconds=15,
            )

    def test_authorization_and_sub_add(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f"""
[app]
data_dir = "{tmp}"

[telegram]
bot_token = "token"

[control]
enabled = true
allowed_user_ids = ["123"]
allowed_chat_ids = ["-100"]
allowed_message_thread_ids = ["42"]
default_routes = ["live"]
""".strip()
            )
            config = load_config(config_path)
            store = Store(config.db_path)
            store.initialize()
            bot = ControlBot(config, store, logging.getLogger("test"))
            message = {"from": {"id": 123}, "chat": {"id": -100}, "message_thread_id": 42}

            self.assertTrue(bot._authorized(message))
            self.assertFalse(bot._authorized({"from": {"id": 456}, "chat": {"id": -100}, "message_thread_id": 42}))
            self.assertFalse(bot._authorized({"from": {"id": 123}, "chat": {"id": -200}, "message_thread_id": 42}))
            self.assertFalse(bot._authorized({"from": {"id": 123}, "chat": {"id": -100}, "message_thread_id": 99}))
            self.assertFalse(bot._authorized({"from": {"id": 456}, "chat": {"id": -200}, "message_thread_id": 99}))

            def fake_resolve(channel_ref: str, yt_dlp: str) -> str:
                return {
                    "@nightmare": "UCnightmare11111111111111",
                    "@nightmare2": "UCnightmare22222222222222",
                    "@nightmare3": "UCnightmare33333333333333",
                }[channel_ref]

            with mock.patch("ytb_tg_backup.control.resolve_channel_id", side_effect=fake_resolve):
                reply = bot._execute('/sub_add n1 "@nightmare" "Nightmare ASMR"', message)
                self.assertIn("added: n1", reply)
                self.assertEqual(store.list_subscriptions()[0].channel_id, "UCnightmare11111111111111")
                self.assertEqual(store.list_subscriptions()[0].routes, ["live"])

                short_reply = bot._execute("/sub add channel @nightmare2 Nightmare Two", message)
                self.assertIn("added: channel@nightmare2", short_reply)
                by_id = {sub.id: sub for sub in store.list_subscriptions()}
                self.assertEqual(by_id["channel@nightmare2"].channel_id, "UCnightmare22222222222222")
                self.assertEqual(by_id["channel@nightmare2"].routes, ["channel"])
                self.assertEqual(by_id["channel@nightmare2"].name, "Nightmare Two")

                default_route_reply = bot._execute("/sub add @nightmare3", message)
                self.assertIn("added: live@nightmare3", default_route_reply)
                by_id = {sub.id: sub for sub in store.list_subscriptions()}
                self.assertEqual(by_id["live@nightmare3"].channel_id, "UCnightmare33333333333333")
                self.assertEqual(by_id["live@nightmare3"].routes, ["live"])
                self.assertEqual(by_id["live@nightmare3"].name, "nightmare3")

            help_text = bot._execute("/help", message)
            self.assertIn("/sub add", help_text)
            self.assertIn("/panel", help_text)
            self.assertIn("/origin add twitch", help_text)
            self.assertIn("Default source filter is /ASMR/i", help_text)

    def test_provider_neutral_origin_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f"""
[app]
data_dir = "{tmp}"

[twitch]
client_id = "test-client"
access_token = "test-token"

[control]
enabled = true
allowed_user_ids = ["123"]
""".strip()
            )
            config = load_config(config_path)
            store = Store(config.db_path)
            store.initialize()
            bot = ControlBot(config, store, logging.getLogger("test"))
            message = {"from": {"id": 123}, "chat": {"id": -100}}

            with mock.patch(
                "ytb_tg_backup.control.resolve_channel_id",
                return_value="UCyoutube1111111111111111",
            ):
                youtube_reply = bot._execute(
                    "/origin add youtube @youtube ASMR YouTube",
                    message,
                )
            twitch_reply = bot._execute(
                "/origin add twitch highlights @streamer Twitch ASMR",
                message,
            )

            self.assertIn("youtube/uploads", youtube_reply)
            self.assertIn("twitch/highlights", twitch_reply)
            rows = store.list_origin_statuses()
            self.assertEqual(
                {(row["provider"], row["kind"], row["external_id"]) for row in rows},
                {
                    ("youtube", "uploads", "UCyoutube1111111111111111"),
                    ("twitch", "highlights", "streamer"),
                },
            )
            twitch_id = next(str(row["id"]) for row in rows if row["provider"] == "twitch")
            self.assertIn("twitch/highlights", bot._execute("/origin list", message))
            self.assertIn("disabled", bot._execute(f"/origin disable {twitch_id}", message))
            self.assertIn("enabled", bot._execute(f"/origin enable {twitch_id}", message))
            self.assertIn("deleted origin", bot._execute(f"/origin del {twitch_id}", message))

    def test_single_message_panel_adds_and_deletes_twitch_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f"""
[app]
data_dir = "{tmp}"

[telegram]
bot_token = "test-token"

[control]
enabled = true
allowed_user_ids = ["123"]
allowed_chat_ids = ["-100"]
""".strip()
            )
            config = load_config(config_path)
            store = Store(config.db_path)
            store.initialize()
            bot = ControlBot(config, store, logging.getLogger("test"))
            calls: list[tuple[str, dict]] = []

            def fake_api(method: str, payload: dict) -> dict:
                calls.append((method, payload))
                if method == "sendMessage":
                    return {"ok": True, "result": {"message_id": 500}}
                return {"ok": True, "result": True}

            command_message = {
                "message_id": 10,
                "from": {"id": 123},
                "chat": {"id": -100},
                "text": "/panel",
            }
            panel_message = {"message_id": 500, "chat": {"id": -100}}

            with mock.patch.object(bot, "_api", side_effect=fake_api):
                bot._handle_update({"message": command_message})
                bot._handle_update(
                    {
                        "callback_query": {
                            "id": "cb-add",
                            "from": {"id": 123},
                            "message": panel_message,
                            "data": "p:addtw",
                        }
                    }
                )
                bot._handle_update(
                    {
                        "message": {
                            "message_id": 11,
                            "from": {"id": 123},
                            "chat": {"id": -100},
                            "text": "streamer Twitch ASMR",
                        }
                    }
                )

                rows = store.list_origin_statuses()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["provider"], "twitch")
                self.assertFalse(bool(rows[0]["enabled"]))
                token = _origin_token(str(rows[0]["id"]))
                bot._handle_update(
                    {
                        "callback_query": {
                            "id": "cb-delete-ask",
                            "from": {"id": 123},
                            "message": panel_message,
                            "data": f"p:delask:{token}",
                        }
                    }
                )
                bot._handle_update(
                    {
                        "callback_query": {
                            "id": "cb-delete",
                            "from": {"id": 123},
                            "message": panel_message,
                            "data": f"p:delete:{token}",
                        }
                    }
                )
                bot._handle_update({"message": command_message})

            self.assertEqual(store.list_origin_statuses(), [])
            self.assertEqual(sum(method == "sendMessage" for method, _ in calls), 1)
            edits = [payload for method, payload in calls if method == "editMessageText"]
            self.assertGreaterEqual(len(edits), 4)
            self.assertTrue(all(payload["message_id"] == 500 for payload in edits))
            self.assertTrue(all("inline_keyboard" in payload["reply_markup"] for payload in edits))

    def test_source_filter_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f"""
[app]
data_dir = "{tmp}"

[control]
enabled = true
""".strip()
            )
            config = load_config(config_path)
            store = Store(config.db_path)
            store.initialize()
            bot = ControlBot(config, store, logging.getLogger("test"))
            message = {"from": {"id": 123}, "chat": {"id": -100}}

            self.assertIn("source_filter=/ASMR/i", bot._execute("/source_filter", message))

            reply = bot._execute('/source_filter "ASMR|sleep"', message)
            self.assertIn("source_filter=/ASMR|sleep/i", reply)
            self.assertEqual(store.get_bot_state(SOURCE_FILTER_STATE_KEY), "ASMR|sleep")

            invalid = bot._execute('/source_filter "["', message)
            self.assertIn("error: invalid source regex", invalid)
            self.assertEqual(store.get_bot_state(SOURCE_FILTER_STATE_KEY), "ASMR|sleep")

            off = bot._execute("/sub filter off", message)
            self.assertIn("source_filter=off", off)
            self.assertEqual(store.get_bot_state(SOURCE_FILTER_STATE_KEY), "")

            reset = bot._execute("/filter reset", message)
            self.assertIn("source_filter=/ASMR/i", reset)
            self.assertEqual(store.get_bot_state(SOURCE_FILTER_STATE_KEY), "ASMR")

    def test_empty_allowlists_deny_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f"""
[app]
data_dir = "{tmp}"

[control]
enabled = true
""".strip()
            )
            config = load_config(config_path)
            store = Store(config.db_path)
            store.initialize()
            bot = ControlBot(config, store, logging.getLogger("test"))
            self.assertFalse(bot._authorized({"from": {"id": 123}, "chat": {"id": -100}, "message_thread_id": 42}))

    def test_authorization_only_requires_configured_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f"""
[app]
data_dir = "{tmp}"

[control]
enabled = true
allowed_user_ids = ["123"]
""".strip()
            )
            config = load_config(config_path)
            store = Store(config.db_path)
            store.initialize()
            bot = ControlBot(config, store, logging.getLogger("test"))

            self.assertTrue(bot._authorized({"from": {"id": 123}, "chat": {"id": -200}}))
            self.assertFalse(bot._authorized({"from": {"id": 456}, "chat": {"id": -200}}))


if __name__ == "__main__":
    unittest.main()
