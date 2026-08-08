from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
import inspect
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, NamedTuple
import tempfile

from .telegram_types import (
    BeforeCommit,
    TelegramTransport,
    TelegramUploadError,
    TelegramUploadPath,
    TelegramUploadResult,
)


class _TelethonBindings(NamedTuple):
    client_factory: Callable[..., Any]
    audio_attribute: Callable[..., Any]
    filename_attribute: Callable[..., Any]
    video_attribute: Callable[..., Any]
    flood_wait_error: type[BaseException]
    rpc_error: type[BaseException]


@dataclass(frozen=True)
class _PreparedUpload:
    entity: Any
    file: Any
    thumbnail: Any | None
    attributes: list[Any]
    caption: str
    force_document: bool
    supports_streaming: bool


def _load_telethon() -> _TelethonBindings:
    try:
        from telethon import TelegramClient
        from telethon.errors import FloodWaitError, RPCError
        from telethon.tl.types import (
            DocumentAttributeAudio,
            DocumentAttributeFilename,
            DocumentAttributeVideo,
        )
    except ImportError as exc:
        raise TelegramUploadError(
            "Telethon is required for telegram.upload_transport=mtproto",
            code="dependency_missing",
            fallback_safe=True,
            transport="mtproto",
        ) from exc
    return _TelethonBindings(
        client_factory=TelegramClient,
        audio_attribute=DocumentAttributeAudio,
        filename_attribute=DocumentAttributeFilename,
        video_attribute=DocumentAttributeVideo,
        flood_wait_error=FloodWaitError,
        rpc_error=RPCError,
    )


class MtprotoTransport(TelegramTransport):
    """Synchronous facade over one process-local asynchronous Telethon client."""

    transport_name = "mtproto"

    def __init__(
        self,
        config: Any,
        *,
        bindings: _TelethonBindings | None = None,
    ):
        self.config = config
        self._bindings = bindings
        self._client: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._runtime_lock = threading.RLock()
        self._runtime_ready = threading.Event()
        self._closed = False

    def validate(self) -> None:
        if not bool(getattr(self.config, "enabled", False)):
            return
        if not str(getattr(self.config, "bot_token", "")).strip():
            raise self._error(
                "telegram.bot_token is required when telegram.enabled=true",
                code="configuration",
            )
        if _bot_id_from_token(str(getattr(self.config, "bot_token", ""))) is None:
            raise self._error(
                "telegram.bot_token must contain a numeric bot id followed by ':'",
                code="configuration",
            )
        if not str(getattr(self.config, "chat_id", "")).strip():
            raise self._error(
                "telegram.chat_id is required when telegram.enabled=true",
                code="configuration",
            )
        api_id = self._mtproto_value("api_id", 0)
        try:
            valid_api_id = int(api_id) > 0
        except (TypeError, ValueError):
            valid_api_id = False
        if not valid_api_id:
            raise self._error(
                "telegram.mtproto.api_id must be a positive integer",
                code="configuration",
            )
        if not str(self._mtproto_value("api_hash", "")).strip():
            raise self._error(
                "telegram.mtproto.api_hash is required",
                code="configuration",
            )
        if not str(self._mtproto_value("session_path", "")).strip():
            raise self._error(
                "telegram.mtproto.session_path is required",
                code="configuration",
            )
        max_upload_bytes = self._mtproto_value("max_upload_bytes", 0)
        try:
            valid_max_upload_bytes = int(max_upload_bytes) > 0
        except (TypeError, ValueError):
            valid_max_upload_bytes = False
        if not valid_max_upload_bytes:
            raise self._error(
                "telegram.mtproto.max_upload_bytes must be positive",
                code="configuration",
            )
        self._get_bindings()

    def upload(
        self,
        file_path: TelegramUploadPath,
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
        paths = [file_path] if isinstance(file_path, Path) else list(file_path)
        if not paths:
            raise self._error("at least one file is required for Telegram upload", code="invalid_request")
        if len(paths) != 1:
            raise self._error(
                "MTProto transport accepts one file per delivery",
                code="multiple_files_unsupported",
            )
        source = paths[0]
        try:
            file_size = source.stat().st_size
        except OSError as exc:
            raise self._error(
                f"Telegram upload file is unavailable: {source}",
                code="file_unavailable",
            ) from exc
        max_upload_bytes = int(self._mtproto_value("max_upload_bytes", 0))
        if file_size > max_upload_bytes:
            raise self._error(
                f"file size {file_size} exceeds telegram.mtproto.max_upload_bytes={max_upload_bytes}",
                code="upload_too_large",
            )

        upload_filename = _upload_filename(
            file_path=source,
            title=title,
            video_id=video_id,
            published_at=published_at,
        )
        caption = self._caption(title=title, url=url, feed_name=feed_name, video_id=video_id)
        timeout = float(getattr(self.config, "upload_timeout_seconds", 7200))
        deadline = time.monotonic() + timeout

        with tempfile.TemporaryDirectory(prefix="ytb-tg-mtproto-") as tmp:
            upload_path = _safe_upload_path(source, Path(tmp) / upload_filename)
            safe_thumbnail_path = None
            if thumbnail_path is not None and thumbnail_path.exists():
                safe_thumbnail_path = _safe_upload_path(
                    thumbnail_path,
                    Path(tmp) / "thumbnail.jpg",
                )
            try:
                prepared = self._submit(
                    self._prepare_upload(
                        upload_path=upload_path,
                        upload_filename=upload_filename,
                        thumbnail_path=safe_thumbnail_path,
                        title=title,
                        performer=performer or feed_name,
                        duration_seconds=duration_seconds,
                        video_width=video_width,
                        video_height=video_height,
                        caption=caption,
                    ),
                    timeout=self._remaining(deadline),
                )
            except TelegramUploadError:
                raise
            except FutureTimeoutError as exc:
                raise self._error(
                    f"Telegram MTProto upload timed out after {int(timeout)} seconds",
                    code="timeout",
                    uncertain=False,
                    fallback_safe=True,
                ) from exc
            except Exception as exc:
                raise self._translate_error(exc, commit_started=False) from exc

            # Run the persistence boundary in the delivery worker's thread.
            # No Telegram message exists yet; callback failures therefore retain
            # their original exception type and never trigger a remote fallback.
            if before_commit is not None:
                before_commit()

            try:
                message_id = self._submit(
                    self._commit_upload(prepared),
                    timeout=self._remaining(deadline),
                )
            except TelegramUploadError:
                raise
            except FutureTimeoutError as exc:
                raise self._error(
                    f"Telegram MTProto upload timed out after {int(timeout)} seconds",
                    code="timeout",
                    uncertain=True,
                    fallback_safe=False,
                ) from exc
            except Exception as exc:
                raise self._translate_error(exc, commit_started=True) from exc
        return message_id

    async def _prepare_upload(
        self,
        *,
        upload_path: Path,
        upload_filename: str,
        thumbnail_path: Path | None,
        title: str,
        performer: str,
        duration_seconds: float | int | None,
        video_width: int | None,
        video_height: int | None,
        caption: str,
    ) -> _PreparedUpload:
        client = await self._ensure_client()
        entity = await client.get_input_entity(
            _telegram_entity_reference(getattr(self.config, "chat_id", ""))
        )
        uploaded = await client.upload_file(str(upload_path), file_name=upload_filename)
        uploaded_thumbnail = None
        if thumbnail_path is not None:
            uploaded_thumbnail = await client.upload_file(str(thumbnail_path), file_name="thumbnail.jpg")
        attributes, force_document, supports_streaming = self._media_attributes(
            upload_filename=upload_filename,
            title=title,
            performer=performer,
            duration_seconds=duration_seconds,
            video_width=video_width,
            video_height=video_height,
        )
        return _PreparedUpload(
            entity=entity,
            file=uploaded,
            thumbnail=uploaded_thumbnail,
            attributes=attributes,
            caption=caption,
            force_document=force_document,
            supports_streaming=supports_streaming,
        )

    async def _commit_upload(self, prepared: _PreparedUpload) -> int:
        client = await self._ensure_client()
        message = await client.send_file(
            prepared.entity,
            file=prepared.file,
            caption=prepared.caption,
            parse_mode=None,
            force_document=prepared.force_document,
            thumb=prepared.thumbnail,
            attributes=prepared.attributes,
            supports_streaming=prepared.supports_streaming,
        )
        try:
            return int(message.id)
        except (AttributeError, TypeError, ValueError) as exc:
            raise self._error(
                "Telegram MTProto reported success without a valid message id",
                code="invalid_response",
                uncertain=True,
                fallback_safe=False,
            ) from exc

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        bindings = self._get_bindings()
        session_path = Path(str(self._mtproto_value("session_path", ""))).expanduser()
        session_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._prepare_session_file(session_path)
        client = bindings.client_factory(
            str(session_path),
            int(self._mtproto_value("api_id", 0)),
            str(self._mtproto_value("api_hash", "")),
            receive_updates=False,
        )
        try:
            started = client.start(bot_token=str(getattr(self.config, "bot_token", "")))
            if inspect.isawaitable(started):
                await started
            self._protect_session_file(session_path)
            identity = client.get_me()
            if inspect.isawaitable(identity):
                identity = await identity
            expected_bot_id = _bot_id_from_token(str(getattr(self.config, "bot_token", "")))
            actual_bot_id = getattr(identity, "id", None)
            is_bot = bool(getattr(identity, "bot", False))
            if expected_bot_id is None or actual_bot_id != expected_bot_id or not is_bot:
                raise self._error(
                    "Telegram MTProto session belongs to a different bot; remove the session "
                    "file or restore the matching bot token",
                    code="session_identity_mismatch",
                    fallback_safe=True,
                )
        except Exception:
            try:
                disconnected = client.disconnect()
                if inspect.isawaitable(disconnected):
                    await disconnected
            except Exception:
                pass
            raise
        self._client = client
        return client

    def _media_attributes(
        self,
        *,
        upload_filename: str,
        title: str,
        performer: str,
        duration_seconds: float | int | None,
        video_width: int | None,
        video_height: int | None,
    ) -> tuple[list[Any], bool, bool]:
        bindings = self._get_bindings()
        attributes = [bindings.filename_attribute(file_name=upload_filename)]
        media_type = "document" if bool(getattr(self.config, "send_as_document", False)) else str(
            getattr(self.config, "media_type", "audio")
        )
        duration = max(0, int(float(duration_seconds or 0)))
        if media_type == "audio":
            attributes.append(
                bindings.audio_attribute(
                    duration=duration,
                    voice=False,
                    title=_metadata_text(title, fallback="Audio"),
                    performer=_metadata_text(performer, fallback="Unknown"),
                )
            )
            return attributes, False, False
        if media_type == "video":
            width = max(1, int(video_width or 1))
            height = max(1, int(video_height or 1))
            attributes.append(
                bindings.video_attribute(
                    duration=duration,
                    w=width,
                    h=height,
                    supports_streaming=True,
                )
            )
            return attributes, False, True
        return attributes, True, False

    def _submit(self, coroutine: Any, *, timeout: float) -> Any:
        loop = self._ensure_runtime()
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            future.cancel()
            raise

    def _ensure_runtime(self) -> asyncio.AbstractEventLoop:
        with self._runtime_lock:
            if self._closed:
                raise self._error(
                    "Telegram MTProto transport is closed",
                    code="transport_closed",
                )
            if self._loop is not None:
                return self._loop
            self._runtime_ready.clear()
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=self._run_loop,
                args=(loop,),
                name="telegram-mtproto",
                daemon=True,
            )
            self._loop = loop
            self._thread = thread
            thread.start()
        if not self._runtime_ready.wait(timeout=5):
            raise self._error(
                "Telegram MTProto event loop did not start",
                code="runtime_start_failed",
            )
        return loop

    def _run_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        self._runtime_ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def close(self) -> None:
        with self._runtime_lock:
            if self._closed:
                return
            loop = self._loop
            thread = self._thread
        try:
            if loop is not None and self._client is not None:
                future = asyncio.run_coroutine_threadsafe(self._disconnect(), loop)
                future.result(timeout=10)
        finally:
            with self._runtime_lock:
                self._closed = True
                if loop is not None:
                    loop.call_soon_threadsafe(loop.stop)
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=10)

    async def _disconnect(self) -> None:
        if self._client is None:
            return
        disconnected = self._client.disconnect()
        if inspect.isawaitable(disconnected):
            await disconnected
        self._client = None

    def __enter__(self) -> MtprotoTransport:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _get_bindings(self) -> _TelethonBindings:
        if self._bindings is None:
            self._bindings = _load_telethon()
        return self._bindings

    def _mtproto_value(self, name: str, default: Any) -> Any:
        section = getattr(self.config, "mtproto", None)
        if section is None:
            return default
        return getattr(section, name, default)

    def _caption(self, *, title: str, url: str, feed_name: str, video_id: str) -> str:
        tag = _tag_from_feed_name(feed_name)
        template = str(
            getattr(
                self.config,
                "caption_template",
                "{title}\n\n{url}\n\n#{tag}",
            )
        )
        try:
            caption = template.format(
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

    def _translate_error(self, exc: Exception, *, commit_started: bool) -> TelegramUploadError:
        if isinstance(exc, TelegramUploadError):
            return exc
        bindings = self._get_bindings()
        if isinstance(exc, bindings.flood_wait_error):
            retry_after = _retry_after(exc)
            return self._error(
                f"Telegram MTProto rate limited the upload for {retry_after} seconds",
                code="flood_wait",
                uncertain=False,
                fallback_safe=False,
                retry_after=retry_after,
            )
        if isinstance(exc, bindings.rpc_error):
            return self._error(
                f"Telegram MTProto rejected the upload: {self._redact(str(exc))[:500]}",
                code=_rpc_error_code(exc),
                uncertain=False,
                fallback_safe=not commit_started,
            )
        return self._error(
            f"Telegram MTProto upload failed: {self._redact(str(exc))[:500]}",
            code="transport_error",
            uncertain=commit_started,
            fallback_safe=not commit_started,
        )

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

    def _redact(self, value: str) -> str:
        for secret in (
            str(getattr(self.config, "bot_token", "")),
            str(self._mtproto_value("api_hash", "")),
        ):
            if secret:
                value = value.replace(secret, "<redacted>")
        return value

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FutureTimeoutError()
        return remaining

    @staticmethod
    def _protect_session_file(session_path: Path) -> None:
        candidates = (session_path, Path(f"{session_path}.session"))
        for candidate in candidates:
            try:
                if candidate.exists():
                    os.chmod(candidate, 0o600)
            except OSError:
                # Telethon can still use a session on filesystems without POSIX modes.
                pass

    @classmethod
    def _prepare_session_file(cls, session_path: Path) -> None:
        actual_path = (
            session_path
            if session_path.name.endswith(".session")
            else Path(f"{session_path}.session")
        )
        actual_path.touch(mode=0o600, exist_ok=True)
        cls._protect_session_file(session_path)


def _upload_filename(
    *,
    file_path: Path,
    title: str,
    video_id: str,
    published_at: str | None,
) -> str:
    date = _date_from_published_at(published_at) or _date_from_path(file_path) or "unknown-date"
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
    cleaned = "".join(
        "_" if char in {'/', '\\', ':', '*', '?', '"', '<', '>', '|', ';', ','} else char
        for char in value
    )
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


def _tag_from_feed_name(feed_name: str) -> str:
    base = feed_name.split(" (", 1)[0].strip()
    if base.startswith("@"):
        base = base[1:]
    tag = "".join(char if char.isalnum() or char == "_" else "_" for char in base)
    tag = "_".join(part for part in tag.split("_") if part)
    return tag[:80]


def _metadata_text(value: str, *, fallback: str) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip()
    return (cleaned or fallback)[:128]


def _retry_after(exc: BaseException) -> int:
    value = getattr(exc, "seconds", getattr(exc, "value", 0))
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _bot_id_from_token(bot_token: str) -> int | None:
    bot_id, separator, _ = bot_token.strip().partition(":")
    if not separator or not bot_id.isascii() or not bot_id.isdecimal():
        return None
    value = int(bot_id)
    return value if value > 0 else None


def _telegram_entity_reference(chat_id: object) -> str | int:
    """Keep usernames textual while preserving Telegram's numeric peer semantics."""
    value = str(chat_id).strip()
    digits = value[1:] if value.startswith("-") else value
    if digits and digits.isascii() and digits.isdecimal():
        return int(value)
    return value


def _rpc_error_code(exc: BaseException) -> str:
    name = type(exc).__name__
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return snake.removesuffix("_error") or "rpc_error"
