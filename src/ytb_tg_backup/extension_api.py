from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Any, Literal, Protocol

from .models import DiscoveryResult, Origin


EXTENSION_API_LEVEL = 1
EXTENSION_ENTRY_POINT_GROUP = "asmr_tg_backup.extensions"


class ExtensionError(RuntimeError):
    """Base error for extension discovery, configuration, and execution."""


class SourceError(RuntimeError):
    """A source adapter failure with stable retry and diagnostic metadata."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "source_error",
        retry_after: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


class SourceAdapter(Protocol):
    provider: str

    def discover(
        self,
        origin: Origin,
        checkpoint: str | None = None,
    ) -> DiscoveryResult: ...


@dataclass(frozen=True)
class HttpRequest:
    url: str
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None
    timeout_seconds: float = 30
    max_response_bytes: int = 10 * 1024 * 1024


@dataclass(frozen=True)
class HttpResponse:
    status: int
    url: str
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class RouteRequest:
    scope: str
    provider: str | None = None
    origin_id: str | None = None
    media_id: int | None = None
    job_id: int | None = None
    attempt: int = 0
    target_url: str | None = None
    phase: str = "prepare"
    idempotent: bool = True
    features: frozenset[str] = frozenset()


RouteMode = Literal["inherit", "direct", "proxy"]


@dataclass(frozen=True, repr=False)
class RouteLease:
    route_id: str
    mode: RouteMode = "inherit"
    proxy_url: str | None = field(default=None, repr=False)
    metadata: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.route_id.strip():
            raise ValueError("route_id must not be empty")
        if self.mode not in {"inherit", "direct", "proxy"}:
            raise ValueError(f"unsupported route mode: {self.mode}")
        if self.mode == "proxy" and not (self.proxy_url or "").strip():
            raise ValueError("proxy routes require proxy_url")
        if self.mode != "proxy" and self.proxy_url is not None:
            raise ValueError("only proxy routes may include proxy_url")

    def __repr__(self) -> str:
        return f"RouteLease(route_id={self.route_id!r}, mode={self.mode!r})"


@dataclass(frozen=True)
class RouteOutcome:
    success: bool
    elapsed_seconds: float
    error_type: str | None = None


class ConnectionPolicy(Protocol):
    def acquire(self, request: RouteRequest) -> RouteLease: ...

    def report(
        self,
        request: RouteRequest,
        lease: RouteLease,
        outcome: RouteOutcome,
    ) -> None: ...


class HttpTransport(Protocol):
    def request(self, request: HttpRequest, route: RouteLease) -> HttpResponse: ...


class HttpClient(Protocol):
    def request(
        self,
        request: HttpRequest,
        route_request: RouteRequest,
    ) -> HttpResponse: ...


@dataclass(frozen=True)
class SourceAdapterContext:
    http: HttpClient
    logger: logging.Logger
    data_dir: Path


OriginValidator = Callable[[Origin], Origin]
OriginIdentity = Callable[[Origin], tuple[str, str, str, str]]
OriginPredicate = Callable[[Origin], bool]
OriginText = Callable[[Origin], str | None]
SourceAdapterFactory = Callable[[SourceAdapterContext], SourceAdapter]


@dataclass(frozen=True)
class SourceProviderDefinition:
    provider: str
    kinds: frozenset[str]
    default_kind: str
    adapter_factory: SourceAdapterFactory
    validate_origin: OriginValidator
    identity: OriginIdentity
    is_live_origin: OriginPredicate
    seed_content_kind: OriginText
    poll_variant: OriginText
    route_features: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        provider = self.provider.strip().lower()
        kinds = frozenset(kind.strip().lower() for kind in self.kinds if kind.strip())
        default_kind = self.default_kind.strip().lower()
        if not provider:
            raise ValueError("source provider id must not be empty")
        if not kinds:
            raise ValueError(f"source provider {provider!r} requires at least one kind")
        if default_kind not in kinds:
            raise ValueError(
                f"source provider {provider!r} default kind must be registered"
            )
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "kinds", kinds)
        object.__setattr__(self, "default_kind", default_kind)
        object.__setattr__(
            self,
            "route_features",
            frozenset(
                feature.strip().lower()
                for feature in self.route_features
                if feature.strip()
            ),
        )


@dataclass(frozen=True)
class ExtensionManifest:
    id: str
    version: str
    api_level: int = EXTENSION_API_LEVEL
    capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ExtensionContext:
    extension_id: str
    config: Mapping[str, Any]
    data_dir: Path
    logger: logging.Logger


@dataclass(frozen=True)
class RuntimeFactoryContext:
    data_dir: Path
    logger: logging.Logger


ConnectionPolicyFactory = Callable[[RuntimeFactoryContext], ConnectionPolicy]
HttpTransportFactory = Callable[[RuntimeFactoryContext], HttpTransport]


class ExtensionRegistrar(Protocol):
    def add_source_provider(self, definition: SourceProviderDefinition) -> None: ...

    def set_connection_policy(self, factory: ConnectionPolicyFactory) -> None: ...

    def set_http_transport(self, factory: HttpTransportFactory) -> None: ...


class Extension(Protocol):
    manifest: ExtensionManifest

    def register(
        self,
        registrar: ExtensionRegistrar,
        context: ExtensionContext,
    ) -> None: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...
