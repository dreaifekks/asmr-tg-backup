from pathlib import Path
import json
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from ytb_tg_backup.config import BotApiConfig, MtprotoConfig, TelegramConfig
from ytb_tg_backup.network import is_loopback_url
from ytb_tg_backup.telegram import (
    BotApiTransport,
    TelegramUploader,
    TelegramUploadError,
    create_telegram_transport,
    _tag_from_feed_name,
    _upload_filename,
)
from ytb_tg_backup.telegram_mtproto import MtprotoTransport


class _FakeFloodWaitError(Exception):
    def __init__(self, seconds: int):
        super().__init__(f"wait {seconds}")
        self.seconds = seconds


class _FakeRpcError(Exception):
    pass


class _FakeTelethonClient:
    def __init__(
        self,
        session: str,
        api_id: int,
        api_hash: str,
        *,
        receive_updates: bool,
        events: list[object],
        upload_error: Exception | None = None,
        send_error: Exception | None = None,
        message_id: object = 314,
        bot_id: int = 123456,
        is_bot: bool = True,
    ):
        self.session = session
        self.api_id = api_id
        self.api_hash = api_hash
        self.receive_updates = receive_updates
        self.events = events
        self.upload_error = upload_error
        self.send_error = send_error
        self.message_id = message_id
        self.bot_id = bot_id
        self.is_bot = is_bot
        self.send_kwargs: dict[str, object] | None = None
        self.start_calls = 0
        self.disconnect_calls = 0

    async def start(self, *, bot_token: str):
        self.start_calls += 1
        self.events.append(("start", bot_token))
        Path(self.session).write_text("fake session", encoding="utf-8")
        return self

    async def get_me(self):
        self.events.append(("get_me", self.bot_id))
        return SimpleNamespace(id=self.bot_id, bot=self.is_bot)

    async def get_input_entity(self, chat_id: str | int):
        self.events.append(("resolve", chat_id))
        return f"entity:{chat_id}"

    async def upload_file(self, path: str, *, file_name: str):
        self.events.append(("upload", file_name))
        if self.upload_error is not None:
            raise self.upload_error
        return f"uploaded:{file_name}"

    async def send_file(self, entity: object, **kwargs):
        self.events.append(("send", threading.get_ident()))
        self.send_kwargs = {"entity": entity, **kwargs}
        if self.send_error is not None:
            raise self.send_error
        return SimpleNamespace(id=self.message_id)

    async def disconnect(self):
        self.disconnect_calls += 1
        self.events.append("disconnect")


def _fake_telethon_bindings(client_factory):
    def attribute(kind: str):
        return lambda **values: {"kind": kind, **values}

    return SimpleNamespace(
        client_factory=client_factory,
        audio_attribute=attribute("audio"),
        filename_attribute=attribute("filename"),
        video_attribute=attribute("video"),
        flood_wait_error=_FakeFloodWaitError,
        rpc_error=_FakeRpcError,
    )


def _mtproto_config(tmp: str, **overrides) -> TelegramConfig:
    values = {
        "enabled": True,
        "bot_token": "123456:test-bot-token",
        "chat_id": "@archive",
        "upload_transport": "mtproto",
        "media_type": "audio",
        "mtproto": MtprotoConfig(
            api_id=12345,
            api_hash="test-api-hash",
            session_path=Path(tmp) / "telegram.session",
            max_upload_bytes=1_990_000_000,
        ),
    }
    values.update(overrides)
    return TelegramConfig(**values)


class LoopbackUrlTest(unittest.TestCase):
    def test_loopback_url_detection_uses_only_explicit_hosts(self):
        loopback_urls = (
            "http://localhost:8081",
            "https://LOCALHOST/path",
            "http://127.0.0.1:8081",
            "http://127.255.255.254",
            "http://[::1]:8081",
        )
        non_loopback_urls = (
            "https://api.telegram.org",
            "http://localhost.example:8081",
            "http://127.0.0.1.example:8081",
            "http://128.0.0.1:8081",
            "http://[::ffff:127.0.0.1]:8081",
            "http://[::1",
            "http://localhost:not-a-port",
            "localhost:8081",
            "ftp://localhost/resource",
        )

        for value in loopback_urls:
            with self.subTest(value=value):
                self.assertTrue(is_loopback_url(value))
        for value in non_loopback_urls:
            with self.subTest(value=value):
                self.assertFalse(is_loopback_url(value))


class MtprotoTransportTest(unittest.TestCase):
    def test_factory_selects_configured_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            mtproto = create_telegram_transport(_mtproto_config(tmp))
            bot_api = create_telegram_transport(
                TelegramConfig(upload_transport="bot_api")
            )
        self.assertIsInstance(mtproto, MtprotoTransport)
        self.assertIsInstance(bot_api, BotApiTransport)
        mtproto.close()

    def test_upload_logs_in_bot_preuploads_media_and_thumb_then_commits(self):
        events: list[object] = []
        clients: list[_FakeTelethonClient] = []

        def client_factory(session, api_id, api_hash, *, receive_updates):
            client = _FakeTelethonClient(
                session,
                api_id,
                api_hash,
                receive_updates=receive_updates,
                events=events,
            )
            clients.append(client)
            return client

        caller_thread = threading.get_ident()
        callback_threads: list[int] = []
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "2026-08-08_source.m4a"
            source.write_bytes(b"audio")
            thumbnail = Path(tmp) / "cover.jpg"
            thumbnail.write_bytes(b"jpeg")
            transport = MtprotoTransport(
                _mtproto_config(tmp),
                bindings=_fake_telethon_bindings(client_factory),
            )
            with transport:
                result = transport.upload(
                    source,
                    title="Sleep / Ear Cleaning",
                    url="https://example.com/watch/abc",
                    feed_name="@artist (channel)",
                    video_id="abc",
                    published_at="2026-08-08T00:00:00Z",
                    thumbnail_path=thumbnail,
                    performer="Artist",
                    duration_seconds=123.9,
                    before_commit=lambda: (
                        events.append("before_commit"),
                        callback_threads.append(threading.get_ident()),
                    ),
                )
                session_mode = Path(clients[0].session).stat().st_mode & 0o777

        self.assertEqual(result, 314)
        self.assertEqual(len(clients), 1)
        client = clients[0]
        self.assertEqual(client.api_id, 12345)
        self.assertEqual(client.api_hash, "test-api-hash")
        self.assertFalse(client.receive_updates)
        self.assertEqual(client.start_calls, 1)
        self.assertEqual(client.disconnect_calls, 1)
        self.assertEqual(session_mode, 0o600)
        self.assertEqual(callback_threads, [caller_thread])
        event_names = [item[0] if isinstance(item, tuple) else item for item in events]
        self.assertEqual(
            event_names,
            [
                "start",
                "get_me",
                "resolve",
                "upload",
                "upload",
                "before_commit",
                "send",
                "disconnect",
            ],
        )
        send_kwargs = client.send_kwargs
        self.assertIsNotNone(send_kwargs)
        assert send_kwargs is not None
        self.assertEqual(send_kwargs["entity"], "entity:@archive")
        self.assertEqual(send_kwargs["file"], "uploaded:2026-08-08_Sleep _ Ear Cleaning.m4a")
        self.assertEqual(send_kwargs["thumb"], "uploaded:thumbnail.jpg")
        self.assertIn("https://example.com/watch/abc", send_kwargs["caption"])
        self.assertIsNone(send_kwargs["parse_mode"])
        self.assertFalse(send_kwargs["force_document"])
        self.assertFalse(send_kwargs["supports_streaming"])
        attributes = send_kwargs["attributes"]
        self.assertEqual(attributes[0], {
            "kind": "filename",
            "file_name": "2026-08-08_Sleep _ Ear Cleaning.m4a",
        })
        self.assertEqual(attributes[1], {
            "kind": "audio",
            "duration": 123,
            "voice": False,
            "title": "Sleep / Ear Cleaning",
            "performer": "Artist",
        })

    def test_one_client_and_session_are_reused_across_uploads(self):
        events: list[object] = []
        clients: list[_FakeTelethonClient] = []

        def client_factory(session, api_id, api_hash, *, receive_updates):
            client = _FakeTelethonClient(
                session,
                api_id,
                api_hash,
                receive_updates=receive_updates,
                events=events,
            )
            clients.append(client)
            return client

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "audio.m4a"
            source.write_bytes(b"audio")
            transport = MtprotoTransport(
                _mtproto_config(tmp),
                bindings=_fake_telethon_bindings(client_factory),
            )
            try:
                for video_id in ("one", "two"):
                    transport.upload(
                        source,
                        title=video_id,
                        url=f"https://example.com/{video_id}",
                        feed_name="Artist",
                        video_id=video_id,
                    )
            finally:
                transport.close()

        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0].start_calls, 1)
        self.assertEqual(
            len([item for item in events if isinstance(item, tuple) and item[0] == "send"]),
            2,
        )

    def test_numeric_chat_id_is_resolved_as_integer(self):
        events: list[object] = []

        def client_factory(session, api_id, api_hash, *, receive_updates):
            return _FakeTelethonClient(
                session,
                api_id,
                api_hash,
                receive_updates=receive_updates,
                events=events,
            )

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "audio.m4a"
            source.write_bytes(b"audio")
            transport = MtprotoTransport(
                _mtproto_config(tmp, chat_id="-1001234567890"),
                bindings=_fake_telethon_bindings(client_factory),
            )
            try:
                transport.upload(
                    source,
                    title="Audio",
                    url="https://example.com/audio",
                    feed_name="Artist",
                    video_id="audio",
                )
            finally:
                transport.close()

        self.assertIn(("resolve", -1001234567890), events)

    def test_document_and_video_send_flags_and_attributes(self):
        cases = (
            ("document", False, True, False, ["filename"]),
            ("audio", True, True, False, ["filename"]),
            ("video", False, False, True, ["filename", "video"]),
        )
        for media_type, send_as_document, force_document, streaming, attribute_kinds in cases:
            with self.subTest(media_type=media_type, send_as_document=send_as_document):
                events: list[object] = []
                clients: list[_FakeTelethonClient] = []

                def client_factory(session, api_id, api_hash, *, receive_updates):
                    client = _FakeTelethonClient(
                        session,
                        api_id,
                        api_hash,
                        receive_updates=receive_updates,
                        events=events,
                    )
                    clients.append(client)
                    return client

                with tempfile.TemporaryDirectory() as tmp:
                    source = Path(tmp) / "media.mp4"
                    source.write_bytes(b"media")
                    transport = MtprotoTransport(
                        _mtproto_config(
                            tmp,
                            media_type=media_type,
                            send_as_document=send_as_document,
                        ),
                        bindings=_fake_telethon_bindings(client_factory),
                    )
                    try:
                        transport.upload(
                            source,
                            title="Media",
                            url="https://example.com/media",
                            feed_name="Artist",
                            video_id="media",
                            duration_seconds=42,
                            video_width=1920,
                            video_height=1080,
                        )
                    finally:
                        transport.close()

                send_kwargs = clients[0].send_kwargs
                assert send_kwargs is not None
                self.assertEqual(send_kwargs["force_document"], force_document)
                self.assertEqual(send_kwargs["supports_streaming"], streaming)
                self.assertEqual(
                    [item["kind"] for item in send_kwargs["attributes"]],
                    attribute_kinds,
                )
                if media_type == "video":
                    self.assertEqual(send_kwargs["attributes"][1]["w"], 1920)
                    self.assertEqual(send_kwargs["attributes"][1]["h"], 1080)

    def test_precommit_and_postcommit_errors_expose_retry_safety(self):
        cases = (
            ("upload", False, True, 0),
            ("send", True, False, 1),
        )
        for failure_stage, uncertain, fallback_safe, callback_count in cases:
            with self.subTest(failure_stage=failure_stage):
                events: list[object] = []
                clients: list[_FakeTelethonClient] = []

                def client_factory(session, api_id, api_hash, *, receive_updates):
                    client = _FakeTelethonClient(
                        session,
                        api_id,
                        api_hash,
                        receive_updates=receive_updates,
                        events=events,
                        upload_error=(
                            OSError("failed with test-api-hash")
                            if failure_stage == "upload"
                            else None
                        ),
                        send_error=(
                            OSError("failed with 123456:test-bot-token")
                            if failure_stage == "send"
                            else None
                        ),
                    )
                    clients.append(client)
                    return client

                callbacks: list[str] = []
                with tempfile.TemporaryDirectory() as tmp:
                    source = Path(tmp) / "audio.m4a"
                    source.write_bytes(b"audio")
                    transport = MtprotoTransport(
                        _mtproto_config(tmp),
                        bindings=_fake_telethon_bindings(client_factory),
                    )
                    try:
                        with self.assertRaises(TelegramUploadError) as raised:
                            transport.upload(
                                source,
                                title="Audio",
                                url="https://example.com/audio",
                                feed_name="Artist",
                                video_id="audio",
                                before_commit=lambda: callbacks.append("called"),
                            )
                    finally:
                        transport.close()

                error = raised.exception
                self.assertEqual(error.transport, "mtproto")
                self.assertEqual(error.code, "transport_error")
                self.assertEqual(error.uncertain, uncertain)
                self.assertEqual(error.fallback_safe, fallback_safe)
                self.assertEqual(len(callbacks), callback_count)
                self.assertIn("<redacted>", str(error))
                self.assertNotIn("test-api-hash", str(error))
                self.assertNotIn("123456:test-bot-token", str(error))

    def test_flood_wait_has_retry_after_and_is_not_cross_transport_safe(self):
        events: list[object] = []

        def client_factory(session, api_id, api_hash, *, receive_updates):
            return _FakeTelethonClient(
                session,
                api_id,
                api_hash,
                receive_updates=receive_updates,
                events=events,
                send_error=_FakeFloodWaitError(23),
            )

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "audio.m4a"
            source.write_bytes(b"audio")
            transport = MtprotoTransport(
                _mtproto_config(tmp),
                bindings=_fake_telethon_bindings(client_factory),
            )
            try:
                with self.assertRaises(TelegramUploadError) as raised:
                    transport.upload(
                        source,
                        title="Audio",
                        url="https://example.com/audio",
                        feed_name="Artist",
                        video_id="audio",
                    )
            finally:
                transport.close()

        error = raised.exception
        self.assertEqual(error.code, "flood_wait")
        self.assertEqual(error.retry_after, 23)
        self.assertFalse(error.uncertain)
        self.assertFalse(error.fallback_safe)

    def test_stale_session_identity_is_rejected_before_upload(self):
        events: list[object] = []
        clients: list[_FakeTelethonClient] = []

        def client_factory(session, api_id, api_hash, *, receive_updates):
            client = _FakeTelethonClient(
                session,
                api_id,
                api_hash,
                receive_updates=receive_updates,
                events=events,
                bot_id=999999,
            )
            clients.append(client)
            return client

        callbacks: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "audio.m4a"
            source.write_bytes(b"audio")
            transport = MtprotoTransport(
                _mtproto_config(tmp),
                bindings=_fake_telethon_bindings(client_factory),
            )
            try:
                with self.assertRaises(TelegramUploadError) as raised:
                    transport.upload(
                        source,
                        title="Audio",
                        url="https://example.com/audio",
                        feed_name="Artist",
                        video_id="audio",
                        before_commit=lambda: callbacks.append("called"),
                    )
            finally:
                transport.close()

        self.assertEqual(raised.exception.code, "session_identity_mismatch")
        self.assertFalse(raised.exception.uncertain)
        self.assertTrue(raised.exception.fallback_safe)
        self.assertEqual(callbacks, [])
        event_names = [item[0] if isinstance(item, tuple) else item for item in events]
        self.assertNotIn("upload", event_names)
        self.assertNotIn("send", event_names)
        self.assertEqual(clients[0].disconnect_calls, 1)

    def test_callback_failure_is_local_and_send_is_not_attempted(self):
        events: list[object] = []

        def client_factory(session, api_id, api_hash, *, receive_updates):
            return _FakeTelethonClient(
                session,
                api_id,
                api_hash,
                receive_updates=receive_updates,
                events=events,
            )

        class CommitFailure(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "audio.m4a"
            source.write_bytes(b"audio")
            transport = MtprotoTransport(
                _mtproto_config(tmp),
                bindings=_fake_telethon_bindings(client_factory),
            )
            try:
                with self.assertRaises(CommitFailure):
                    transport.upload(
                        source,
                        title="Audio",
                        url="https://example.com/audio",
                        feed_name="Artist",
                        video_id="audio",
                        before_commit=lambda: (_ for _ in ()).throw(CommitFailure("db failed")),
                    )
            finally:
                transport.close()

        self.assertNotIn("send", [item[0] for item in events if isinstance(item, tuple)])


class TelegramCaptionTest(unittest.TestCase):
    def test_caption_uses_title_blank_line_and_tag(self):
        uploader = TelegramUploader(TelegramConfig())
        caption = uploader._caption(
            title="Video Title",
            url="https://www.youtube.com/watch?v=x",
            feed_name="@nightmare (live)",
            video_id="x",
        )
        self.assertEqual(caption, "Video Title\n\nhttps://www.youtube.com/watch?v=x\n\n#nightmare")

    def test_feed_name_tag_sanitizes_config_name(self):
        self.assertEqual(_tag_from_feed_name("Nightmare ASMR (channel)"), "Nightmare_ASMR")

    def test_upload_filename_uses_date_and_title(self):
        filename = _upload_filename(
            file_path=Path("2026-05-12_old-title_A55ZVGZcfv8.tg39k.m4a"),
            title='ASMR / EarCleaning: "KU100", Triggers',
            video_id="A55ZVGZcfv8",
        )
        self.assertEqual(filename, "2026-05-12_ASMR _ EarCleaning_ _KU100__ Triggers.m4a")

    def test_upload_filename_uses_published_date_for_live_derivative(self):
        filename = _upload_filename(
            file_path=Path("twitch_316244257650.live-merged.tg64k.m4a"),
            title="Live ASMR",
            video_id="316244257650",
            published_at="2026-07-29T14:08:51Z",
        )
        self.assertEqual(filename, "2026-07-29_Live ASMR.m4a")

    def test_upload_filename_falls_back_to_path_for_invalid_published_date(self):
        filename = _upload_filename(
            file_path=Path("2026-05-12_old-title_A55ZVGZcfv8.m4a"),
            title="ASMR",
            video_id="A55ZVGZcfv8",
            published_at="not-a-date",
        )
        self.assertEqual(filename, "2026-05-12_ASMR.m4a")

    def test_upload_uses_safe_temp_path_with_display_filename(self):
        captured: dict[str, object] = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='{"ok": true, "result": {"message_id": 123}}',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "2026-05-12_original_A55ZVGZcfv8.m4a"
            source.write_bytes(b"audio")
            uploader = TelegramUploader(
                TelegramConfig(enabled=True, bot_token="token", chat_id="@channel", media_type="audio")
            )
            with (
                mock.patch("ytb_tg_backup.telegram.shutil.which", return_value="/usr/bin/curl"),
                mock.patch("ytb_tg_backup.telegram.subprocess.run", fake_run),
            ):
                message_id = uploader.upload(
                    source,
                    title='ASMR / EarCleaning: "KU100", Triggers',
                    url="https://www.youtube.com/watch?v=A55ZVGZcfv8",
                    feed_name="@macoto",
                    video_id="A55ZVGZcfv8",
                )

        self.assertEqual(message_id, 123)
        file_form = captured["cmd"][captured["cmd"].index("-F") + 1]
        upload_path = Path(file_form.split(";filename=", 1)[0].split("@", 1)[1])
        self.assertEqual(upload_path.name, "A55ZVGZcfv8.m4a")
        self.assertIn("filename=2026-05-12_ASMR _ EarCleaning_ _KU100__ Triggers.m4a", file_form)
        self.assertNotIn("token", " ".join(captured["cmd"]))
        self.assertNotIn("--noproxy", captured["cmd"])
        self.assertIn("/bottoken/sendAudio", captured["kwargs"]["input"])
        self.assertEqual(captured["kwargs"]["timeout"], 7200)

    def test_loopback_upload_disables_curl_proxy_without_putting_token_in_argv(self):
        captured: dict[str, object] = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["endpoint_config"] = kwargs["input"]
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='{"ok": true, "result": {"message_id": 123}}',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "video.m4a"
            source.write_bytes(b"audio")
            uploader = TelegramUploader(
                TelegramConfig(
                    enabled=True,
                    bot_token="secret-token",
                    chat_id="@channel",
                    upload_transport="bot_api",
                    bot_api=BotApiConfig(api_base="http://127.42.0.9:8081"),
                )
            )
            with (
                mock.patch("ytb_tg_backup.telegram.shutil.which", return_value="/usr/bin/curl"),
                mock.patch("ytb_tg_backup.telegram.subprocess.run", fake_run),
            ):
                uploader.upload(
                    source,
                    title="Video Title",
                    url="https://www.youtube.com/watch?v=x",
                    feed_name="@macoto",
                    video_id="x",
                )

        self.assertEqual(captured["cmd"][1:3], ["--noproxy", "*"])
        self.assertNotIn("secret-token", " ".join(captured["cmd"]))
        self.assertIn("/botsecret-token/sendAudio", captured["endpoint_config"])

    def test_upload_includes_thumbnail_when_available(self):
        captured: dict[str, list[str]] = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='{"ok": true, "result": {"message_id": 123}}',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "2026-05-12_original_A55ZVGZcfv8.m4a"
            source.write_bytes(b"audio")
            thumbnail = tmp_path / "A55ZVGZcfv8.tgthumb.jpg"
            thumbnail.write_bytes(b"jpeg")
            uploader = TelegramUploader(
                TelegramConfig(enabled=True, bot_token="token", chat_id="@channel", media_type="audio")
            )
            with (
                mock.patch("ytb_tg_backup.telegram.shutil.which", return_value="/usr/bin/curl"),
                mock.patch("ytb_tg_backup.telegram.subprocess.run", fake_run),
            ):
                uploader.upload(
                    source,
                    title="Video Title",
                    url="https://www.youtube.com/watch?v=A55ZVGZcfv8",
                    feed_name="@macoto",
                    video_id="A55ZVGZcfv8",
                    thumbnail_path=thumbnail,
                )

        thumbnail_forms = [item for item in captured["cmd"] if item.startswith("thumbnail=@")]
        self.assertEqual(len(thumbnail_forms), 1)
        self.assertTrue(thumbnail_forms[0].endswith(";filename=thumbnail.jpg"))

    def test_upload_uses_symlink_instead_of_copy_when_hardlink_fails(self):
        captured: dict[str, object] = {}

        def fake_run(cmd, **kwargs):
            file_form = cmd[cmd.index("-F") + 1]
            upload_path = Path(file_form.split(";filename=", 1)[0].split("@", 1)[1])
            captured["is_symlink"] = upload_path.is_symlink()
            captured["resolved_path"] = upload_path.resolve()
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='{"ok": true, "result": {"message_id": 123}}',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "2026-05-12_original_A55ZVGZcfv8.m4a"
            source.write_bytes(b"audio")
            uploader = TelegramUploader(
                TelegramConfig(enabled=True, bot_token="token", chat_id="@channel", media_type="audio")
            )
            with (
                mock.patch("ytb_tg_backup.telegram.shutil.which", return_value="/usr/bin/curl"),
                mock.patch.object(Path, "hardlink_to", side_effect=OSError("cross-device link")),
                mock.patch("ytb_tg_backup.telegram.shutil.copy2") as copy2,
                mock.patch("ytb_tg_backup.telegram.subprocess.run", fake_run),
            ):
                uploader.upload(
                    source,
                    title="Video Title",
                    url="https://www.youtube.com/watch?v=A55ZVGZcfv8",
                    feed_name="@macoto",
                    video_id="A55ZVGZcfv8",
                )

        self.assertTrue(captured["is_symlink"])
        self.assertEqual(captured["resolved_path"], source.resolve())
        copy2.assert_not_called()

    def test_upload_falls_back_to_source_path_when_linking_is_unavailable(self):
        captured: dict[str, Path] = {}

        def fake_run(cmd, **kwargs):
            file_form = cmd[cmd.index("-F") + 1]
            captured["upload_path"] = Path(file_form.split(";filename=", 1)[0].split("@", 1)[1])
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='{"ok": true, "result": {"message_id": 123}}',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "2026-05-12_original_A55ZVGZcfv8.m4a"
            source.write_bytes(b"audio")
            uploader = TelegramUploader(
                TelegramConfig(enabled=True, bot_token="token", chat_id="@channel", media_type="audio")
            )
            with (
                mock.patch("ytb_tg_backup.telegram.shutil.which", return_value="/usr/bin/curl"),
                mock.patch.object(Path, "hardlink_to", side_effect=OSError("cross-device link")),
                mock.patch.object(Path, "symlink_to", side_effect=OSError("symlinks unavailable")),
                mock.patch("ytb_tg_backup.telegram.subprocess.run", fake_run),
            ):
                uploader.upload(
                    source,
                    title="Video Title",
                    url="https://www.youtube.com/watch?v=A55ZVGZcfv8",
                    feed_name="@macoto",
                    video_id="A55ZVGZcfv8",
                )

        self.assertEqual(captured["upload_path"], source)

    def test_upload_uses_configured_timeout(self):
        captured: dict[str, object] = {}

        def fake_run(cmd, **kwargs):
            captured["timeout"] = kwargs["timeout"]
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='{"ok": true, "result": {"message_id": 123}}',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "video.m4a"
            source.write_bytes(b"audio")
            config = TelegramConfig(enabled=True, bot_token="token", chat_id="@channel")
            object.__setattr__(config, "upload_timeout_seconds", 37)
            uploader = TelegramUploader(config)
            with (
                mock.patch("ytb_tg_backup.telegram.shutil.which", return_value="/usr/bin/curl"),
                mock.patch("ytb_tg_backup.telegram.subprocess.run", fake_run),
            ):
                uploader.upload(
                    source,
                    title="Video Title",
                    url="https://www.youtube.com/watch?v=x",
                    feed_name="@macoto",
                    video_id="x",
                )

        self.assertEqual(captured["timeout"], 37)

    def test_bot_api_calls_before_commit_once_immediately_before_curl(self):
        events: list[str] = []

        def fake_run(cmd, **kwargs):
            events.append("curl")
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='{"ok": true, "result": {"message_id": 123}}',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "video.m4a"
            source.write_bytes(b"audio")
            uploader = TelegramUploader(
                TelegramConfig(
                    enabled=True,
                    bot_token="token",
                    chat_id="@channel",
                    upload_transport="bot_api",
                )
            )
            with (
                mock.patch("ytb_tg_backup.telegram.shutil.which", return_value="/usr/bin/curl"),
                mock.patch("ytb_tg_backup.telegram.subprocess.run", fake_run),
            ):
                uploader.upload(
                    source,
                    title="Video Title",
                    url="https://www.youtube.com/watch?v=x",
                    feed_name="@macoto",
                    video_id="x",
                    before_commit=lambda: events.append("before_commit"),
                )

        self.assertEqual(events, ["before_commit", "curl"])

    def test_upload_sends_audio_parts_as_one_media_group(self):
        captured: dict[str, object] = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["endpoint_config"] = kwargs["input"]
            form_strings = [
                cmd[index + 1]
                for index, value in enumerate(cmd)
                if value == "--form-string"
            ]
            media_value = next(value for value in form_strings if value.startswith("media="))
            captured["media"] = json.loads(media_value.removeprefix("media="))
            captured["files"] = [
                cmd[index + 1]
                for index, value in enumerate(cmd)
                if value == "-F" and cmd[index + 1].startswith("audio")
            ]
            captured["thumbnails"] = [
                cmd[index + 1]
                for index, value in enumerate(cmd)
                if value == "-F" and cmd[index + 1].startswith("thumbnail")
            ]
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "result": [
                            {"message_id": 101},
                            {"message_id": 102},
                            {"message_id": 103},
                        ],
                    }
                )
                + "\n200",
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tmp:
            parts = []
            for index in range(1, 4):
                part = Path(tmp) / f"part-{index}.m4a"
                part.write_bytes(b"audio")
                parts.append(part)
            thumbnail = Path(tmp) / "cover.jpg"
            thumbnail.write_bytes(b"jpeg")
            uploader = TelegramUploader(
                TelegramConfig(
                    enabled=True,
                    bot_token="token",
                    chat_id="@channel",
                    upload_transport="bot_api",
                    bot_api=BotApiConfig(api_base="http://[::1]:8081"),
                )
            )
            with (
                mock.patch("ytb_tg_backup.telegram.shutil.which", return_value="/usr/bin/curl"),
                mock.patch("ytb_tg_backup.telegram.subprocess.run", fake_run),
            ):
                result = uploader.upload(
                    parts,
                    title="Long ASMR " + "sleep sounds " * 10,
                    url="https://www.youtube.com/watch?v=long",
                    feed_name="@artist",
                    video_id="long",
                    published_at="2026-08-08T00:00:00Z",
                    thumbnail_path=thumbnail,
                )

        self.assertIn("sendMediaGroup", captured["endpoint_config"])
        self.assertEqual(captured["cmd"][1:3], ["--noproxy", "*"])
        self.assertEqual(result, [101, 102, 103])
        self.assertEqual(len(captured["files"]), 3)
        media = captured["media"]
        self.assertEqual([item["media"] for item in media], [
            "attach://audio1",
            "attach://audio2",
            "attach://audio3",
        ])
        self.assertEqual(
            [item["title"].removeprefix(item["title"].split(" (Part", 1)[0]) for item in media],
            [" (Part 1/3)", " (Part 2/3)", " (Part 3/3)"],
        )
        self.assertEqual(len({item["title"] for item in media}), 3)
        self.assertTrue(all(item["title"].startswith("Long ASMR") for item in media))
        self.assertTrue(all(len(item["title"]) <= 64 for item in media))
        self.assertEqual(
            [item["thumbnail"] for item in media],
            [
                "attach://thumbnail1",
                "attach://thumbnail2",
                "attach://thumbnail3",
            ],
        )
        self.assertIn("Long ASMR", media[0]["caption"])
        self.assertNotIn("caption", media[1])
        self.assertNotIn("caption", media[2])
        self.assertIn("part-01-of-03.m4a", captured["files"][0])
        self.assertEqual(len(captured["thumbnails"]), 3)
        self.assertEqual(
            [value.split("=", 1)[0] for value in captured["thumbnails"]],
            ["thumbnail1", "thumbnail2", "thumbnail3"],
        )
        self.assertEqual(
            [value.rsplit(";filename=", 1)[1] for value in captured["thumbnails"]],
            [
                "thumbnail-part-01.jpg",
                "thumbnail-part-02.jpg",
                "thumbnail-part-03.jpg",
            ],
        )

    def test_curl_failure_does_not_expose_bot_token(self):
        bot_token = "123456:super-secret-token"

        def fake_run(cmd, **kwargs):
            raise subprocess.CalledProcessError(
                22,
                cmd,
                output=f"response mentioned {bot_token}",
                stderr=f"request failed for {bot_token}",
            )

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "video.m4a"
            source.write_bytes(b"audio")
            uploader = TelegramUploader(
                TelegramConfig(enabled=True, bot_token=bot_token, chat_id="@channel")
            )
            with (
                mock.patch("ytb_tg_backup.telegram.shutil.which", return_value="/usr/bin/curl"),
                mock.patch("ytb_tg_backup.telegram.subprocess.run", fake_run),
                self.assertRaises(TelegramUploadError) as raised,
            ):
                uploader.upload(
                    source,
                    title="Video Title",
                    url="https://www.youtube.com/watch?v=x",
                    feed_name="@macoto",
                    video_id="x",
                )

        self.assertNotIn(bot_token, str(raised.exception))
        self.assertIn("<redacted>", str(raised.exception))
        self.assertFalse(raised.exception.uncertain)

    def test_transport_failure_and_malformed_success_are_uncertain(self):
        failures = (
            subprocess.CalledProcessError(56, ["curl"], stderr="connection reset after upload"),
            subprocess.CompletedProcess(
                ["curl"],
                0,
                stdout='{"ok": true, "result": {}}',
                stderr="",
            ),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / "video.m4a"
                source.write_bytes(b"audio")
                uploader = TelegramUploader(
                    TelegramConfig(enabled=True, bot_token="token", chat_id="@channel")
                )
                patch_kwargs = (
                    {"side_effect": failure}
                    if isinstance(failure, BaseException)
                    else {"return_value": failure}
                )
                with (
                    mock.patch("ytb_tg_backup.telegram.shutil.which", return_value="/usr/bin/curl"),
                    mock.patch("ytb_tg_backup.telegram.subprocess.run", **patch_kwargs),
                    self.assertRaises(TelegramUploadError) as raised,
                ):
                    uploader.upload(
                        source,
                        title="Video Title",
                        url="https://www.youtube.com/watch?v=x",
                        feed_name="@macoto",
                        video_id="x",
                    )
                self.assertTrue(raised.exception.uncertain)

    def test_http_5xx_is_uncertain_but_http_429_is_retryable(self):
        for status, expected_uncertain in ((500, True), (429, False)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / "video.m4a"
                source.write_bytes(b"audio")
                uploader = TelegramUploader(
                    TelegramConfig(enabled=True, bot_token="token", chat_id="@channel")
                )
                error = subprocess.CalledProcessError(
                    22,
                    ["curl"],
                    output=f'{{"ok": false, "error_code": {status}}}\n{status}',
                    stderr="",
                )
                with (
                    mock.patch("ytb_tg_backup.telegram.shutil.which", return_value="/usr/bin/curl"),
                    mock.patch("ytb_tg_backup.telegram.subprocess.run", side_effect=error),
                    self.assertRaises(TelegramUploadError) as raised,
                ):
                    uploader.upload(
                        source,
                        title="Video Title",
                        url="https://www.youtube.com/watch?v=x",
                        feed_name="@macoto",
                        video_id="x",
                    )
                self.assertEqual(raised.exception.uncertain, expected_uncertain)

    def test_bot_api_error_exposes_transport_code_and_retry_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "video.m4a"
            source.write_bytes(b"audio")
            uploader = TelegramUploader(
                TelegramConfig(
                    enabled=True,
                    bot_token="token",
                    chat_id="@channel",
                    upload_transport="bot_api",
                )
            )
            error = subprocess.CalledProcessError(
                22,
                ["curl"],
                output=(
                    '{"ok": false, "error_code": 429, '
                    '"parameters": {"retry_after": 17}}\n429'
                ),
                stderr="",
            )
            with (
                mock.patch("ytb_tg_backup.telegram.shutil.which", return_value="/usr/bin/curl"),
                mock.patch("ytb_tg_backup.telegram.subprocess.run", side_effect=error),
                self.assertRaises(TelegramUploadError) as raised,
            ):
                uploader.upload(
                    source,
                    title="Video Title",
                    url="https://www.youtube.com/watch?v=x",
                    feed_name="@macoto",
                    video_id="x",
                )

        self.assertEqual(raised.exception.transport, "bot_api")
        self.assertEqual(raised.exception.code, "http_429")
        self.assertEqual(raised.exception.retry_after, 17)
        self.assertFalse(raised.exception.uncertain)
        self.assertFalse(raised.exception.fallback_safe)

    def test_upload_sanitizes_external_id_before_building_temp_path(self):
        captured: dict[str, Path] = {}

        def fake_run(cmd, **kwargs):
            file_form = cmd[cmd.index("-F") + 1]
            captured["path"] = Path(file_form.split(";filename=", 1)[0].split("@", 1)[1])
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='{"ok": true, "result": {"message_id": 123}}\n200',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "video.m4a"
            source.write_bytes(b"audio")
            uploader = TelegramUploader(
                TelegramConfig(enabled=True, bot_token="token", chat_id="@channel")
            )
            with (
                mock.patch("ytb_tg_backup.telegram.shutil.which", return_value="/usr/bin/curl"),
                mock.patch("ytb_tg_backup.telegram.subprocess.run", fake_run),
            ):
                uploader.upload(
                    source,
                    title="Video Title",
                    url="https://example.com/media",
                    feed_name="RSS",
                    video_id="../escaped*?[id]",
                )

        self.assertNotIn("..", captured["path"].parts)
        self.assertNotRegex(captured["path"].name, r"[/*?\[\]]")


if __name__ == "__main__":
    unittest.main()
