from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .config import TelegramConfig


class TelegramUploadError(RuntimeError):
    def __init__(self, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain


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
        published_at: str | None = None,
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
        upload_filename = _upload_filename(
            file_path=file_path,
            title=title,
            video_id=video_id,
            published_at=published_at,
        )
        upload_timeout_seconds = int(getattr(self.config, "upload_timeout_seconds", 7200))

        with tempfile.TemporaryDirectory(prefix="ytb-tg-upload-") as tmp:
            safe_id = _safe_path_component(video_id)
            safe_path = Path(tmp) / f"{safe_id}{file_path.suffix.lower()}"
            upload_path = _safe_upload_path(file_path, safe_path)
            safe_thumbnail_path = None
            if thumbnail_path is not None and thumbnail_path.exists() and method in {"sendAudio", "sendDocument", "sendVideo"}:
                safe_thumbnail_path = _safe_upload_path(
                    thumbnail_path,
                    Path(tmp) / f"{safe_id}.thumb.jpg",
                )

            cmd = [
                "curl",
                "--config",
                "-",
                "--fail-with-body",
                "--silent",
                "--show-error",
                "--request",
                "POST",
                "--write-out",
                "\n%{http_code}",
                "--form-string",
                f"chat_id={self.config.chat_id}",
                "-F",
                f"{file_field}=@{upload_path};filename={upload_filename}",
            ]
            if safe_thumbnail_path is not None:
                cmd.extend(["-F", f"thumbnail=@{safe_thumbnail_path};filename=thumbnail.jpg"])
            if caption:
                cmd.extend(["--form-string", f"caption={caption}"])
            if method == "sendVideo":
                cmd.extend(["--form-string", "supports_streaming=true"])

            try:
                completed = subprocess.run(
                    cmd,
                    check=True,
                    text=True,
                    capture_output=True,
                    input=_curl_stdin_config(endpoint),
                    timeout=upload_timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                raise TelegramUploadError(
                    f"Telegram upload timed out after {upload_timeout_seconds} seconds",
                    uncertain=True,
                ) from None
            except subprocess.CalledProcessError as exc:
                response_body, http_status = _split_curl_output(exc.stdout or exc.output or "")
                detail = _redact_bot_token(exc.stderr or response_body, self.config.bot_token)
                suffix = f": {detail[:500]}" if detail else ""
                ambiguous_http = http_status == 408 or (http_status is not None and http_status >= 500)
                raise TelegramUploadError(
                    f"Telegram upload command failed with exit code {exc.returncode}{suffix}",
                    uncertain=ambiguous_http or exc.returncode not in {3, 5, 6, 7, 22, 60, 77},
                ) from None
        response_body, _ = _split_curl_output(completed.stdout)
        try:
            payload = json.loads(response_body)
        except json.JSONDecodeError as exc:
            response = _redact_bot_token(response_body[:200], self.config.bot_token)
            raise TelegramUploadError(
                f"Telegram returned non-JSON response: {response}",
                uncertain=True,
            ) from exc
        if not isinstance(payload, dict):
            raise TelegramUploadError("Telegram returned a non-object response", uncertain=True)
        if not payload.get("ok"):
            error_payload = _redact_bot_token(str(payload), self.config.bot_token)
            raise TelegramUploadError(f"Telegram upload failed: {error_payload}")
        try:
            return int(payload["result"]["message_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TelegramUploadError(
                "Telegram reported success without a valid message_id",
                uncertain=True,
            ) from exc

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
        try:
            caption = self.config.caption_template.format(
                title=title,
                url=url,
                feed_name=feed_name,
                video_id=video_id,
                tag=tag,
            )
        except (KeyError, ValueError) as exc:
            raise TelegramUploadError(f"invalid telegram.caption_template: {exc}") from None
        return caption[:1024]


def _tag_from_feed_name(feed_name: str) -> str:
    base = feed_name.split(" (", 1)[0].strip()
    if base.startswith("@"):
        base = base[1:]
    tag = "".join(char if char.isalnum() or char == "_" else "_" for char in base)
    tag = "_".join(part for part in tag.split("_") if part)
    return tag[:80]


def _upload_filename(
    *,
    file_path: Path,
    title: str,
    video_id: str,
    published_at: str | None = None,
) -> str:
    date = (
        _date_from_published_at(published_at)
        or _date_from_path(file_path)
        or "unknown-date"
    )
    clean_title = _clean_filename(title) or _clean_filename(video_id) or "media"
    suffix = file_path.suffix.lower() or ".m4a"
    return f"{date}_{clean_title}{suffix}"


def _date_from_published_at(published_at: str | None) -> str | None:
    if not published_at:
        return None
    match = re.match(r"(\d{4}-\d{2}-\d{2})(?:T|$)", published_at.strip())
    return match.group(1) if match else None


def _date_from_path(file_path: Path) -> str | None:
    match = re.match(r"(\d{4}-\d{2}-\d{2})_", file_path.name)
    return match.group(1) if match else None


def _clean_filename(value: str, limit: int = 120) -> str:
    cleaned = "".join("_" if char in {'/', '\\', ':', '*', '?', '"', '<', '>', '|', ';', ','} else char for char in value)
    cleaned = "".join(" " if char.isspace() else char for char in cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:limit].rstrip(" .")


def _safe_upload_path(source: Path, safe_path: Path) -> Path:
    try:
        safe_path.hardlink_to(source)
    except OSError:
        try:
            safe_path.symlink_to(source.resolve())
        except OSError:
            return source
    return safe_path


def _curl_stdin_config(endpoint: str) -> str:
    escaped_endpoint = endpoint.replace("\\", "\\\\").replace('"', '\\"')
    return f'url = "{escaped_endpoint}"\n'


def _redact_bot_token(value: str, bot_token: str) -> str:
    if not bot_token:
        return value
    return value.replace(bot_token, "<redacted>")


def _safe_path_component(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", value) and value not in {".", ".."}:
        return value
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _split_curl_output(value: str) -> tuple[str, int | None]:
    body, separator, status_text = value.rpartition("\n")
    if separator and len(status_text) == 3 and status_text.isdigit():
        return body, int(status_text)
    return value, None
