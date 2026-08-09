from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from ipaddress import IPv4Address, IPv6Address, ip_address
import os
import time
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit
from urllib.request import ProxyHandler, build_opener, urlopen

from .extension_api import (
    ConnectionPolicy,
    HttpRequest,
    HttpResponse,
    HttpTransport,
    RouteLease,
    RouteOutcome,
    RouteRequest,
)


_IPV6_LOOPBACK = IPv6Address("::1")
_PROXY_ENV_KEYS = {
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}


class NetworkScope(StrEnum):
    ORIGIN_RESOLVE = "origin.resolve"
    SOURCE_NOTIFICATION = "source.notification"
    SOURCE_DISCOVERY = "source.discovery"
    MEDIA_PROBE = "media.probe"
    MEDIA_DOWNLOAD = "media.download"
    TELEGRAM_CONTROL_RECEIVE = "telegram.control.receive"
    TELEGRAM_CONTROL_SEND = "telegram.control.send"
    TELEGRAM_DELIVERY_BOT_API = "telegram.delivery.bot_api"
    TELEGRAM_DELIVERY_MTPROTO = "telegram.delivery.mtproto"


class EnvironmentConnectionPolicy:
    """Compatibility policy that preserves the process environment."""

    def acquire(self, request: RouteRequest) -> RouteLease:
        return RouteLease(route_id="environment", mode="inherit")

    def report(
        self,
        request: RouteRequest,
        lease: RouteLease,
        outcome: RouteOutcome,
    ) -> None:
        return None


class UrllibHttpTransport:
    """Default HTTP implementation for inherited, direct, and HTTP routes.

    SOCKS is deliberately supplied by an optional extension so the core does
    not acquire a mandatory SOCKS dependency.
    """

    def request(self, request: HttpRequest, route: RouteLease) -> HttpResponse:
        if request.max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        if route.mode == "direct" or is_loopback_url(request.url):
            open_request = build_opener(ProxyHandler({})).open
        elif route.mode == "proxy":
            proxy_url = route.proxy_url or ""
            scheme = urlsplit(proxy_url).scheme.lower()
            if scheme not in {"http", "https"}:
                raise RuntimeError(
                    "the active HTTP transport does not support this proxy route; "
                    "install the extension-provided HTTP transport"
                )
            open_request = build_opener(
                ProxyHandler({"http": proxy_url, "https": proxy_url})
            ).open
        else:
            open_request = urlopen

        from urllib.request import Request

        urllib_request = Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method=request.method.upper(),
        )
        try:
            response = open_request(
                urllib_request,
                timeout=request.timeout_seconds,
            )
        except HTTPError as exc:
            response = exc
        with response:
            body = response.read(request.max_response_bytes + 1)
            if len(body) > request.max_response_bytes:
                raise ValueError(
                    f"HTTP response exceeded {request.max_response_bytes} bytes"
                )
            return HttpResponse(
                status=int(response.status),
                url=str(response.geturl()),
                headers={str(key): str(value) for key, value in response.headers.items()},
                body=body,
            )


@dataclass(frozen=True)
class RoutedConnection:
    lease: RouteLease

    @property
    def route_id(self) -> str:
        return self.lease.route_id

    def yt_dlp_args(self) -> list[str]:
        if self.lease.mode == "proxy":
            return ["--proxy", self.lease.proxy_url or ""]
        if self.lease.mode == "direct":
            return ["--proxy", ""]
        return []

    def curl_args(self) -> list[str]:
        if self.lease.mode == "proxy":
            return ["--proxy", self.lease.proxy_url or ""]
        if self.lease.mode == "direct":
            return ["--noproxy", "*"]
        return []

    def process_environment(
        self,
        environment: Mapping[str, str] | None = None,
    ) -> dict[str, str] | None:
        if self.lease.mode == "inherit":
            return None if environment is None else dict(environment)
        result = dict(os.environ if environment is None else environment)
        for key in tuple(result):
            if key.casefold() in _PROXY_ENV_KEYS:
                result.pop(key, None)
        return result

    def telethon_proxy(self) -> tuple[object, ...] | None:
        if self.lease.mode != "proxy":
            return None
        return _telethon_proxy(self.lease.proxy_url or "")


class ConnectionRuntime:
    def __init__(
        self,
        policy: ConnectionPolicy | None = None,
        http_transport: HttpTransport | None = None,
    ):
        self.policy = policy or EnvironmentConnectionPolicy()
        self.http_transport = http_transport or UrllibHttpTransport()

    @contextmanager
    def route(self, request: RouteRequest) -> Iterator[RoutedConnection]:
        started = time.monotonic()
        if request.target_url and is_loopback_url(request.target_url):
            lease = RouteLease(route_id="loopback", mode="direct")
            report = False
        else:
            lease = self.policy.acquire(request)
            report = True
        try:
            yield RoutedConnection(lease)
        except BaseException as exc:
            if report:
                self._report(
                    request,
                    lease,
                    RouteOutcome(
                        success=False,
                        elapsed_seconds=max(0.0, time.monotonic() - started),
                        error_type=type(exc).__name__,
                    ),
                )
            raise
        else:
            if report:
                self._report(
                    request,
                    lease,
                    RouteOutcome(
                        success=True,
                        elapsed_seconds=max(0.0, time.monotonic() - started),
                    ),
                )

    def request(
        self,
        request: HttpRequest,
        route_request: RouteRequest,
    ) -> HttpResponse:
        if route_request.target_url is None:
            route_request = replace(route_request, target_url=request.url)
        with self.route(route_request) as route:
            return self.http_transport.request(request, route.lease)

    def _report(
        self,
        request: RouteRequest,
        lease: RouteLease,
        outcome: RouteOutcome,
    ) -> None:
        try:
            self.policy.report(request, lease, outcome)
        except Exception:
            # Route health reporting must never change the primary operation's
            # success or failure semantics.
            return


def is_loopback_url(value: str) -> bool:
    """Return whether an HTTP(S) URL has an unambiguous loopback host."""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except (AttributeError, ValueError):
        return False

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not hostname:
        return False

    normalized_hostname = hostname.casefold()
    if normalized_hostname in {"localhost", "localhost."}:
        return True

    try:
        address = ip_address(normalized_hostname)
    except ValueError:
        return False
    if isinstance(address, IPv4Address):
        return address.is_loopback
    return isinstance(address, IPv6Address) and address == _IPV6_LOOPBACK


def _telethon_proxy(proxy_url: str) -> tuple[object, ...]:
    try:
        parsed = urlsplit(proxy_url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("proxy URL is malformed") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"socks5", "socks5h", "socks4", "http", "https"}:
        raise ValueError(f"unsupported Telethon proxy scheme: {scheme or '<missing>'}")
    if not parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("proxy URL must contain only scheme, credentials, host, and port")
    if port is None:
        port = 1080 if scheme.startswith("socks") else 8080
    proxy_type = "http" if scheme == "https" else scheme.removesuffix("h")
    remote_dns = scheme != "socks5"
    return (
        proxy_type,
        parsed.hostname,
        port,
        remote_dns,
        unquote(parsed.username) if parsed.username is not None else None,
        unquote(parsed.password) if parsed.password is not None else None,
    )
