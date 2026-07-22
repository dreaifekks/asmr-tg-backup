from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from ytb_tg_backup.config import TelegramConfig
from ytb_tg_backup.telegram import (
    TelegramUploader,
    TelegramUploadError,
    _tag_from_feed_name,
    _upload_filename,
)


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
        self.assertIn("/bottoken/sendAudio", captured["kwargs"]["input"])
        self.assertEqual(captured["kwargs"]["timeout"], 7200)

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
