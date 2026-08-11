from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import logging
import tempfile
import unittest
from unittest import mock

from ytb_tg_backup.config import load_config
from ytb_tg_backup.control import ControlBot, _origin_token, _provider_token
from ytb_tg_backup.extension_api import HttpResponse, SourceProviderDefinition
from ytb_tg_backup.network import NetworkScope
from ytb_tg_backup.extensions import SourceProviderCatalog
from ytb_tg_backup.models import MediaCandidate, Origin
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
    def test_loopback_api_uses_an_opener_with_proxies_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f"""
    [app]
    data_dir = "{tmp}"

    [telegram]
    bot_token = "secret-token"

    [telegram.bot_api]
    api_base = "https://telegram.example"

    [control]
    api_base = "http://[::1]:18081"
    """.strip()
            )
            config = load_config(config_path)
            self.assertEqual(config.telegram.bot_api.api_base, "https://telegram.example")
            self.assertEqual(config.control.api_base, "http://[::1]:18081")
            bot = ControlBot(config, mock.Mock(), logging.getLogger("test"))
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = b'{"ok": true, "result": true}'
            opener = mock.Mock()
            opener.open.return_value = response
            proxy_handler = mock.sentinel.proxy_handler

            with (
                mock.patch(
                    "ytb_tg_backup.control.ProxyHandler",
                    return_value=proxy_handler,
                ) as proxy_handler_factory,
                mock.patch(
                    "ytb_tg_backup.control.build_opener",
                    return_value=opener,
                ) as build_opener,
                mock.patch("ytb_tg_backup.control.urlopen") as urlopen,
            ):
                result = bot._api("getMe", {}, request_timeout_seconds=17)

        self.assertTrue(result["ok"])
        proxy_handler_factory.assert_called_once_with({})
        build_opener.assert_called_once_with(proxy_handler)
        urlopen.assert_not_called()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://[::1]:18081/botsecret-token/getMe")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 17)

    def test_non_loopback_api_keeps_using_urlopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f"""
    [app]
    data_dir = "{tmp}"

    [telegram]
    bot_token = "secret-token"

    [telegram.bot_api]
    api_base = "https://telegram.example"
    """.strip()
            )
            config = load_config(config_path)
            bot = ControlBot(config, mock.Mock(), logging.getLogger("test"))
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = b'{"ok": true, "result": true}'

            with (
                mock.patch("ytb_tg_backup.control.build_opener") as build_opener,
                mock.patch(
                    "ytb_tg_backup.control.urlopen",
                    return_value=response,
                ) as urlopen,
            ):
                result = bot._api("getMe", {})

        self.assertTrue(result["ok"])
        build_opener.assert_not_called()
        urlopen.assert_called_once()

    def test_control_api_override_is_used_by_the_routed_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f"""
[app]
data_dir = "{tmp}"

[telegram]
bot_token = "secret-token"

[telegram.bot_api]
api_base = "https://telegram.example"

[control]
api_base = "http://127.0.0.1:18081"
""".strip()
            )
            config = load_config(config_path)
            connection = mock.Mock()
            connection.request.return_value = HttpResponse(
                200,
                "http://127.0.0.1:18081/bottest/getUpdates",
                {},
                b'{"ok": true, "result": []}',
            )
            bot = ControlBot(
                config,
                mock.Mock(),
                logging.getLogger("test"),
                connection=connection,
            )

            result = bot._api("getUpdates", {}, request_timeout_seconds=15)

        self.assertTrue(result["ok"])
        request, route_request = connection.request.call_args.args
        self.assertEqual(
            request.url,
            "http://127.0.0.1:18081/botsecret-token/getUpdates",
        )
        self.assertEqual(route_request.target_url, request.url)
        self.assertEqual(route_request.scope, NetworkScope.TELEGRAM_CONTROL_RECEIVE)
        self.assertEqual(request.timeout_seconds, 15)

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

    def test_authorization_and_catalog_origin_add(self):
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
                self.assertEqual(channel_ref, "@nightmare")
                return "UCnightmare11111111111111"

            with mock.patch("ytb_tg_backup.control.resolve_channel_id", side_effect=fake_resolve):
                reply = bot._execute(
                    '/origin add youtube "@nightmare" "Nightmare ASMR"',
                    message,
                )
                self.assertIn("added:", reply)

            origins = store.list_origins(managed_by="catalog")
            self.assertEqual(len(origins), 1)
            self.assertEqual(origins[0].external_id, "UCnightmare11111111111111")
            self.assertEqual(origins[0].name, "Nightmare ASMR")
            self.assertTrue((Path(tmp) / "sources.toml").is_file())

            help_text = bot._execute("/help", message)
            self.assertNotIn("/sub add", help_text)
            self.assertIn("/panel", help_text)
            self.assertIn("/origin add twitch", help_text)
            self.assertIn("/origin rename", help_text)
            self.assertIn("/origin history", help_text)
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
            self.assertEqual({row["managed_by"] for row in rows}, {"catalog"})
            self.assertTrue(
                all(str(row["id"]).startswith("source:") for row in rows)
            )
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
            self.assertEqual(vod_options["created_from"], "telegram_panel")
            self.assertIn(
                "renamed",
                bot._execute(f'/origin rename {twitch_vod_id} "Renamed VOD"', message),
            )
            self.assertIn(
                "historical import requested",
                bot._execute(f"/origin history {twitch_vod_id}", message),
            )
            renamed = store.conn.execute(
                "SELECT name, bootstrap, options_json FROM origins WHERE id=?",
                (twitch_vod_id,),
            ).fetchone()
            self.assertEqual(renamed["name"], "Renamed VOD")
            self.assertEqual(renamed["bootstrap"], "all")
            self.assertEqual(
                json.loads(str(renamed["options_json"]))["created_from"],
                "telegram_panel",
            )
            self.assertIn("mode=live", bot._execute("/origin list", message))
            self.assertIn("disabled", bot._execute(f"/origin disable {twitch_id}", message))
            self.assertIn("enabled", bot._execute(f"/origin enable {twitch_id}", message))
            self.assertIn("deleted origin", bot._execute(f"/origin del {twitch_id}", message))

    def test_panel_source_identity_uses_provider_specific_case_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f"""
[app]
data_dir = "{tmp}"

[twitch]
client_id = "test-client"
access_token = "test-token"

[[origins]]
id = "existing-twitch"
provider = "twitch"
kind = "vods"
name = "Existing Twitch"
external_id = "ExampleStreamer"
recording_mode = "vod"

[control]
enabled = true
""".strip()
            )
            config = load_config(config_path)
            store = Store(config.db_path)
            store.initialize()
            bot = ControlBot(config, store, logging.getLogger("test"))
            message = {"from": {"id": 123}, "chat": {"id": -100}}

            twitch_reply = bot._origin_add(
                ["twitch", "vods", "examplestreamer", "Updated Twitch"],
                message,
                recording_mode="live",
            )
            self.assertIn("already exists; enabled: existing-twitch", twitch_reply)
            self.assertIn("mode=live", twitch_reply)
            twitch_rows = store.list_origins(managed_by="catalog")
            self.assertEqual(len(twitch_rows), 1)
            self.assertEqual(twitch_rows[0].external_id, "ExampleStreamer")
            self.assertEqual(twitch_rows[0].options["recording_mode"], "live")

            with mock.patch(
                "ytb_tg_backup.control.resolve_channel_id",
                side_effect=["UCExampleCase", "UCexampleCase"],
            ):
                first_reply = bot._execute(
                    "/origin add youtube @first First Channel",
                    message,
                )
                second_reply = bot._execute(
                    "/origin add youtube @second Second Channel",
                    message,
                )

            self.assertIn("added:", first_reply)
            self.assertIn("added:", second_reply)
            youtube_rows = [
                row
                for row in store.list_origins(managed_by="catalog")
                if row.provider == "youtube"
            ]
            self.assertEqual(len(youtube_rows), 2)
            self.assertEqual(
                {row.external_id for row in youtube_rows},
                {"UCExampleCase", "UCexampleCase"},
            )

    def test_source_views_hide_legacy_history_origins(self):
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

            with mock.patch(
                "ytb_tg_backup.control.resolve_channel_id",
                return_value="UCCatalogSource",
            ):
                self.assertIn(
                    "added:",
                    bot._execute(
                        "/origin add youtube @catalog Catalog Source",
                        message,
                    ),
                )
            store.upsert_origin(
                Origin(
                    id="historical-origin",
                    provider="youtube",
                    kind="uploads",
                    name="Historical Only",
                    external_id="UCHistoricalOnly",
                    enabled=True,
                ),
                managed_by="legacy",
            )

            command_text = bot._execute("/origin list", message)
            home_text, _ = bot._render_home_panel()
            origins_text, origins_keyboard = bot._render_origins_panel({})
            callbacks = {
                str(button["callback_data"])
                for row in origins_keyboard
                for button in row
                if "callback_data" in button
            }

            self.assertIn("Catalog Source", command_text)
            self.assertNotIn("Historical Only", command_text)
            self.assertIn("来源：1/1 已启用", home_text)
            self.assertIn("Catalog Source", origins_text)
            self.assertNotIn("Historical Only", origins_text)
            self.assertFalse(
                any(_origin_token("historical-origin") in value for value in callbacks)
            )

    def test_legacy_config_origin_is_migrated_and_editable_as_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f"""
[app]
data_dir = "{tmp}"

[twitch]
client_id = "test-client"
access_token = "test-token"

[[origins]]
id = "legacy-vod"
provider = "twitch"
kind = "vods"
name = "Legacy VOD"
external_id = "legacy_streamer"
enabled = true
bootstrap = "latest"
recording_mode = "vod"
future_option = "keep-me"
""".strip()
            )
            config = load_config(config_path)
            store = Store(config.db_path)
            store.initialize()
            bot = ControlBot(config, store, logging.getLogger("test"))
            message = {"from": {"id": 123}, "chat": {"id": -100}}

            self.assertIn(
                "renamed",
                bot._execute('/origin rename legacy-vod "Catalog VOD"', message),
            )
            self.assertTrue(config.sources.path.exists())
            row = store.conn.execute(
                "SELECT * FROM origins WHERE id='legacy-vod'"
            ).fetchone()
            self.assertEqual(row["managed_by"], "catalog")
            self.assertEqual(row["name"], "Catalog VOD")
            self.assertEqual(
                json.loads(str(row["options_json"]))["future_option"],
                "keep-me",
            )
            self.assertIn(
                "disabled",
                bot._execute("/origin disable legacy-vod", message),
            )
            self.assertFalse(
                bool(
                    store.conn.execute(
                        "SELECT enabled FROM origins WHERE id='legacy-vod'"
                    ).fetchone()["enabled"]
                )
            )

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
                vod_kind_callback = _current_panel_callback(
                    calls,
                    "p:addtwkind:vods",
                )
                bot._handle_update(
                    {
                        "callback_query": {
                            "id": "cb-add-kind",
                            "from": {"id": 123},
                            "message": panel_message,
                            "data": vod_kind_callback,
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

    def test_panel_discovers_and_adds_an_extension_source_provider(self):
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

            def validate_niconico(origin: Origin) -> Origin:
                keyword = origin.external_id.strip()
                if not keyword:
                    raise ValueError("Niconico keyword must not be empty")
                return replace(
                    origin,
                    external_id=keyword,
                    options={"max_results": 20},
                )

            definition = SourceProviderDefinition(
                provider="niconico",
                kinds=frozenset({"live_search"}),
                default_kind="live_search",
                adapter_factory=lambda _context: mock.Mock(),
                validate_origin=validate_niconico,
                identity=lambda origin: (
                    "niconico",
                    "live_search",
                    origin.external_id.strip().casefold(),
                    str(origin.options["max_results"]),
                ),
                is_live_origin=lambda _origin: True,
                seed_content_kind=lambda _origin: "live_stream",
                poll_variant=lambda _origin: "live-search-v1",
            )
            providers = SourceProviderCatalog({"niconico": definition})
            bot = ControlBot(
                config,
                store,
                logging.getLogger("test"),
                providers=providers,
            )
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
            add_callback_base = f"p:addprovider:{_provider_token('niconico')}"

            with mock.patch.object(bot, "_api", side_effect=fake_api):
                bot._handle_update({"message": command_message})
                add_callback = _current_panel_callback(calls, add_callback_base)
                bot._handle_update(
                    {
                        "callback_query": {
                            "id": "cb-add-niconico",
                            "from": {"id": 123},
                            "message": panel_message,
                            "data": add_callback,
                        }
                    }
                )
                prompt = next(
                    payload["text"]
                    for method, payload in reversed(calls)
                    if method == "editMessageText"
                )
                self.assertIn("Niconico", prompt)
                self.assertIn("niconico/live_search", prompt)

                bot._handle_update(
                    {
                        "message": {
                            "message_id": 11,
                            "from": {"id": 123},
                            "chat": {"id": -100},
                            "text": 'ASMR "Niconico ASMR"',
                        }
                    }
                )

            origins = store.list_origins(managed_by="catalog")
            self.assertEqual(len(origins), 1)
            self.assertEqual(origins[0].provider, "niconico")
            self.assertEqual(origins[0].kind, "live_search")
            self.assertEqual(origins[0].external_id, "ASMR")
            self.assertEqual(origins[0].name, "Niconico ASMR")
            self.assertEqual(origins[0].bootstrap, "all")
            self.assertEqual(origins[0].options, {"max_results": 20})
            self.assertIn(
                "/origin add niconico <external_id> [name]",
                bot._help(),
            )
            latest_panel = next(
                payload
                for method, payload in reversed(calls)
                if method == "editMessageText"
            )
            self.assertIn("niconico/live_search", latest_panel["text"])

            duplicate = bot._execute(
                "/origin add niconico asmr Duplicate",
                command_message,
            )
            self.assertIn("already exists; enabled", duplicate)
            self.assertEqual(len(store.list_origins(managed_by="catalog")), 1)

    def test_twitch_panel_selects_kind_before_vod_recording_mode(self):
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

            for kind, label in (("highlights", "Highlights"), ("uploads", "Uploads")):
                with self.subTest(kind=kind):
                    state: dict[str, object] = {}
                    bot._apply_panel_action("p:addtw", state, message)
                    self.assertEqual(state["view"], "twitch_kind")
                    bot._apply_panel_action(f"p:addtwkind:{kind}", state, message)
                    self.assertEqual(state["view"], "input")
                    self.assertEqual(state["awaiting"], "add_twitch")
                    self.assertEqual(state["twitch_kind"], kind)
                    self.assertIsNone(state["twitch_mode"])
                    prompt, _ = bot._render_panel(state)
                    self.assertIn(label, prompt)
                    self.assertNotIn("直播中录制", prompt)

            vods: dict[str, object] = {}
            bot._apply_panel_action("p:addtw", vods, message)
            bot._apply_panel_action("p:addtwkind:vods", vods, message)
            self.assertEqual(vods["view"], "twitch_mode")
            self.assertIsNone(vods["awaiting"])
            mode_text, mode_markup = bot._render_panel(vods)
            self.assertIn("直播中录制", mode_text)
            callbacks = {
                button["callback_data"]
                for row in mode_markup["inline_keyboard"]
                for button in row
            }
            self.assertIn("p:addtwmode:live", callbacks)

    def test_panel_browses_searches_and_deletes_tracked_disk_resource(self):
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
allow_disk_delete = true
allowed_user_ids = ["123"]
allowed_chat_ids = ["-100"]
""".strip()
            )
            config = load_config(config_path)
            store = Store(config.db_path)
            store.initialize()
            store.upsert_origin(
                Origin(
                    "youtube-asmr",
                    "youtube",
                    "uploads",
                    "Quiet ASMR",
                    "UC-quiet",
                )
            )
            media_id, _ = store.upsert_discovered(
                "youtube-asmr",
                MediaCandidate(
                    provider="youtube",
                    content_kind="video",
                    external_id="whisper-1",
                    title="Soft Whisper ASMR",
                    url="https://www.youtube.com/watch?v=whisper-1",
                    published_at="2026-07-26T12:00:00+00:00",
                ),
            )
            resource_dir = config.download_dir / "youtube" / "Quiet ASMR"
            resource_dir.mkdir(parents=True)
            resource_path = resource_dir / "Soft Whisper ASMR_whisper-1.m4a"
            resource_path.write_bytes(b"audio")
            download = store.claim_next_job(
                ("download",),
                owner="download",
                lease_seconds=60,
            )
            artifact_id = store.complete_download(
                download,
                path=resource_path,
                size_bytes=5,
            )
            self.assertEqual(download.media_id, media_id)

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
                resources_callback = _current_panel_callback(
                    calls,
                    "p:resources:0",
                )
                bot._handle_update(
                    {
                        "callback_query": {
                            "id": "cb-resources",
                            "from": {"id": 123},
                            "message": panel_message,
                            "data": resources_callback,
                        }
                    }
                )
                library_panel = next(
                    payload
                    for method, payload in reversed(calls)
                    if method == "editMessageText"
                )
                self.assertIn("本地资源", library_panel["text"])
                self.assertIn("Soft Whisper ASMR", library_panel["text"])

                search_callback = _current_panel_callback(calls, "p:ressearch")
                bot._handle_update(
                    {
                        "callback_query": {
                            "id": "cb-search",
                            "from": {"id": 123},
                            "message": panel_message,
                            "data": search_callback,
                        }
                    }
                )
                bot._handle_update(
                    {
                        "message": {
                            "message_id": 11,
                            "from": {"id": 123},
                            "chat": {"id": -100},
                            "text": "quiet whisper-1",
                        }
                    }
                )
                searched_panel = next(
                    payload
                    for method, payload in reversed(calls)
                    if method == "editMessageText"
                )
                self.assertIn("搜索：quiet whisper-1", searched_panel["text"])
                self.assertIn("Soft Whisper ASMR", searched_panel["text"])

                detail_callback = _current_panel_callback(
                    calls,
                    f"p:resource:{artifact_id}",
                )
                bot._handle_update(
                    {
                        "callback_query": {
                            "id": "cb-detail",
                            "from": {"id": 123},
                            "message": panel_message,
                            "data": detail_callback,
                        }
                    }
                )
                detail_panel = next(
                    payload
                    for method, payload in reversed(calls)
                    if method == "editMessageText"
                )
                self.assertIn("本地资源详情", detail_panel["text"])
                self.assertIn(
                    "youtube/Quiet ASMR/Soft Whisper ASMR_whisper-1.m4a",
                    detail_panel["text"],
                )

                ask_callback = _current_panel_callback(
                    calls,
                    f"p:resdelask:{artifact_id}",
                )
                bot._handle_update(
                    {
                        "callback_query": {
                            "id": "cb-delete-ask",
                            "from": {"id": 123},
                            "message": panel_message,
                            "data": ask_callback,
                        }
                    }
                )
                confirm_panel = next(
                    payload
                    for method, payload in reversed(calls)
                    if method == "editMessageText"
                )
                self.assertIn("永久删除本地备份", confirm_panel["text"])
                self.assertIn("不会自动重新下载", confirm_panel["text"])

                delete_callback = _current_panel_callback(
                    calls,
                    f"p:resdelete:{artifact_id}",
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

            self.assertFalse(resource_path.exists())
            self.assertEqual(store.get_artifact(media_id)["state"], "purged")
            final_panel = next(
                payload
                for method, payload in reversed(calls)
                if method == "editMessageText"
            )
            self.assertIn("已删除 Soft Whisper ASMR", final_panel["text"])
            self.assertIn("没有匹配的受管本地资源", final_panel["text"])
            self.assertEqual(
                sum(method == "sendMessage" for method, _ in calls),
                1,
            )

    def test_panel_disk_delete_actions_are_rejected_by_default(self):
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
            self.assertFalse(config.control.allow_disk_delete)
            store = Store(config.db_path)
            store.initialize()
            store.upsert_origin(
                Origin(
                    "youtube-asmr",
                    "youtube",
                    "uploads",
                    "Quiet ASMR",
                    "UC-quiet",
                )
            )
            media_id, _ = store.upsert_discovered(
                "youtube-asmr",
                MediaCandidate(
                    provider="youtube",
                    content_kind="video",
                    external_id="readonly-1",
                    title="Read-only ASMR",
                    url="https://www.youtube.com/watch?v=readonly-1",
                    published_at=None,
                ),
            )
            resource_path = config.download_dir / "youtube" / "readonly-1.m4a"
            resource_path.parent.mkdir(parents=True)
            resource_path.write_bytes(b"audio")
            download = store.claim_next_job(
                ("download",),
                owner="download",
                lease_seconds=60,
            )
            artifact_id = store.complete_download(
                download,
                path=resource_path,
                size_bytes=5,
            )
            bot = ControlBot(config, store, logging.getLogger("test"))
            message = {"from": {"id": 123}, "chat": {"id": -100}}

            for action in ("resdelask", "resdelete"):
                with self.subTest(action=action), self.assertRaisesRegex(
                    ValueError,
                    "本地删除未启用",
                ):
                    bot._apply_panel_action(
                        f"p:{action}:{artifact_id}",
                        {
                            "view": "resource_detail",
                            "target_artifact_id": artifact_id,
                            "target_resource_revision": "forged",
                        },
                        message,
                    )

            self.assertTrue(resource_path.exists())
            self.assertEqual(store.get_artifact(media_id)["state"], "ready")

    def test_panel_stale_resource_confirmation_cannot_delete_new_artifacts(self):
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
allow_disk_delete = true
allowed_user_ids = ["123"]
allowed_chat_ids = ["-100"]
""".strip()
            )
            config = load_config(config_path)
            store = Store(config.db_path)
            store.initialize()
            store.upsert_origin(
                Origin(
                    "youtube-asmr",
                    "youtube",
                    "uploads",
                    "Quiet ASMR",
                    "UC-quiet",
                )
            )
            media_id, _ = store.upsert_discovered(
                "youtube-asmr",
                MediaCandidate(
                    provider="youtube",
                    content_kind="video",
                    external_id="stale-confirmation-1",
                    title="Stale Confirmation ASMR",
                    url=(
                        "https://www.youtube.com/watch?"
                        "v=stale-confirmation-1"
                    ),
                    published_at=None,
                ),
            )
            resource_path = (
                config.download_dir / "youtube" / "stale-confirmation-1.m4a"
            )
            resource_path.parent.mkdir(parents=True)
            resource_path.write_bytes(b"audio")
            download = store.claim_next_job(
                ("download",),
                owner="download",
                lease_seconds=60,
            )
            artifact_id = store.complete_download(
                download,
                path=resource_path,
                size_bytes=5,
            )
            bot = ControlBot(config, store, logging.getLogger("test"))
            message = {"from": {"id": 123}, "chat": {"id": -100}}
            state = {
                "view": "resource_detail",
                "target_artifact_id": artifact_id,
            }
            bot._apply_panel_action(
                f"p:resdelask:{artifact_id}",
                state,
                message,
            )
            confirmed_revision = state["target_resource_revision"]

            thumbnail_path = (
                config.download_dir / "youtube" / "stale-confirmation-1.jpg"
            )
            thumbnail_path.write_bytes(b"thumbnail")
            store.record_artifact(
                media_id,
                role="thumbnail",
                path=thumbnail_path,
                size_bytes=9,
            )

            with self.assertRaisesRegex(
                ValueError,
                "changed after confirmation",
            ):
                bot._apply_panel_action(
                    f"p:resdelete:{artifact_id}",
                    state,
                    message,
                )

            self.assertEqual(
                state["target_resource_revision"],
                confirmed_revision,
            )
            self.assertTrue(resource_path.exists())
            self.assertTrue(thumbnail_path.exists())
            self.assertEqual(
                {
                    str(row["state"])
                    for row in store.conn.execute(
                        "SELECT state FROM artifacts WHERE media_id=?",
                        (media_id,),
                    )
                },
                {"ready"},
            )
            text, keyboard = bot._render_resource_delete_confirm_panel(state)
            self.assertIn("资源在确认期间发生了变化", text)
            callbacks = {
                button["callback_data"]
                for row in keyboard
                for button in row
            }
            self.assertNotIn(f"p:resdelete:{artifact_id}", callbacks)

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

            off = bot._execute("/source_filter off", message)
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
