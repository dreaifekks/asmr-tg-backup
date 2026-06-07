from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .config import TelegramConfig


class TelegramUploadError(RuntimeError):
    pass


class TelegramUploader:
    def __init__(self, config: TelegramConfig):
        self.config = config

    def validate(self) -> None:
        if not self.config.enabled:
            return
        if not self.config.bot_token:
            raise TelegramUploadError("telegram.bot_token is required when telegram.enabled=true")
        if not self.config.chat_id:
            raise TelegramUploadError("telegram.chat_id is required when telegram.enabled=true")
        if shutil.which("curl") is None:
            raise TelegramUploadError("curl is required for Telegram uploads")

    def upload(
        self,
        file_path: Path,
        *,
        title: str,
        url: str,
        feed_name: str,
        video_id: str,
        thumbnail_path: Path | None = None,
    ) -> int:
        self.validate()
        file_size = file_path.stat().st_size
        if file_size > self.config.max_upload_bytes:
            raise TelegramUploadError(
                f"file size {file_size} exceeds telegram.max_upload_bytes={self.config.max_upload_bytes}"
            )

        method, file_field = self._upload_target()
        endpoint = f"{self.config.api_base.rstrip('/')}/bot{self.config.bot_token}/{method}"
        caption = self._caption(title=title, url=url, feed_name=feed_name, video_id=video_id)
        upload_filename = _upload_filename(file_path=file_path, title=title, video_id=video_id)

        with tempfile.TemporaryDirectory(prefix="ytb-tg-upload-") as tmp:
            safe_path = Path(tmp) / f"{video_id}{file_path.suffix.lower()}"
            try:
                safe_path.hardlink_to(file_path)
            except OSError:
                shutil.copy2(file_path, safe_path)
            safe_thumbnail_path = None
            if thumbnail_path is not None and thumbnail_path.exists() and method in {"sendAudio", "sendDocument", "sendVideo"}:
                safe_thumbnail_path = Path(tmp) / f"{video_id}.thumb.jpg"
                try:
                    safe_thumbnail_path.hardlink_to(thumbnail_path)
                except OSError:
                    shutil.copy2(thumbnail_path, safe_thumbnail_path)

            cmd = [
                "curl",
                "--fail-with-body",
                "--silent",
                "--show-error",
                "--request",
                "POST",
                endpoint,
                "--form-string",
                f"chat_id={self.config.chat_id}",
                "-F",
                f"{file_field}=@{safe_path};filename={upload_filename}",
            ]
            if safe_thumbnail_path is not None:
                cmd.extend(["-F", f"thumbnail=@{safe_thumbnail_path};filename=thumbnail.jpg"])
            if caption:
                cmd.extend(["--form-string", f"caption={caption}"])
            if method == "sendVideo":
                cmd.extend(["--form-string", "supports_streaming=true"])

            completed = subprocess.run(cmd, check=True, text=True, capture_output=True)
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise TelegramUploadError(f"Telegram returned non-JSON response: {completed.stdout[:200]}") from exc
        if not payload.get("ok"):
            raise TelegramUploadError(f"Telegram upload failed: {payload}")
        return int(payload["result"]["message_id"])

    def _upload_target(self) -> tuple[str, str]:
        if self.config.send_as_document:
            return "sendDocument", "document"
        if self.config.media_type == "audio":
            return "sendAudio", "audio"
        if self.config.media_type == "document":
            return "sendDocument", "document"
        return "sendVideo", "video"

    def _caption(self, *, title: str, url: str, feed_name: str, video_id: str) -> str:
        tag = _tag_from_feed_name(feed_name)
        body = f"{title}\n\n{url}"
        caption = f"{body}\n\n#{tag}" if tag else body
        return caption[:1024]


def _tag_from_feed_name(feed_name: str) -> str:
    base = feed_name.split(" (", 1)[0].strip()
    if base.startswith("@"):
        base = base[1:]
    tag = "".join(char if char.isalnum() or char == "_" else "_" for char in base)
    tag = "_".join(part for part in tag.split("_") if part)
    return tag[:80]


def _upload_filename(*, file_path: Path, title: str, video_id: str) -> str:
    date = _date_from_path(file_path) or "unknown-date"
    clean_title = _clean_filename(title) or video_id
    suffix = file_path.suffix.lower() or ".m4a"
    return f"{date}_{clean_title}{suffix}"


def _date_from_path(file_path: Path) -> str | None:
    match = re.match(r"(\d{4}-\d{2}-\d{2})_", file_path.name)
    return match.group(1) if match else None


def _clean_filename(value: str, limit: int = 120) -> str:
    cleaned = "".join("_" if char in {'/', '\\', ':', '*', '?', '"', '<', '>', '|', ';', ','} else char for char in value)
    cleaned = "".join(" " if char.isspace() else char for char in cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:limit].rstrip(" .")
