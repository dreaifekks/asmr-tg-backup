from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Origin:
    """A configured source of remote media.

    ``external_id`` is interpreted by the provider adapter.  For YouTube it is
    a UC channel id; for Twitch it can be either a broadcaster id or login.
    """

    id: str
    provider: str
    kind: str
    name: str
    external_id: str
    enabled: bool = True
    bootstrap: str = "latest"
    credential_ref: str | None = None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MediaCandidate:
    provider: str
    content_kind: str
    external_id: str
    title: str
    url: str
    published_at: str | None
    updated_at: str | None = None
    live_status: str | None = None
    visibility: str = "public"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DiscoveryResult:
    items: list[MediaCandidate]
    cursor: str | None = None
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    media_id: int
    job_type: str
    target_key: str
    attempts: int
    max_attempts: int
    reason_code: str | None
    payload: dict[str, Any]
    lease_owner: str
    lease_token: str
