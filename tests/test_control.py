from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import logging
import tempfile
import unittest
from unittest import mock

from ytb_tg_backup.config import load_config
from ytb_tg_backup.control import ControlBot, _origin_token
from ytb_tg_backup.source_filter import SOURCE_FILTER_STATE_KEY
from ytb_tg_backup.store import Store


def _current_panel_callback(
    calls: list[tuple[str, dict]],
    base_callback_data: str,
) -> str:
    for method, payload in reversed(calls):
        if method not in {"sendMessage", "editMessageText"}:
            continue
        for row in payload["reply_markup"]["inline_keyboard"]:
            for button in row:
                callback_data = str(button.get("callback_data") or "")
                if callback_data.partition("~")[0] == base_callback_data:
                    return callback_data
    raise AssertionError(f"panel callback not found: {base_callback_data}")


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
            ) as api, mock.patch.object(
                bot,
                "expire_idle_panels",
            ) as expire_idle_panels:
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
            expire_idle_panels.assert_called_once_with()

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
            twitch_vod_reply = bot._execute(
                "/origin add twitch @vodstreamer Twitch VOD",
                message,
            )

            self.assertIn("youtube/uploads", youtube_reply)
            self.assertIn("twitch/highlights", twitch_reply)
            self.assertIn("twitch/vods", twitch_vod_reply)
            self.assertIn("mode=vod", twitch_vod_reply)
            rows = store.list_origin_statuses()
            self.assertEqual(
                {(row["provider"], row["kind"], row["external_id"]) for row in rows},
                {
                    ("youtube", "uploads", "UCyoutube1111111111111111"),
                    ("twitch", "highlights", "streamer"),
                    ("twitch", "vods", "vodstreamer"),
                },
            )
            twitch_id = next(
                str(row["id"])
                for row in rows
                if row["provider"] == "twitch" and row["kind"] == "highlights"
            )
            twitch_vod_id = next(
                str(row["id"])
                for row in rows
                if row["provider"] == "twitch" and row["kind"] == "vods"
            )
            self.assertIn("twitch/highlights", bot._execute("/origin list", message))
            self.assertIn(
                "recording mode=live",
                bot._execute(f"/origin mode {twitch_vod_id} live", message),
            )
            vod_options = json.loads(
                str(
                    store.conn.execute(
                        "SELECT options_json FROM origins WHERE id=?",
                        (twitch_vod_id,),
                    ).fetchone()["options_json"]
                )
            )
            self.assertEqual(vod_options["recording_mode"], "live")
            self.assertIn("mode=live", bot._execute("/origin list", message))
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
                add_twitch_callback = _current_panel_callback(calls, "p:addtw")
                bot._handle_update(
                    {
                        "callback_query": {
                            "id": "cb-add",
                            "from": {"id": 123},
                            "message": panel_message,
                            "data": add_twitch_callback,
                        }
                    }
                )
                live_mode_callback = _current_panel_callback(
                    calls,
                    "p:addtwmode:live",
                )
                bot._handle_update(
                    {
                        "callback_query": {
                            "id": "cb-add-mode",
                            "from": {"id": 123},
                            "message": panel_message,
                            "data": live_mode_callback,
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
                options = json.loads(str(rows[0]["options_json"]))
                self.assertEqual(options["recording_mode"], "live")
                token = _origin_token(str(rows[0]["id"]))
                live_panel = next(
                    payload
                    for method, payload in reversed(calls)
                    if method == "editMessageText"
                )
                live_callbacks = {
                    button["callback_data"].partition("~")[0]
                    for row in live_panel["reply_markup"]["inline_keyboard"]
                    for button in row
                }
                self.assertIn("🔴 LIVE", live_panel["text"])
                self.assertIn(f"p:twmode:{token}:vod", live_callbacks)
                switch_to_vod_callback = _current_panel_callback(
                    calls,
                    f"p:twmode:{token}:vod",
                )
                bot._handle_update(
                    {
                        "callback_query": {
                            "id": "cb-mode-vod",
                            "from": {"id": 123},
                            "message": panel_message,
                            "data": switch_to_vod_callback,
                        }
                    }
                )
                switched_options = json.loads(
                    str(
                        store.conn.execute(
                            "SELECT options_json FROM origins WHERE id=?",
                            (rows[0]["id"],),
                        ).fetchone()["options_json"]
                    )
                )
                self.assertEqual(switched_options["recording_mode"], "vod")
                vod_panel = next(
                    payload
                    for method, payload in reversed(calls)
                    if method == "editMessageText"
                )
                vod_callbacks = {
                    button["callback_data"].partition("~")[0]
                    for row in vod_panel["reply_markup"]["inline_keyboard"]
                    for button in row
                }
                self.assertIn("📼 VOD", vod_panel["text"])
                self.assertIn(f"p:twmode:{token}:live", vod_callbacks)
                delete_ask_callback = _current_panel_callback(
                    calls,
                    f"p:delask:{token}",
                )
                bot._handle_update(
                    {
                        "callback_query": {
                            "id": "cb-delete-ask",
                            "from": {"id": 123},
                            "message": panel_message,
                            "data": delete_ask_callback,
                        }
                    }
                )
                delete_callback = _current_panel_callback(
                    calls,
                    f"p:delete:{token}",
                )
                bot._handle_update(
                    {
                        "callback_query": {
                            "id": "cb-delete",
                            "from": {"id": 123},
                            "message": panel_message,
                            "data": delete_callback,
                        }
                    }
                )

            self.assertEqual(store.list_origin_statuses(), [])
            self.assertEqual(sum(method == "sendMessage" for method, _ in calls), 1)
            edits = [payload for method, payload in calls if method == "editMessageText"]
            self.assertGreaterEqual(len(edits), 6)
            self.assertTrue(all(payload["message_id"] == 500 for payload in edits))
            self.assertTrue(all("inline_keyboard" in payload["reply_markup"] for payload in edits))
            self.assertTrue(
                any(
                    "p:addtwmode:live"
                    in {
                        button["callback_data"].partition("~")[0]
                        for row in payload["reply_markup"]["inline_keyboard"]
                        for button in row
                    }
                    for payload in edits
                )
            )

    def test_each_panel_command_sends_a_fresh_panel_and_retires_the_previous_one(self):
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
allowed_message_thread_ids = ["42"]
""".strip()
            )
            config = load_config(config_path)
            store = Store(config.db_path)
            store.initialize()
            bot = ControlBot(config, store, logging.getLogger("test"))
            calls: list[tuple[str, dict]] = []
            next_message_id = iter((500, 501))

            def fake_api(method: str, payload: dict) -> dict:
                calls.append((method, payload))
                if method == "sendMessage":
                    return {
                        "ok": True,
                        "result": {"message_id": next(next_message_id)},
                    }
                return {"ok": True, "result": True}

            first_command = {
                "message_id": 10,
                "from": {"id": 123},
                "chat": {"id": -100},
                "message_thread_id": 42,
                "text": "/panel",
            }
            latest_command = {
                **first_command,
                "message_id": 11,
            }

            with mock.patch.object(bot, "_api", side_effect=fake_api):
                bot._handle_update({"message": first_command})
                first_callback = _current_panel_callback(calls, "p:stats")
                first_revision = first_callback.rpartition("~")[2]
                bot._handle_update({"message": latest_command})

            sends = [payload for method, payload in calls if method == "sendMessage"]
            self.assertEqual(len(sends), 2)
            self.assertEqual(sends[1]["chat_id"], -100)
            self.assertEqual(sends[1]["message_thread_id"], 42)
            self.assertIn("Media Backup 控制面板", sends[1]["text"])
            second_callback = _current_panel_callback(calls, "p:stats")
            self.assertNotEqual(
                second_callback.rpartition("~")[2],
                first_revision,
            )

            retired = [
                payload
                for method, payload in calls
                if method == "editMessageReplyMarkup"
            ]
            self.assertEqual(
                retired,
                [
                    {
                        "chat_id": -100,
                        "message_id": 500,
                        "reply_markup": {"inline_keyboard": []},
                    }
                ],
            )
            state = json.loads(
                str(store.get_bot_state(bot._panel_state_key(latest_command)))
            )
            self.assertEqual(state["message_id"], 501)
            self.assertEqual(state["message_thread_id"], 42)
            self.assertEqual(
                state["panel_revision"],
                second_callback.rpartition("~")[2],
            )

            calls.clear()
            with mock.patch.object(bot, "_api", side_effect=fake_api):
                bot._handle_update(
                    {
                        "callback_query": {
                            "id": "cb-replaced",
                            "from": {"id": 123},
                            "message": {
                                "message_id": 500,
                                "chat": {"id": -100},
                                "message_thread_id": 42,
                            },
                            "data": first_callback,
                        }
                    }
                )

            self.assertEqual([method for method, _ in calls], ["answerCallbackQuery"])
            self.assertIn("不是你当前的会话", calls[0][1]["text"])

            calls.clear()
            with mock.patch.object(bot, "_api", side_effect=fake_api):
                bot._handle_update(
                    {
                        "callback_query": {
                            "id": "cb-current",
                            "from": {"id": 123},
                            "message": {
                                "message_id": 501,
                                "chat": {"id": -100},
                                "message_thread_id": 42,
                            },
                            "data": second_callback,
                        }
                    }
                )

            current_edit = next(
                payload
                for method, payload in calls
                if method == "editMessageText"
            )
            self.assertEqual(current_edit["message_id"], 501)

    def test_panel_closes_after_one_idle_hour_and_reopens_on_command(self):
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
panel_idle_timeout_seconds = 3600
allowed_user_ids = ["123"]
allowed_chat_ids = ["-100"]
""".strip()
            )
            config = load_config(config_path)
            store = Store(config.db_path)
            store.initialize()
            bot = ControlBot(config, store, logging.getLogger("test"))
            calls: list[tuple[str, dict]] = []
            next_message_id = iter((500, 501))

            def fake_api(method: str, payload: dict) -> dict:
                calls.append((method, payload))
                if method == "sendMessage":
                    return {
                        "ok": True,
                        "result": {"message_id": next(next_message_id)},
                    }
                return {"ok": True, "result": True}

            command_message = {
                "message_id": 10,
                "from": {"id": 123},
                "chat": {"id": -100},
                "text": "/panel",
            }
            panel_message = {"message_id": 500, "chat": {"id": -100}}
            started_at = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)

            with mock.patch.object(bot, "_api", side_effect=fake_api), mock.patch(
                "ytb_tg_backup.control._utcnow",
                return_value=started_at,
            ):
                bot._handle_update({"message": command_message})

            state_key = bot._panel_state_key(command_message)
            state = json.loads(str(store.get_bot_state(state_key)))
            self.assertTrue(state["active"])
            self.assertEqual(state["last_activity_at"], started_at.isoformat())
            self.assertEqual(
                state["expires_at"],
                (started_at + timedelta(hours=1)).isoformat(),
            )

            activity_at = started_at + timedelta(minutes=45)
            stats_callback = _current_panel_callback(calls, "p:stats")
            with mock.patch.object(bot, "_api", side_effect=fake_api), mock.patch(
                "ytb_tg_backup.control._utcnow",
                return_value=activity_at,
            ):
                bot._handle_update(
                    {
                        "callback_query": {
                            "id": "cb-stats",
                            "from": {"id": 123},
                            "message": panel_message,
                            "data": stats_callback,
                        }
                    }
                )

            renewed = json.loads(str(store.get_bot_state(state_key)))
            self.assertEqual(renewed["last_activity_at"], activity_at.isoformat())
            self.assertEqual(
                renewed["expires_at"],
                (activity_at + timedelta(hours=1)).isoformat(),
            )
            expired_home_callback = _current_panel_callback(calls, "p:home")
            calls.clear()
            with mock.patch.object(bot, "_api", side_effect=fake_api), mock.patch(
                "ytb_tg_backup.control._utcnow",
                return_value=activity_at + timedelta(seconds=1),
            ):
                bot._handle_update(
                    {
                        "callback_query": {
                            "id": "cb-old-revision",
                            "from": {"id": 123},
                            "message": panel_message,
                            "data": stats_callback,
                        }
                    }
                )
            self.assertEqual([method for method, _ in calls], ["answerCallbackQuery"])
            self.assertIn("已经刷新", calls[0][1]["text"])

            with mock.patch.object(bot, "_api", side_effect=fake_api):
                self.assertEqual(
                    bot.expire_idle_panels(
                        now=activity_at + timedelta(minutes=59, seconds=59)
                    ),
                    0,
                )

            calls.clear()
            with mock.patch.object(bot, "_api", side_effect=fake_api):
                self.assertEqual(
                    bot.expire_idle_panels(now=activity_at + timedelta(hours=1)),
                    1,
                )
            closed = json.loads(str(store.get_bot_state(state_key)))
            self.assertFalse(closed["active"])
            self.assertEqual(closed["view"], "closed")
            close_call = next(
                payload
                for method, payload in calls
                if method == "editMessageText"
            )
            self.assertIn("控制面板已自动关闭", close_call["text"])
            self.assertEqual(
                close_call["reply_markup"],
                {"inline_keyboard": []},
            )

            calls.clear()
            with mock.patch.object(bot, "_api", side_effect=fake_api):
                bot._handle_update(
                    {
                        "callback_query": {
                            "id": "cb-expired",
                            "from": {"id": 123},
                            "message": panel_message,
                            "data": expired_home_callback,
                        }
                    }
                )
            self.assertEqual([method for method, _ in calls], ["answerCallbackQuery"])
            self.assertTrue(calls[0][1]["show_alert"])

            reopened_at = activity_at + timedelta(hours=1, seconds=1)
            calls.clear()
            with mock.patch.object(bot, "_api", side_effect=fake_api), mock.patch(
                "ytb_tg_backup.control._utcnow",
                return_value=reopened_at,
            ):
                bot._handle_update({"message": command_message})

            reopened = json.loads(str(store.get_bot_state(state_key)))
            self.assertTrue(reopened["active"])
            self.assertEqual(reopened["message_id"], 501)
            self.assertEqual(reopened["last_activity_at"], reopened_at.isoformat())
            self.assertTrue(
                any(
                    method == "sendMessage"
                    and "Media Backup 控制面板" in payload["text"]
                    for method, payload in calls
                )
            )
            self.assertTrue(
                any(
                    method == "editMessageReplyMarkup"
                    and payload["message_id"] == 500
                    and payload["reply_markup"] == {"inline_keyboard": []}
                    for method, payload in calls
                )
            )

            current_stats_callback = _current_panel_callback(calls, "p:stats")
            calls.clear()
            with mock.patch.object(bot, "_api", side_effect=fake_api):
                bot._handle_update(
                    {
                        "callback_query": {
                            "id": "cb-old-message",
                            "from": {"id": 123},
                            "message": {"message_id": 500, "chat": {"id": -100}},
                            "data": current_stats_callback,
                        }
                    }
                )
            self.assertEqual([method for method, _ in calls], ["answerCallbackQuery"])
            self.assertIn("不是你当前的会话", calls[0][1]["text"])

    def test_panel_replacement_succeeds_when_old_keyboard_cleanup_fails(self):
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
""".strip()
            )
            config = load_config(config_path)
            store = Store(config.db_path)
            store.initialize()
            bot = ControlBot(config, store, logging.getLogger("test"))
            calls: list[tuple[str, dict]] = []
            next_message_id = iter((500, 501))

            def fake_api(method: str, payload: dict) -> dict:
                calls.append((method, payload))
                if method == "sendMessage":
                    return {
                        "ok": True,
                        "result": {"message_id": next(next_message_id)},
                    }
                if method == "editMessageReplyMarkup":
                    raise RuntimeError("message cannot be edited")
                return {"ok": True, "result": True}

            command_message = {
                "message_id": 10,
                "from": {"id": 123},
                "chat": {"id": -100},
                "text": "/panel",
            }

            with mock.patch.object(bot, "_api", side_effect=fake_api):
                bot._handle_update({"message": command_message})
                bot._handle_update(
                    {
                        "message": {
                            **command_message,
                            "message_id": 11,
                        }
                    }
                )

            self.assertEqual(
                sum(method == "sendMessage" for method, _ in calls),
                2,
            )
            state = json.loads(
                str(store.get_bot_state(bot._panel_state_key(command_message)))
            )
            self.assertEqual(state["message_id"], 501)

            calls.clear()
            with mock.patch.object(bot, "_api", side_effect=fake_api):
                bot._handle_update(
                    {
                        "callback_query": {
                            "id": "cb-replaced",
                            "from": {"id": 123},
                            "message": {
                                "message_id": 500,
                                "chat": {"id": -100},
                            },
                            "data": "p:stats~stale",
                        }
                    }
                )
            self.assertEqual([method for method, _ in calls], ["answerCallbackQuery"])
            self.assertIn("不是你当前的会话", calls[0][1]["text"])

    def test_failed_fresh_panel_send_preserves_the_previous_panel(self):
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
""".strip()
            )
            config = load_config(config_path)
            store = Store(config.db_path)
            store.initialize()
            bot = ControlBot(config, store, logging.getLogger("test"))
            calls: list[tuple[str, dict]] = []
            fail_next_send = False

            def fake_api(method: str, payload: dict) -> dict:
                calls.append((method, payload))
                if method == "sendMessage":
                    if fail_next_send:
                        raise RuntimeError("Telegram unavailable")
                    return {"ok": True, "result": {"message_id": 500}}
                return {"ok": True, "result": True}

            command_message = {
                "message_id": 10,
                "from": {"id": 123},
                "chat": {"id": -100},
                "text": "/panel",
            }
            state_key = bot._panel_state_key(command_message)

            with mock.patch.object(bot, "_api", side_effect=fake_api):
                bot._handle_update({"message": command_message})
            previous_state = str(store.get_bot_state(state_key))

            fail_next_send = True
            calls.clear()
            with mock.patch.object(bot, "_api", side_effect=fake_api):
                with self.assertRaisesRegex(RuntimeError, "Telegram unavailable"):
                    bot._handle_update(
                        {
                            "message": {
                                **command_message,
                                "message_id": 11,
                            }
                        }
                    )

            self.assertEqual(str(store.get_bot_state(state_key)), previous_state)
            self.assertEqual([method for method, _ in calls], ["sendMessage"])

    def test_panel_remains_closed_when_expiry_message_edit_fails(self):
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
panel_idle_timeout_seconds = 3600
allowed_user_ids = ["123"]
""".strip()
            )
            config = load_config(config_path)
            store = Store(config.db_path)
            store.initialize()
            bot = ControlBot(config, store, logging.getLogger("test"))
            message = {"from": {"id": 123}, "chat": {"id": -100}}
            state_key = bot._panel_state_key(message)
            started_at = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
            store.set_bot_state(
                state_key,
                json.dumps(
                    {
                        "active": True,
                        "chat_id": -100,
                        "message_id": 500,
                        "last_activity_at": started_at.isoformat(),
                        "view": "home",
                    }
                ),
            )

            with mock.patch.object(
                bot,
                "_api",
                side_effect=RuntimeError("Telegram unavailable"),
            ):
                self.assertEqual(
                    bot.expire_idle_panels(now=started_at + timedelta(hours=1)),
                    1,
                )

            state = json.loads(str(store.get_bot_state(state_key)))
            self.assertFalse(state["active"])
            self.assertEqual(state["view"], "closed")

    def test_panel_idle_expiry_can_be_disabled(self):
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
panel_idle_timeout_seconds = 0
allowed_user_ids = ["123"]
""".strip()
            )
            config = load_config(config_path)
            store = Store(config.db_path)
            store.initialize()
            bot = ControlBot(config, store, logging.getLogger("test"))
            message = {
                "message_id": 10,
                "from": {"id": 123},
                "chat": {"id": -100},
                "text": "/panel",
            }
            started_at = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)

            with mock.patch.object(
                bot,
                "_api",
                return_value={"ok": True, "result": {"message_id": 500}},
            ), mock.patch(
                "ytb_tg_backup.control._utcnow",
                return_value=started_at,
            ):
                bot._handle_update({"message": message})

            state = json.loads(
                str(store.get_bot_state(bot._panel_state_key(message)))
            )
            self.assertNotIn("expires_at", state)
            self.assertEqual(
                bot.expire_idle_panels(now=started_at + timedelta(days=365)),
                0,
            )

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
