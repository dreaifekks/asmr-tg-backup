from pathlib import Path
import logging
import tempfile
import unittest
from unittest import mock

from ytb_tg_backup.config import load_config
from ytb_tg_backup.control import ControlBot
from ytb_tg_backup.source_filter import SOURCE_FILTER_STATE_KEY
from ytb_tg_backup.store import Store


class ControlBotTest(unittest.TestCase):
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
            self.assertTrue(bot._authorized({"from": {"id": 456}, "chat": {"id": -100}, "message_thread_id": 99}))
            self.assertTrue(bot._authorized({"from": {"id": 456}, "chat": {"id": -200}, "message_thread_id": 42}))
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
            self.assertIn("Default route is live", help_text)
            self.assertIn("Default source filter is /ASMR/i", help_text)
            self.assertIn("Push caption", help_text)

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


if __name__ == "__main__":
    unittest.main()
