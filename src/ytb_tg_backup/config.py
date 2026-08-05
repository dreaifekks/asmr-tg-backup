from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import tomllib
from typing import Any

from .models import Origin
from .youtube import is_channel_id, youtube_channel_feed_url


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path
    poll_interval_seconds: int = 1800
    download_delay_seconds: int = 300
    max_items_per_poll: int = 3
    max_attempts: int = 5
    retry_seconds: int = 1800
    live_retry_seconds: int = 900
    worker_count: int = 1
    worker_poll_interval_seconds: int = 2
    job_lease_seconds: int = 900
    log_level: str = "INFO"


@dataclass(frozen=True)
class FeedConfig:
    id: str
    name: str
    url: str
    enabled: bool = True


@dataclass(frozen=True)
class RsshubConfig:
    base_url: str = "https://rss.dreaife.tokyo"


@dataclass(frozen=True)
class ChannelConfig:
    id: str
    name: str
    channel_id: str
    enabled: bool = True
    routes: list[str] = field(default_factory=lambda: ["channel"])


@dataclass(frozen=True)
class DownloadProfile:
    format: str | None = None
    merge_output_format: str | None = None
    extract_audio: bool | None = None
    audio_format: str | None = None
    audio_quality: str | None = None
    extra_args: list[str] | None = None


@dataclass(frozen=True)
class DownloadConfig:
    yt_dlp: str = "yt-dlp"
    ffmpeg: str = "ffmpeg"
    format: str = "bestaudio/best"
    merge_output_format: str = ""
    extract_audio: bool = True
    audio_format: str = "m4a"
    audio_quality: str = "0"
    output_template: str = "%(uploader|unknown)s/%(upload_date>%Y-%m-%d|unknown)s_%(title).80B_%(id)s.%(ext)s"
    archive_file: str = "download-archive.txt"
    restrict_filenames: bool = False
    write_info_json: bool = True
    write_thumbnail: bool = True
    probe_timeout_seconds: int = 180
    download_timeout_seconds: int = 21_600
    ffmpeg_timeout_seconds: int = 7_200
    extra_args: list[str] = field(default_factory=list)
    provider_profiles: dict[str, DownloadProfile] = field(default_factory=dict)


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    api_base: str = "https://api.telegram.org"
    media_type: str = "audio"
    send_as_document: bool = False
    max_upload_bytes: int = 52_428_800
    upload_timeout_seconds: int = 7_200
    caption_template: str = "{title}\n\n{url}\n\n#{tag}"


@dataclass(frozen=True)
class TwitchConfig:
    client_id: str = ""
    access_token: str = ""
    client_secret: str = ""
    api_base: str = "https://api.twitch.tv/helix"
    oauth_base: str = "https://id.twitch.tv/oauth2"
    request_timeout_seconds: int = 30
    max_pages_per_poll: int = 3
    recording_mode: str = "vod"
    live_poll_interval_seconds: int = 30
    live_retry_seconds: int = 15
    live_worker_count: int = 1
    live_download_timeout_seconds: int = 0


@dataclass(frozen=True)
class ControlConfig:
    enabled: bool = False
    poll_interval_seconds: int = 10
    panel_idle_timeout_seconds: int = 3600
    allow_disk_delete: bool = False
    delete_webhook_on_startup: bool = True
    default_routes: list[str] = field(default_factory=lambda: ["live"])
    allowed_user_ids: list[str] = field(default_factory=list)
    allowed_chat_ids: list[str] = field(default_factory=list)
    allowed_message_thread_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DevYoutubeMembershipConfig:
    enabled: bool = False
    notify: bool = False
    origin_ids: list[str] = field(default_factory=list)
    yt_dlp: str = "yt-dlp"
    poll_interval_seconds: int = 1800
    request_timeout_seconds: int = 180
    request_spacing_seconds: float = 5.0
    tab_limit: int = 30
    chat_id: str = ""


@dataclass(frozen=True)
class DevConfig:
    youtube_membership: DevYoutubeMembershipConfig = field(
        default_factory=DevYoutubeMembershipConfig
    )


@dataclass(frozen=True)
class Config:
    path: Path
    rsshub: RsshubConfig
    channels: list[ChannelConfig]
    app: AppConfig
    feeds: list[FeedConfig]
    download: DownloadConfig
    telegram: TelegramConfig
    control: ControlConfig
    origins: list[Origin] = field(default_factory=list)
    twitch: TwitchConfig = field(default_factory=TwitchConfig)
    dev: DevConfig = field(default_factory=DevConfig)

    @property
    def db_path(self) -> Path:
        return self.app.data_dir / "state.db"

    @property
    def download_dir(self) -> Path:
        return self.app.data_dir / "downloads"

    @property
    def archive_file(self) -> Path:
        value = Path(self.download.archive_file).expanduser()
        if value.is_absolute():
            return value
        return self.app.data_dir / value


def load_config(path: str | Path) -> Config:
    config_path = Path(path).expanduser()
    with config_path.open("rb") as fh:
        raw = tomllib.load(fh)

    app_raw = raw.get("app", {})
    data_dir = Path(app_raw.get("data_dir", "~/.local/share/asmr-tg-backup")).expanduser()
    app = AppConfig(
        data_dir=data_dir,
        poll_interval_seconds=int(app_raw.get("poll_interval_seconds", 1800)),
        download_delay_seconds=int(app_raw.get("download_delay_seconds", 300)),
        max_items_per_poll=int(app_raw.get("max_items_per_poll", 3)),
        max_attempts=int(app_raw.get("max_attempts", 5)),
        retry_seconds=int(app_raw.get("retry_seconds", 1800)),
        live_retry_seconds=int(app_raw.get("live_retry_seconds", 900)),
        worker_count=max(1, int(app_raw.get("worker_count", 1))),
        worker_poll_interval_seconds=max(1, int(app_raw.get("worker_poll_interval_seconds", 2))),
        job_lease_seconds=max(30, int(app_raw.get("job_lease_seconds", 900))),
        log_level=str(app_raw.get("log_level", "INFO")),
    )

    rsshub_raw = raw.get("rsshub", {})
    rsshub = RsshubConfig(base_url=str(rsshub_raw.get("base_url", "https://rss.dreaife.tokyo")).rstrip("/"))

    channels = [
        ChannelConfig(
            id=str(item["id"]),
            name=str(item.get("name") or item["id"]),
            channel_id=str(item["channel_id"]),
            enabled=_strict_bool(
                item.get("enabled", True),
                label=f"channel {item['id']!r} enabled",
            ),
            routes=[str(route).strip("/") for route in item.get("routes", ["channel"])],
        )
        for item in raw.get("channels", [])
    ]

    raw_feeds = [
        FeedConfig(
            id=str(item["id"]),
            name=str(item.get("name") or item["id"]),
            url=str(item["url"]),
            enabled=_strict_bool(
                item.get("enabled", True),
                label=f"feed {item['id']!r} enabled",
            ),
        )
        for item in raw.get("feeds", [])
    ]
    feeds = list(raw_feeds)
    feeds.extend(_expand_channel_feeds(rsshub, channels))

    download_raw = raw.get("download", {})
    provider_profiles = _load_download_profiles(download_raw.get("provider_profiles", {}))
    download = DownloadConfig(
        yt_dlp=str(download_raw.get("yt_dlp", "yt-dlp")),
        ffmpeg=str(download_raw.get("ffmpeg", "ffmpeg")),
        format=str(download_raw.get("format", DownloadConfig.format)),
        merge_output_format=str(download_raw.get("merge_output_format", "")),
        extract_audio=bool(download_raw.get("extract_audio", True)),
        audio_format=str(download_raw.get("audio_format", "m4a")),
        audio_quality=str(download_raw.get("audio_quality", "0")),
        output_template=str(download_raw.get("output_template", DownloadConfig.output_template)),
        archive_file=str(download_raw.get("archive_file", "download-archive.txt")),
        restrict_filenames=bool(download_raw.get("restrict_filenames", False)),
        write_info_json=bool(download_raw.get("write_info_json", True)),
        write_thumbnail=bool(download_raw.get("write_thumbnail", True)),
        probe_timeout_seconds=max(1, int(download_raw.get("probe_timeout_seconds", 180))),
        download_timeout_seconds=max(1, int(download_raw.get("download_timeout_seconds", 21_600))),
        ffmpeg_timeout_seconds=max(1, int(download_raw.get("ffmpeg_timeout_seconds", 7_200))),
        extra_args=[str(arg) for arg in download_raw.get("extra_args", [])],
        provider_profiles=provider_profiles,
    )

    telegram_raw = raw.get("telegram", {})
    telegram = TelegramConfig(
        enabled=bool(telegram_raw.get("enabled", False)),
        bot_token=str(telegram_raw.get("bot_token", "")),
        chat_id=str(telegram_raw.get("chat_id", "")),
        api_base=str(telegram_raw.get("api_base", "https://api.telegram.org")),
        media_type=str(telegram_raw.get("media_type", "audio")),
        send_as_document=bool(telegram_raw.get("send_as_document", False)),
        max_upload_bytes=int(telegram_raw.get("max_upload_bytes", 52_428_800)),
        upload_timeout_seconds=max(1, int(telegram_raw.get("upload_timeout_seconds", 7_200))),
        caption_template=str(telegram_raw.get("caption_template", TelegramConfig.caption_template)),
    )
    if telegram.media_type not in {"audio", "document", "video"}:
        raise ValueError("telegram.media_type must be 'audio', 'document', or 'video'")
    if telegram.max_upload_bytes <= 0:
        raise ValueError("telegram.max_upload_bytes must be positive")

    twitch_raw = raw.get("twitch", {})
    client_id_env = str(twitch_raw.get("client_id_env", "TWITCH_CLIENT_ID"))
    access_token_env = str(twitch_raw.get("access_token_env", "TWITCH_ACCESS_TOKEN"))
    client_secret_env = str(twitch_raw.get("client_secret_env", "TWITCH_CLIENT_SECRET"))
    recording_mode = _twitch_recording_mode(
        twitch_raw.get("recording_mode", TwitchConfig.recording_mode),
        label="twitch.recording_mode",
    )
    twitch = TwitchConfig(
        client_id=str(twitch_raw.get("client_id") or os.environ.get(client_id_env, "")),
        access_token=str(twitch_raw.get("access_token") or os.environ.get(access_token_env, "")),
        client_secret=str(twitch_raw.get("client_secret") or os.environ.get(client_secret_env, "")),
        api_base=str(twitch_raw.get("api_base", "https://api.twitch.tv/helix")).rstrip("/"),
        oauth_base=str(twitch_raw.get("oauth_base", "https://id.twitch.tv/oauth2")).rstrip("/"),
        request_timeout_seconds=max(1, int(twitch_raw.get("request_timeout_seconds", 30))),
        max_pages_per_poll=max(1, int(twitch_raw.get("max_pages_per_poll", 3))),
        recording_mode=recording_mode,
        live_poll_interval_seconds=max(5, int(twitch_raw.get("live_poll_interval_seconds", 30))),
        live_retry_seconds=max(1, int(twitch_raw.get("live_retry_seconds", 15))),
        live_worker_count=max(1, int(twitch_raw.get("live_worker_count", 1))),
        live_download_timeout_seconds=max(
            0,
            int(twitch_raw.get("live_download_timeout_seconds", 0)),
        ),
    )

    control_raw = raw.get("control", {})
    control = ControlConfig(
        enabled=bool(control_raw.get("enabled", False)),
        poll_interval_seconds=max(1, min(30, int(control_raw.get("poll_interval_seconds", 10)))),
        panel_idle_timeout_seconds=max(
            0,
            int(control_raw.get("panel_idle_timeout_seconds", 3600)),
        ),
        allow_disk_delete=_strict_bool(
            control_raw.get("allow_disk_delete", False),
            label="control.allow_disk_delete",
        ),
        delete_webhook_on_startup=bool(control_raw.get("delete_webhook_on_startup", True)),
        default_routes=[str(route).strip("/") for route in control_raw.get("default_routes", ["live"])],
        allowed_user_ids=[str(item) for item in control_raw.get("allowed_user_ids", [])],
        allowed_chat_ids=[str(item) for item in control_raw.get("allowed_chat_ids", [])],
        allowed_message_thread_ids=[str(item) for item in control_raw.get("allowed_message_thread_ids", [])],
    )

    origins = _load_origins(raw.get("origins", []), channels, raw_feeds)
    dev = _load_dev_config(raw.get("dev", {}), origins, telegram)
    if dev.youtube_membership.enabled:
        ambiguous_auth_sections = sorted(
            str(key)
            for key in raw
            if "auth" in str(key).lower() or "cookie" in str(key).lower()
        )
        if ambiguous_auth_sections:
            raise ValueError(
                "dev.youtube_membership is anonymous-only; unsupported top-level "
                "authentication/cookie section(s): "
                + ", ".join(ambiguous_auth_sections)
            )

    return Config(
        path=config_path,
        rsshub=rsshub,
        channels=channels,
        app=app,
        feeds=feeds,
        download=download,
        telegram=telegram,
        control=control,
        origins=origins,
        twitch=twitch,
        dev=dev,
    )


def _load_origins(
    raw_origins: list[dict[str, Any]],
    channels: list[ChannelConfig],
    raw_feeds: list[FeedConfig],
) -> list[Origin]:
    origins: list[Origin] = []
    seen_ids: set[str] = set()

    for item in raw_origins:
        origin_id = str(item["id"])
        if origin_id in seen_ids:
            raise ValueError(f"duplicate origin id: {origin_id}")
        provider = str(item["provider"]).lower().strip()
        kind = str(item.get("kind") or _default_origin_kind(provider)).lower().strip()
        external_id = str(item.get("external_id") or item.get("url") or "").strip()
        if not provider or not external_id:
            raise ValueError(f"origin {origin_id!r} requires provider and external_id")
        bootstrap = str(item.get("bootstrap", "latest")).lower().strip()
        if bootstrap not in {"latest", "all"}:
            raise ValueError(f"origin {origin_id!r} bootstrap must be 'latest' or 'all'")
        options = {
            str(key): value
            for key, value in item.items()
            if key not in {"id", "provider", "kind", "name", "external_id", "url", "enabled", "bootstrap", "credential_ref"}
        }
        if provider == "twitch" and "recording_mode" in options:
            if kind != "vods":
                raise ValueError(
                    f"origin {origin_id!r} recording_mode is only valid for "
                    "Twitch kind='vods'"
                )
            options["recording_mode"] = _twitch_recording_mode(
                options["recording_mode"],
                label=f"origin {origin_id!r} recording_mode",
            )
        origins.append(
            Origin(
                id=origin_id,
                provider=provider,
                kind=kind,
                name=str(item.get("name") or origin_id),
                external_id=external_id,
                enabled=_strict_bool(
                    item.get("enabled", True),
                    label=f"origin {origin_id!r} enabled",
                ),
                bootstrap=bootstrap,
                credential_ref=str(item["credential_ref"]) if item.get("credential_ref") else None,
                options=options,
            )
        )
        seen_ids.add(origin_id)

    for channel in channels:
        if channel.id in seen_ids:
            continue
        origins.append(
            Origin(
                id=channel.id,
                provider="youtube",
                kind="uploads",
                name=channel.name,
                external_id=channel.channel_id,
                enabled=channel.enabled,
                options={"routes": list(channel.routes)},
            )
        )
        seen_ids.add(channel.id)

    for feed in raw_feeds:
        if feed.id in seen_ids:
            continue
        origins.append(
            Origin(
                id=feed.id,
                provider="rss",
                kind="feed",
                name=feed.name,
                external_id=feed.url,
                enabled=feed.enabled,
            )
        )
        seen_ids.add(feed.id)

    return origins


def _default_origin_kind(provider: str) -> str:
    if provider == "youtube":
        return "uploads"
    if provider == "twitch":
        return "vods"
    return "feed"


def _twitch_recording_mode(value: object, *, label: str) -> str:
    mode = str(value).lower().strip()
    if mode not in {"vod", "live"}:
        raise ValueError(f"{label} must be 'vod' or 'live'")
    return mode


def _strict_bool(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be true or false")
    return value


def _load_dev_config(
    raw_dev: object,
    origins: list[Origin],
    telegram: TelegramConfig,
) -> DevConfig:
    if not isinstance(raw_dev, dict):
        raise ValueError("dev must be a table")
    _reject_unknown_keys(raw_dev, {"youtube_membership"}, label="dev")

    raw_membership = raw_dev.get("youtube_membership", {})
    if not isinstance(raw_membership, dict):
        raise ValueError("dev.youtube_membership must be a table")
    _reject_unknown_keys(
        raw_membership,
        {
            "enabled",
            "notify",
            "origin_ids",
            "yt_dlp",
            "poll_interval_seconds",
            "request_timeout_seconds",
            "request_spacing_seconds",
            "tab_limit",
            "chat_id",
        },
        label="dev.youtube_membership",
    )

    origin_ids = _strict_string_list(
        raw_membership.get("origin_ids", []),
        label="dev.youtube_membership.origin_ids",
    )
    enabled = _strict_bool(
        raw_membership.get("enabled", False),
        label="dev.youtube_membership.enabled",
    )
    notify = _strict_bool(
        raw_membership.get("notify", False),
        label="dev.youtube_membership.notify",
    )
    membership = DevYoutubeMembershipConfig(
        enabled=enabled,
        notify=notify,
        origin_ids=origin_ids,
        yt_dlp=str(raw_membership.get("yt_dlp", "yt-dlp")).strip(),
        poll_interval_seconds=max(
            300,
            int(raw_membership.get("poll_interval_seconds", 1800)),
        ),
        request_timeout_seconds=max(
            30,
            int(raw_membership.get("request_timeout_seconds", 180)),
        ),
        request_spacing_seconds=max(
            0.0,
            float(raw_membership.get("request_spacing_seconds", 5.0)),
        ),
        tab_limit=max(1, min(100, int(raw_membership.get("tab_limit", 30)))),
        chat_id=str(raw_membership.get("chat_id", "")).strip(),
    )

    if not membership.yt_dlp:
        raise ValueError("dev.youtube_membership.yt_dlp must not be empty")

    if membership.notify and not membership.enabled:
        raise ValueError(
            "dev.youtube_membership.notify=true requires enabled=true"
        )
    if membership.enabled:
        if not membership.origin_ids:
            raise ValueError(
                "dev.youtube_membership.enabled=true requires at least one origin_id"
            )
        origins_by_id = {origin.id: origin for origin in origins}
        selected_channel_ids: dict[str, str] = {}
        for origin_id in membership.origin_ids:
            origin = origins_by_id.get(origin_id)
            if origin is None:
                raise ValueError(
                    "dev.youtube_membership origin_id does not exist: "
                    f"{origin_id}"
                )
            if (
                origin.enabled
                or origin.provider != "youtube"
                or origin.kind not in {"uploads", "vod_after_live"}
            ):
                raise ValueError(
                    "dev.youtube_membership origin_id must reference a disabled "
                    "dev-only YouTube uploads or vod_after_live origin: "
                    f"{origin_id}"
                )
            if not is_channel_id(origin.external_id):
                raise ValueError(
                    "dev.youtube_membership origin_id must use a resolved UC "
                    f"channel ID: {origin_id}"
                )
            unsupported_options = sorted(
                key for key in origin.options if key != "routes"
            )
            if origin.credential_ref or unsupported_options:
                details = [*unsupported_options]
                if origin.credential_ref:
                    details.append("credential_ref")
                raise ValueError(
                    "dev.youtube_membership selected origins are anonymous-only; "
                    f"unsupported option(s) on {origin_id}: "
                    + ", ".join(sorted(details))
                )
            previous_origin_id = selected_channel_ids.get(origin.external_id)
            if previous_origin_id:
                raise ValueError(
                    "dev.youtube_membership selected origins must reference "
                    "unique YouTube channels: "
                    f"{previous_origin_id}, {origin_id}"
                )
            selected_channel_ids[origin.external_id] = origin_id

        production_conflicts = sorted(
            origin.id
            for origin in origins
            if origin.enabled
            and origin.provider == "youtube"
            and origin.external_id in selected_channel_ids
        )
        if production_conflicts:
            raise ValueError(
                "dev.youtube_membership channels must not also appear in enabled "
                "production YouTube origins: "
                + ", ".join(production_conflicts)
            )
    if membership.notify:
        if not telegram.bot_token.strip():
            raise ValueError(
                "dev.youtube_membership.notify=true requires telegram.bot_token"
            )
        if not (membership.chat_id or telegram.chat_id.strip()):
            raise ValueError(
                "dev.youtube_membership.notify=true requires "
                "dev.youtube_membership.chat_id or telegram.chat_id"
            )

    return DevConfig(youtube_membership=membership)


def _reject_unknown_keys(
    raw: dict[str, Any],
    allowed: set[str],
    *,
    label: str,
) -> None:
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise ValueError(f"unsupported {label} option(s): {', '.join(unknown)}")


def _strict_string_list(value: object, *, label: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{label} must be an array of non-empty strings")
    result = [item.strip() for item in value]
    if len(result) != len(set(result)):
        raise ValueError(f"{label} must not contain duplicate values")
    return result


def _load_download_profiles(raw_profiles: dict[str, Any]) -> dict[str, DownloadProfile]:
    defaults = {
        "twitch": DownloadProfile(
            format="bestaudio/best",
            merge_output_format="",
            extract_audio=True,
            audio_format="m4a",
            audio_quality="0",
        )
    }
    profiles = dict(defaults)
    for raw_name, item in raw_profiles.items():
        if not isinstance(item, dict):
            raise ValueError(f"download.provider_profiles.{raw_name} must be a table")
        name = str(raw_name).lower().strip()
        if not name:
            raise ValueError("download provider profile name must not be empty")
        base = defaults.get(name, DownloadProfile())
        profiles[name] = DownloadProfile(
            format=str(item["format"]) if "format" in item else base.format,
            merge_output_format=(
                str(item["merge_output_format"])
                if "merge_output_format" in item
                else base.merge_output_format
            ),
            extract_audio=bool(item["extract_audio"]) if "extract_audio" in item else base.extract_audio,
            audio_format=str(item["audio_format"]) if "audio_format" in item else base.audio_format,
            audio_quality=str(item["audio_quality"]) if "audio_quality" in item else base.audio_quality,
            extra_args=(
                [str(arg) for arg in item.get("extra_args", [])]
                if "extra_args" in item
                else base.extra_args
            ),
        )
    return profiles


def expand_channel_feeds(rsshub: RsshubConfig, channels: list[ChannelConfig], prefix: str = "") -> list[FeedConfig]:
    feeds: list[FeedConfig] = []
    for channel in channels:
        routes = [route.strip("/") for route in channel.routes if route.strip("/")]
        route_label = f" ({','.join(routes)})" if routes else ""
        feeds.append(
            FeedConfig(
                id=f"{prefix}{channel.id}",
                name=f"{channel.name}{route_label}",
                url=youtube_channel_feed_url(channel.channel_id),
                enabled=channel.enabled,
            )
        )
    return feeds


def _expand_channel_feeds(rsshub: RsshubConfig, channels: list[ChannelConfig]) -> list[FeedConfig]:
    return expand_channel_feeds(rsshub, channels)
