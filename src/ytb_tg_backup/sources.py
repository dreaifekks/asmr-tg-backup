from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import time
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .config import TwitchConfig
from .feed import fetch_feed, parse_feed
from .models import DiscoveryResult, MediaCandidate, Origin
from .youtube import youtube_channel_feed_url


class SourceError(RuntimeError):
    def __init__(self, message: str, *, code: str = "source_error", retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


class SourceAdapter(Protocol):
    provider: str

    def discover(self, origin: Origin, checkpoint: str | None = None) -> DiscoveryResult: ...


class YouTubePublicSource:
    provider = "youtube"

    def __init__(self, fetcher: Callable[[str], bytes] = fetch_feed):
        self.fetcher = fetcher

    def discover(self, origin: Origin, checkpoint: str | None = None) -> DiscoveryResult:
        if origin.kind not in {"uploads", "vod_after_live"}:
            raise SourceError(f"unsupported YouTube origin kind: {origin.kind}", code="invalid_origin")
        url = youtube_channel_feed_url(origin.external_id)
        entries = parse_feed(self.fetcher(url), origin.id, origin.name)
        return DiscoveryResult(
            items=[
                MediaCandidate(
                    provider="youtube",
                    content_kind="video",
                    external_id=entry.video_id,
                    title=entry.title,
                    url=entry.url,
                    published_at=entry.published_at,
                    updated_at=entry.updated_at,
                )
                for entry in entries
            ]
        )


class RssSource:
    provider = "rss"

    def __init__(self, fetcher: Callable[[str], bytes] = fetch_feed):
        self.fetcher = fetcher

    def discover(self, origin: Origin, checkpoint: str | None = None) -> DiscoveryResult:
        entries = sorted(
            parse_feed(self.fetcher(origin.external_id), origin.id, origin.name),
            key=lambda entry: (entry.published_at or "", entry.updated_at or "", entry.video_id),
            reverse=True,
        )
        allowed_hosts = origin.options.get("allowed_media_hosts", [])
        if not isinstance(allowed_hosts, list) or not all(isinstance(item, str) for item in allowed_hosts):
            raise SourceError("rss allowed_media_hosts must be an array of host names", code="invalid_origin")
        allow_private = bool(origin.options.get("allow_private_media", False))
        items: list[MediaCandidate] = []
        for entry in entries:
            if not entry.url:
                continue
            validate_public_media_url(
                entry.url,
                allowed_hosts=tuple(allowed_hosts),
                allow_private=allow_private,
            )
            items.append(
                MediaCandidate(
                    provider="rss",
                    content_kind="media",
                    external_id=_rss_external_id(entry.url),
                    title=entry.title,
                    url=entry.url,
                    published_at=entry.published_at,
                    updated_at=entry.updated_at,
                    metadata={
                        "feed_entry_id": entry.video_id,
                        "allow_private_media": allow_private,
                        "allowed_media_hosts": allowed_hosts,
                    },
                )
            )
        return DiscoveryResult(items=items)


class TwitchHelixSource:
    provider = "twitch"

    def __init__(self, config: TwitchConfig):
        self.config = config
        self._access_token = config.access_token

    def discover(self, origin: Origin, checkpoint: str | None = None) -> DiscoveryResult:
        self._validate_config()
        user_id = self._resolve_user_id(origin.external_id)
        video_type = {
            "vods": "archive",
            "highlights": "highlight",
            "uploads": "upload",
        }.get(origin.kind)
        if video_type is None:
            raise SourceError(f"unsupported Twitch origin kind: {origin.kind}", code="invalid_origin")

        previous = _decode_twitch_checkpoint(checkpoint)
        previous_id = str(previous.get("external_id") or "")
        items: list[MediaCandidate] = []
        newest_checkpoint: dict[str, str] | None = None
        reached_previous = False
        cursor: str | None = None
        page_limit = self.config.max_pages_per_poll if (previous_id or origin.bootstrap == "all") else 1
        for _ in range(page_limit):
            params = {"user_id": user_id, "type": video_type, "first": "100"}
            if cursor:
                params["after"] = cursor
            payload = self._api_json("videos", params)
            for raw in _payload_list(payload, "data"):
                if not isinstance(raw, dict):
                    raise SourceError("Twitch API returned an invalid video item", code="invalid_response")
                external_id = str(raw.get("id") or "")
                url = str(raw.get("url") or "")
                if not external_id or not url:
                    continue
                published_at = str(raw.get("published_at") or raw.get("created_at") or "") or None
                if previous_id and external_id == previous_id:
                    reached_previous = True
                    break
                if previous_id and _is_older_than_checkpoint(published_at, previous.get("published_at")):
                    reached_previous = True
                    break
                if newest_checkpoint is None:
                    newest_checkpoint = {
                        "external_id": external_id,
                        "published_at": published_at or "",
                    }
                items.append(
                    MediaCandidate(
                        provider="twitch",
                        content_kind="vod" if raw.get("type") == "archive" else str(raw.get("type") or "video"),
                        external_id=external_id,
                        title=str(raw.get("title") or external_id),
                        url=url,
                        published_at=published_at,
                        updated_at=None,
                        visibility=str(raw.get("viewable") or "public"),
                        metadata={
                            "broadcaster_id": user_id,
                            "stream_id": raw.get("stream_id"),
                            "duration": raw.get("duration"),
                            "thumbnail_url": raw.get("thumbnail_url"),
                            "muted_segments": raw.get("muted_segments"),
                        },
                    )
                )
            if reached_previous:
                cursor = None
                break
            pagination = payload.get("pagination") or {}
            if not isinstance(pagination, dict):
                raise SourceError("Twitch API returned invalid pagination", code="invalid_response")
            cursor = str(pagination.get("cursor") or "") or None
            if not cursor:
                break
        if previous_id and not reached_previous and cursor:
            raise SourceError(
                "Twitch pagination limit reached before the previous checkpoint; increase max_pages_per_poll",
                code="pagination_limit",
            )
        if not previous_id and origin.bootstrap == "all" and cursor:
            raise SourceError(
                "Twitch bootstrap exceeded max_pages_per_poll; increase the limit or use bootstrap='latest'",
                code="pagination_limit",
            )
        next_checkpoint = newest_checkpoint or previous
        return DiscoveryResult(items=items, cursor=_encode_twitch_checkpoint(next_checkpoint))

    def _resolve_user_id(self, value: str) -> str:
        candidate = value.strip()
        if candidate.isdigit():
            return candidate
        login = candidate.removeprefix("@").lower()
        if not re.fullmatch(r"[a-z0-9_]{1,25}", login):
            raise SourceError("invalid Twitch broadcaster id or login", code="invalid_origin")
        payload = self._api_json("users", {"login": login})
        users = _payload_list(payload, "data")
        if users and not isinstance(users[0], dict):
            raise SourceError("Twitch API returned an invalid user item", code="invalid_response")
        if not users or not users[0].get("id"):
            raise SourceError(f"Twitch broadcaster not found: {login}", code="not_found")
        return str(users[0]["id"])

    def _validate_config(self) -> None:
        if not self.config.client_id or not (self._access_token or self.config.client_secret):
            raise SourceError(
                "Twitch source requires client_id and either access_token or client_secret",
                code="auth_missing",
            )

    def _api_json(self, path: str, params: dict[str, str], *, retry_auth: bool = True) -> dict[str, object]:
        if not self._access_token:
            self._refresh_app_token()
        url = f"{self.config.api_base}/{path}?{urlencode(params)}"
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Client-Id": self.config.client_id,
                "User-Agent": "ytb-tg-backup/0.1",
            },
        )
        try:
            with urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                payload = _read_json_response(response, label="Twitch API")
        except HTTPError as exc:
            if exc.code == 401 and retry_auth and self.config.client_secret:
                self._refresh_app_token()
                return self._api_json(path, params, retry_auth=False)
            reset_at = _int_header(exc.headers, "Ratelimit-Reset") if exc.code == 429 else None
            retry_after = max(1, reset_at - int(time.time())) if reset_at else None
            code = "rate_limited" if exc.code == 429 else "auth_invalid" if exc.code in {401, 403} else "http_error"
            raise SourceError(f"Twitch API HTTP {exc.code}", code=code, retry_after=retry_after) from None
        except (URLError, TimeoutError) as exc:
            raise SourceError(f"Twitch API request failed: {exc}", code="network_error") from None
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise SourceError(f"Twitch API returned invalid JSON: {exc}", code="invalid_response") from None
        if not isinstance(payload, dict):
            raise SourceError("Twitch API returned a non-object response", code="invalid_response")
        return payload

    def _refresh_app_token(self) -> None:
        if not self.config.client_id or not self.config.client_secret:
            raise SourceError("Twitch app token cannot be refreshed without client_secret", code="auth_missing")
        request = Request(
            f"{self.config.oauth_base}/token",
            data=urlencode(
                {
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "grant_type": "client_credentials",
                }
            ).encode("ascii"),
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "ytb-tg-backup/0.1"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                payload = _read_json_response(response, label="Twitch OAuth")
        except HTTPError as exc:
            raise SourceError(f"Twitch OAuth HTTP {exc.code}", code="auth_invalid") from None
        except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceError(f"Twitch OAuth failed: {exc}", code="auth_invalid") from None
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not token:
            raise SourceError("Twitch OAuth response did not include access_token", code="auth_invalid")
        self._access_token = str(token)


class SourceRegistry:
    def __init__(
        self,
        twitch: TwitchConfig,
        *,
        youtube_fetcher: Callable[[str], bytes] = fetch_feed,
        rss_fetcher: Callable[[str], bytes] = fetch_feed,
    ):
        youtube = YouTubePublicSource(youtube_fetcher)
        rss = RssSource(rss_fetcher)
        twitch_source = TwitchHelixSource(twitch)
        self._defaults: dict[str, SourceAdapter] = {
            "youtube": youtube,
            "rss": rss,
            "twitch": twitch_source,
        }
        self._adapters: dict[tuple[str, str], SourceAdapter] = {
            ("youtube", "uploads"): youtube,
            ("youtube", "vod_after_live"): youtube,
            ("rss", "feed"): rss,
            ("twitch", "vods"): twitch_source,
            ("twitch", "highlights"): twitch_source,
            ("twitch", "uploads"): twitch_source,
        }

    def get(self, provider: str, kind: str | None = None) -> SourceAdapter:
        try:
            if kind is None:
                return self._defaults[provider]
            return self._adapters[(provider, kind)]
        except KeyError:
            label = provider if kind is None else f"{provider}/{kind}"
            raise SourceError(f"unsupported source adapter: {label}", code="invalid_origin") from None


def _rss_external_id(url: str) -> str:
    return f"url-{hashlib.sha256(url.encode('utf-8')).hexdigest()}"


def validate_public_media_url(
    url: str,
    *,
    allowed_hosts: tuple[str, ...] = (),
    allow_private: bool = False,
) -> None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise SourceError("RSS media URL is malformed", code="unsafe_media_url") from exc
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise SourceError("RSS media URL must use HTTP or HTTPS", code="unsafe_media_url")
    if parts.username is not None or parts.password is not None:
        raise SourceError("RSS media URL must not include credentials", code="unsafe_media_url")
    host = parts.hostname.lower().rstrip(".")
    if allowed_hosts and not _host_matches_allowlist(host, allowed_hosts):
        raise SourceError("RSS media URL host is not in allowed_media_hosts", code="unsafe_media_url")
    if allow_private:
        return
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".home.arpa")):
        raise SourceError("RSS media URL resolves to a local host", code="unsafe_media_url")
    try:
        addresses = socket.getaddrinfo(
            host,
            port or (443 if parts.scheme.lower() == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise SourceError("RSS media URL host could not be resolved", code="network_error") from exc
    if not addresses:
        raise SourceError("RSS media URL host did not resolve", code="network_error")
    for address in addresses:
        raw_ip = str(address[4][0]).split("%", 1)[0]
        try:
            parsed_ip = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise SourceError("RSS media URL resolved to an invalid address", code="unsafe_media_url") from exc
        if not parsed_ip.is_global:
            raise SourceError("RSS media URL resolves to a non-public address", code="unsafe_media_url")


def _host_matches_allowlist(host: str, patterns: tuple[str, ...]) -> bool:
    for raw_pattern in patterns:
        pattern = raw_pattern.lower().rstrip(".")
        if pattern.startswith("*."):
            suffix = pattern[1:]
            if host.endswith(suffix) and host != suffix[1:]:
                return True
        elif host == pattern:
            return True
    return False


def _is_older_than_checkpoint(value: str | None, checkpoint: str | None) -> bool:
    if not value or not checkpoint:
        return False
    return value < checkpoint


def _int_header(headers: object, name: str) -> int | None:
    try:
        value = headers.get(name)  # type: ignore[attr-defined]
        return int(value) if value else None
    except (AttributeError, TypeError, ValueError):
        return None


def _decode_twitch_checkpoint(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise SourceError("stored Twitch checkpoint is invalid JSON", code="invalid_checkpoint") from None
    if not isinstance(payload, dict):
        raise SourceError("stored Twitch checkpoint is not an object", code="invalid_checkpoint")
    external_id = str(payload.get("external_id") or "")
    published_at = str(payload.get("published_at") or "")
    if not external_id:
        raise SourceError("stored Twitch checkpoint has no external_id", code="invalid_checkpoint")
    return {"external_id": external_id, "published_at": published_at}


def _encode_twitch_checkpoint(value: dict[str, str] | None) -> str | None:
    if not value or not value.get("external_id"):
        return None
    return json.dumps(
        {
            "external_id": value["external_id"],
            "published_at": value.get("published_at", ""),
            "version": 1,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _payload_list(payload: dict[str, object], key: str) -> list[object]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise SourceError(f"Twitch API response field {key!r} is not a list", code="invalid_response")
    return value


def _read_json_response(response: object, *, label: str) -> object:
    max_bytes = 5 * 1024 * 1024
    payload = response.read(max_bytes + 1)  # type: ignore[attr-defined]
    if len(payload) > max_bytes:
        raise SourceError(f"{label} response exceeds {max_bytes} bytes", code="invalid_response")
    return json.loads(payload.decode("utf-8"))
