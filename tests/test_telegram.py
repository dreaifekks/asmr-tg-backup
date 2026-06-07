from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from ytb_tg_backup.config import TelegramConfig
from ytb_tg_backup.telegram import TelegramUploader, _tag_from_feed_name, _upload_filename


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


if __name__ == "__main__":
    unittest.main()
