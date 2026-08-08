from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any

from .config import TelegramConfig
from .network import is_loopback_url
from .telegram_types import (
    BeforeCommit,
    TelegramTransport,
    TelegramUploadError,
    TelegramUploadResult,
)


class TelegramUploader:
    transport_name = "bot_api"

    def __init__(self, config: TelegramConfig):
        self.config = config

    def validate(self) -> None:
        if not self.config.enabled:
            return
        if not self.config.bot_token:
            raise self._error(
                "telegram.bot_token is required when telegram.enabled=true",
                code="configuration",
            )
        if not self.config.chat_id:
            raise self._error(
                "telegram.chat_id is required when telegram.enabled=true",
                code="configuration",
            )
        if shutil.which("curl") is None:
            raise self._error(
                "curl is required for Telegram uploads",
                code="dependency_missing",
            )

    def upload(
        self,
        file_path: Path | list[Path],
        *,
        title: str,
        url: str,
        feed_name: str,
        video_id: str,
        published_at: str | None = None,
        thumbnail_path: Path | None = None,
        performer: str | None = None,
        duration_seconds: float | int | None = None,
        video_width: int | None = None,
        video_height: int | None = None,
        before_commit: BeforeCommit | None = None,
    ) -> TelegramUploadResult:
        self.validate()
        file_paths = [file_path] if isinstance(file_path, Path) else list(file_path)
        if not file_paths:
            raise self._error(
                "at least one file is required for Telegram upload",
                code="invalid_request",
            )
        max_upload_bytes = int(self._bot_api_value("max_upload_bytes", 49_000_000))
        for item in file_paths:
            file_size = item.stat().st_size
            if file_size > max_upload_bytes:
                raise self._error(
                    f"file size {file_size} exceeds telegram.max_upload_bytes={max_upload_bytes}",
                    code="upload_too_large",
                )
        if len(file_paths) > 1:
            return self._upload_media_group(
                file_paths,
                title=title,
                url=url,
                feed_name=feed_name,
                video_id=video_id,
                published_at=published_at,
                thumbnail_path=thumbnail_path,
                before_commit=before_commit,
            )
        file_path = file_paths[0]

        method, file_field = self._upload_target()
        api_base = str(self._bot_api_value("api_base", "https://api.telegram.org"))
        endpoint = f"{api_base.rstrip('/')}/bot{self.config.bot_token}/{method}"
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

            payload = self._post_upload(
                cmd,
                endpoint=endpoint,
                upload_timeout_seconds=upload_timeout_seconds,
                before_commit=before_commit,
            )
        try:
            return int(payload["result"]["message_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise self._error(
                "Telegram reported success without a valid message_id",
                code="invalid_response",
                uncertain=True,
                fallback_safe=False,
            ) from exc

    def _upload_media_group(
        self,
        file_paths: list[Path],
        *,
        title: str,
        url: str,
        feed_name: str,
        video_id: str,
        published_at: str | None,
        thumbnail_path: Path | None,
        before_commit: BeforeCommit | None = None,
    ) -> list[int]:
        max_upload_parts = int(self._bot_api_value("max_upload_parts", 10))
        if len(file_paths) > max_upload_parts or len(file_paths) > 10:
            raise self._error(
                f"audio requires {len(file_paths)} parts but Telegram allows at most "
                f"{min(max_upload_parts, 10)}",
                code="too_many_parts",
            )
        if self.config.send_as_document or self.config.media_type != "audio":
            raise self._error(
                "multi-part uploads are supported only for audio media groups",
                code="multiple_files_unsupported",
            )

        api_base = str(self._bot_api_value("api_base", "https://api.telegram.org"))
        endpoint = f"{api_base.rstrip('/')}/bot{self.config.bot_token}/sendMediaGroup"
        caption = self._caption(title=title, url=url, feed_name=feed_name, video_id=video_id)
        upload_timeout_seconds = int(getattr(self.config, "upload_timeout_seconds", 7200))
        total = len(file_paths)

        with tempfile.TemporaryDirectory(prefix="ytb-tg-upload-") as tmp:
            safe_id = _safe_path_component(video_id)
            media: list[dict[str, str]] = []
            attachments: list[tuple[str, Path, str]] = []
            for index, source in enumerate(file_paths, start=1):
                field = f"audio{index}"
                safe_path = _safe_upload_path(
                    source,
                    Path(tmp) / f"{safe_id}.part{index:02d}{source.suffix.lower()}",
                )
                filename = _part_upload_filename(
                    file_path=source,
                    title=title,
                    video_id=video_id,
                    part_no=index,
                    part_count=total,
                    published_at=published_at,
                )
                item = {
                    "type": "audio",
                    "media": f"attach://{field}",
                    "title": _part_audio_title(
                        title,
                        part_no=index,
                        part_count=total,
                    ),
                }
                if index == 1 and caption:
                    item["caption"] = caption
                media.append(item)
                attachments.append((field, safe_path, filename))

            if thumbnail_path is not None and thumbnail_path.exists():
                for index, item in enumerate(media, start=1):
                    thumbnail_field = f"thumbnail{index}"
                    safe_thumbnail_path = _safe_upload_path(
                        thumbnail_path,
                        Path(tmp) / f"{safe_id}.part{index:02d}.thumb.jpg",
                    )
                    item["thumbnail"] = f"attach://{thumbnail_field}"
                    attachments.append(
                        (
                            thumbnail_field,
                            safe_thumbnail_path,
                            f"thumbnail-part-{index:02d}.jpg",
                        )
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
                "--form-string",
                f"media={json.dumps(media, ensure_ascii=False, separators=(',', ':'))}",
            ]
            for field, safe_path, filename in attachments:
                cmd.extend(["-F", f"{field}=@{safe_path};filename={filename}"])

            payload = self._post_upload(
                cmd,
                endpoint=endpoint,
                upload_timeout_seconds=upload_timeout_seconds,
                before_commit=before_commit,
            )
        try:
            result = payload["result"]
            message_ids = [int(item["message_id"]) for item in result]
        except (KeyError, TypeError, ValueError) as exc:
            raise self._error(
                "Telegram reported media-group success without valid message_ids",
                code="invalid_response",
                uncertain=True,
                fallback_safe=False,
            ) from exc
        if len(message_ids) != total:
            raise self._error(
                "Telegram returned an incomplete media-group result",
                code="invalid_response",
                uncertain=True,
                fallback_safe=False,
            )
        return message_ids

    def _post_upload(
        self,
        cmd: list[str],
        *,
        endpoint: str,
        upload_timeout_seconds: int,
        before_commit: BeforeCommit | None = None,
    ) -> dict[str, object]:
        api_base = str(self._bot_api_value("api_base", "https://api.telegram.org"))
        if is_loopback_url(api_base):
            cmd = [cmd[0], "--noproxy", "*", *cmd[1:]]
        if before_commit is not None:
            before_commit()
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
            raise self._error(
                f"Telegram upload timed out after {upload_timeout_seconds} seconds",
                code="timeout",
                uncertain=True,
                fallback_safe=False,
            ) from None
        except subprocess.CalledProcessError as exc:
            response_body, http_status = _split_curl_output(exc.stdout or exc.output or "")
            detail = _redact_bot_token(exc.stderr or response_body, self.config.bot_token)
            suffix = f": {detail[:500]}" if detail else ""
            ambiguous_http = http_status == 408 or (http_status is not None and http_status >= 500)
            retry_after = _bot_api_retry_after(response_body)
            raise self._error(
                f"Telegram upload command failed with exit code {exc.returncode}{suffix}",
                code=f"http_{http_status}" if http_status is not None else "transport_error",
                uncertain=ambiguous_http or exc.returncode not in {3, 5, 6, 7, 22, 60, 77},
                fallback_safe=False,
                retry_after=retry_after,
            ) from None
        response_body, _ = _split_curl_output(completed.stdout)
        try:
            payload = json.loads(response_body)
        except json.JSONDecodeError as exc:
            response = _redact_bot_token(response_body[:200], self.config.bot_token)
            raise self._error(
                f"Telegram returned non-JSON response: {response}",
                code="invalid_response",
                uncertain=True,
                fallback_safe=False,
            ) from exc
        if not isinstance(payload, dict):
            raise self._error(
                "Telegram returned a non-object response",
                code="invalid_response",
                uncertain=True,
                fallback_safe=False,
            )
        if not payload.get("ok"):
            error_payload = _redact_bot_token(str(payload), self.config.bot_token)
            error_code = payload.get("error_code")
            raise self._error(
                f"Telegram upload failed: {error_payload}",
                code=f"http_{error_code}" if isinstance(error_code, int) else "api_error",
                fallback_safe=False,
                retry_after=_bot_api_retry_after(payload),
            )
        return payload

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
            raise self._error(
                f"invalid telegram.caption_template: {exc}",
                code="invalid_caption",
            ) from None
        return caption[:1024]

    def _bot_api_value(self, name: str, default: Any) -> Any:
        section = getattr(self.config, "bot_api", None)
        if section is not None and hasattr(section, name):
            return getattr(section, name)
        return getattr(self.config, name, default)

    def _error(
        self,
        message: str,
        *,
        code: str,
        uncertain: bool = False,
        fallback_safe: bool | None = None,
        retry_after: int | None = None,
    ) -> TelegramUploadError:
        return TelegramUploadError(
            message,
            code=code,
            uncertain=uncertain,
            fallback_safe=fallback_safe,
            retry_after=retry_after,
            transport=self.transport_name,
        )

    def close(self) -> None:
        """The HTTP transport owns no persistent resources."""

    def __enter__(self) -> TelegramUploader:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


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


def _part_upload_filename(
    *,
    file_path: Path,
    title: str,
    video_id: str,
    part_no: int,
    part_count: int,
    published_at: str | None = None,
) -> str:
    filename = Path(
        _upload_filename(
            file_path=file_path,
            title=title,
            video_id=video_id,
            published_at=published_at,
        )
    )
    return (
        f"{filename.stem}.part-{part_no:02d}-of-{part_count:02d}"
        f"{filename.suffix}"
    )


def _part_audio_title(title: str, *, part_no: int, part_count: int) -> str:
    suffix = f" (Part {part_no}/{part_count})"
    base = re.sub(r"\s+", " ", title).strip() or "Audio"
    available = max(0, 64 - len(suffix))
    trimmed = base[:available].rstrip()
    return f"{trimmed}{suffix}" if trimmed else suffix.strip()[:64]


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


def _bot_api_retry_after(payload: object) -> int | None:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        return None
    value = parameters.get("retry_after")
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


BotApiTransport = TelegramUploader


def create_telegram_transport(config: TelegramConfig) -> TelegramTransport:
    value = getattr(config, "upload_transport", "mtproto")
    if hasattr(value, "value"):
        value = value.value
    transport = str(value).strip().lower().replace("-", "_")
    if transport == "bot_api":
        return TelegramUploader(config)
    if transport == "mtproto":
        from .telegram_mtproto import MtprotoTransport

        return MtprotoTransport(config)
    raise TelegramUploadError(
        f"unsupported telegram.upload_transport: {value}",
        code="configuration",
        fallback_safe=True,
        transport=transport or None,
    )
