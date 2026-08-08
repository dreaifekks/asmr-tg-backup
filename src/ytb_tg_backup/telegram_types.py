from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol, TypeAlias, runtime_checkable


TelegramUploadPath: TypeAlias = Path | list[Path]
TelegramUploadResult: TypeAlias = int | list[int]
BeforeCommit: TypeAlias = Callable[[], None]


class TelegramUploadError(RuntimeError):
    """A delivery error with enough state for safe retry/fallback decisions."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "telegram_error",
        uncertain: bool = False,
        fallback_safe: bool | None = None,
        retry_after: int | None = None,
        transport: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.uncertain = uncertain
        self.fallback_safe = not uncertain if fallback_safe is None else fallback_safe
        self.retry_after = retry_after
        self.transport = transport


@runtime_checkable
class TelegramTransport(Protocol):
    transport_name: str

    def validate(self) -> None: ...

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
    ) -> TelegramUploadResult: ...

    def close(self) -> None: ...
