from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import tomllib
from typing import Any
from urllib.parse import urlsplit

from .extension_state import load_managed_extension_state, merge_extension_tables
from .models import Origin
from .youtube import youtube_channel_feed_url


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
    base_url: str = ""


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
class StorageConfig:
    process_retention_hours: int = 0
    backup_retention_hours: int = 0
    archive_dir: Path | None = None
    archive_after_delivery_hours: int = 24
    archive_require_mount: bool = True


@dataclass(frozen=True)
class MtprotoConfig:
    api_id: int | None = None
    api_hash: str = ""
    session_path: Path = Path("telegram-mtproto.session")
    max_upload_bytes: int = 1_990_000_000


@dataclass(frozen=True)
class BotApiConfig:
    api_base: str = "https://api.telegram.org"
    max_upload_bytes: int = 49_000_000
    split_large_audio: bool = True
    max_upload_parts: int = 10


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    upload_transport: str = "mtproto"
    media_type: str = "audio"
    send_as_document: bool = False
    upload_timeout_seconds: int = 7_200
    caption_template: str = "{title}\n\n{url}\n\n#{tag}"
    mtproto: MtprotoConfig = field(default_factory=MtprotoConfig)
    bot_api: BotApiConfig = field(default_factory=BotApiConfig)

    # Transitional read-only views for the control bot and delivery planner.
    # New transport code should use ``telegram.mtproto`` or ``telegram.bot_api``.
    @property
    def api_base(self) -> str:
        return self.bot_api.api_base

    @property
    def max_upload_bytes(self) -> int:
        if self.upload_transport == "mtproto":
            return self.mtproto.max_upload_bytes
        return self.bot_api.max_upload_bytes

    @property
    def split_large_audio(self) -> bool:
        return self.upload_transport == "bot_api" and self.bot_api.split_large_audio

    @property
    def max_upload_parts(self) -> int:
        return self.bot_api.max_upload_parts


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
class LiveConfig:
    poll_interval_seconds: int = 30
    retry_seconds: int = 15
    worker_count: int = 1
    download_timeout_seconds: int = 0


@dataclass(frozen=True)
class ControlConfig:
    enabled: bool = False
    api_base: str = ""
    poll_interval_seconds: int = 10
    panel_idle_timeout_seconds: int = 3600
    allow_disk_delete: bool = False
    delete_webhook_on_startup: bool = True
    default_routes: list[str] = field(default_factory=lambda: ["live"])
    allowed_user_ids: list[str] = field(default_factory=list)
    allowed_chat_ids: list[str] = field(default_factory=list)
    allowed_message_thread_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SourcesConfig:
    path: Path


@dataclass(frozen=True)
class ExtensionSettings:
    id: str
    required: bool = True
    config_file: Path | None = None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtensionsConfig:
    enabled: tuple[str, ...] = ()
    settings: dict[str, ExtensionSettings] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    path: Path
    rsshub: RsshubConfig
    channels: list[ChannelConfig]
    app: AppConfig
    feeds: list[FeedConfig]
    download: DownloadConfig
    storage: StorageConfig
    telegram: TelegramConfig
    control: ControlConfig
    sources: SourcesConfig
    extensions: ExtensionsConfig = field(default_factory=ExtensionsConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    # Legacy source declarations are read only for the one-time sources.toml
    # migration. Runtime source management uses ``sources.path``.
    origins: list[Origin] = field(default_factory=list)
    legacy_sources_declared: bool = False
    twitch: TwitchConfig = field(default_factory=TwitchConfig)

    @property
    def db_path(self) -> Path:
        return self.app.data_dir / "state.db"

    @property
    def download_dir(self) -> Path:
        return self.app.data_dir / "downloads"

    @property
    def managed_storage_roots(self) -> tuple[Path, ...]:
        roots = [self.download_dir]
        if self.storage.archive_dir is not None:
            roots.append(self.storage.archive_dir)
        return tuple(roots)

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
    data_dir_env = str(app_raw.get("data_dir_env", "ASMR_TG_BACKUP_DATA_DIR"))
    data_dir = Path(
        os.environ.get(data_dir_env)
        or app_raw.get("data_dir", "~/.local/share/asmr-tg-backup")
    ).expanduser()
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
    rsshub = RsshubConfig(base_url=str(rsshub_raw.get("base_url", "")).rstrip("/"))

    channels = [
        ChannelConfig(
            id=str(item["id"]),
            name=str(item.get("name") or item["id"]),
            channel_id=str(item["channel_id"]),
            enabled=bool(item.get("enabled", True)),
            routes=[str(route).strip("/") for route in item.get("routes", ["channel"])],
        )
        for item in raw.get("channels", [])
    ]

    raw_feeds = [
        FeedConfig(
            id=str(item["id"]),
            name=str(item.get("name") or item["id"]),
            url=str(item["url"]),
            enabled=bool(item.get("enabled", True)),
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
    mtproto_raw = telegram_raw.get("mtproto", {})
    bot_api_raw = telegram_raw.get("bot_api", {})
    if not isinstance(mtproto_raw, dict):
        raise ValueError("telegram.mtproto must be a table")
    if not isinstance(bot_api_raw, dict):
        raise ValueError("telegram.bot_api must be a table")

    bot_token_env = str(telegram_raw.get("bot_token_env", "TELEGRAM_BOT_TOKEN"))
    chat_id_env = str(telegram_raw.get("chat_id_env", "TELEGRAM_CHAT_ID"))
    upload_transport = str(
        os.environ.get("ASMR_TG_UPLOAD_TRANSPORT")
        or telegram_raw.get("upload_transport", "mtproto")
    ).lower().strip()
    api_base_env = str(bot_api_raw.get("api_base_env", "TELEGRAM_API_BASE"))
    max_upload_bytes_env = str(
        bot_api_raw.get("max_upload_bytes_env", "TELEGRAM_MAX_UPLOAD_BYTES")
    )
    api_id, api_hash = resolve_mtproto_credentials(mtproto_raw)
    session_value = str(mtproto_raw.get("session_path", "")).strip()
    session_path = (
        Path(session_value).expanduser()
        if session_value
        else Path("telegram-mtproto.session")
    )
    if not session_path.is_absolute():
        session_path = app.data_dir / session_path

    telegram = TelegramConfig(
        enabled=bool(telegram_raw.get("enabled", False)),
        bot_token=str(
            os.environ.get(bot_token_env) or telegram_raw.get("bot_token", "")
        ),
        chat_id=str(os.environ.get(chat_id_env) or telegram_raw.get("chat_id", "")),
        upload_transport=upload_transport,
        media_type=str(telegram_raw.get("media_type", "audio")),
        send_as_document=bool(telegram_raw.get("send_as_document", False)),
        upload_timeout_seconds=max(1, int(telegram_raw.get("upload_timeout_seconds", 7_200))),
        caption_template=str(telegram_raw.get("caption_template", TelegramConfig.caption_template)),
        mtproto=MtprotoConfig(
            api_id=api_id,
            api_hash=api_hash,
            session_path=session_path,
            max_upload_bytes=int(
                mtproto_raw.get("max_upload_bytes", MtprotoConfig.max_upload_bytes)
            ),
        ),
        bot_api=BotApiConfig(
            api_base=str(
                os.environ.get(api_base_env)
                or bot_api_raw.get("api_base", BotApiConfig.api_base)
            ),
            max_upload_bytes=int(
                os.environ.get(max_upload_bytes_env)
                or bot_api_raw.get("max_upload_bytes", BotApiConfig.max_upload_bytes)
            ),
            split_large_audio=_strict_bool(
                bot_api_raw.get("split_large_audio", True),
                label="telegram.bot_api.split_large_audio",
            ),
            max_upload_parts=int(bot_api_raw.get("max_upload_parts", 10)),
        ),
    )
    if telegram.upload_transport not in {"mtproto", "bot_api"}:
        raise ValueError("telegram.upload_transport must be 'mtproto' or 'bot_api'")
    if telegram.media_type not in {"audio", "document", "video"}:
        raise ValueError("telegram.media_type must be 'audio', 'document', or 'video'")
    if telegram.mtproto.max_upload_bytes <= 0:
        raise ValueError("telegram.mtproto.max_upload_bytes must be positive")
    if telegram.bot_api.max_upload_bytes <= 0:
        raise ValueError("telegram.bot_api.max_upload_bytes must be positive")
    if not 1 <= telegram.bot_api.max_upload_parts <= 10:
        raise ValueError("telegram.bot_api.max_upload_parts must be between 1 and 10")

    storage_raw = raw.get("storage", {})
    if not isinstance(storage_raw, dict):
        raise ValueError("storage must be a table")
    archive_dir = _optional_archive_dir(
        storage_raw.get("archive_dir", ""),
        label="storage.archive_dir",
    )
    storage = StorageConfig(
        process_retention_hours=_strict_non_negative_int(
            storage_raw.get("process_retention_hours", 0),
            label="storage.process_retention_hours",
        ),
        backup_retention_hours=_strict_non_negative_int(
            storage_raw.get("backup_retention_hours", 0),
            label="storage.backup_retention_hours",
        ),
        archive_dir=archive_dir,
        archive_after_delivery_hours=_strict_non_negative_int(
            storage_raw.get("archive_after_delivery_hours", 24),
            label="storage.archive_after_delivery_hours",
        ),
        archive_require_mount=_strict_bool(
            storage_raw.get("archive_require_mount", True),
            label="storage.archive_require_mount",
        ),
    )
    if archive_dir is not None:
        download_root = (app.data_dir / "downloads").resolve(strict=False)
        archive_root = archive_dir.resolve(strict=False)
        if (
            archive_root == download_root
            or archive_root in download_root.parents
            or download_root in archive_root.parents
        ):
            raise ValueError(
                "storage.archive_dir must be separate from the downloads directory"
            )

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

    live_raw = raw.get("live", {})
    if not isinstance(live_raw, dict):
        raise ValueError("live must be a table")
    live = LiveConfig(
        poll_interval_seconds=max(
            5,
            int(
                live_raw.get(
                    "poll_interval_seconds",
                    twitch.live_poll_interval_seconds,
                )
            ),
        ),
        retry_seconds=max(
            1,
            int(live_raw.get("retry_seconds", twitch.live_retry_seconds)),
        ),
        worker_count=max(
            1,
            int(live_raw.get("worker_count", twitch.live_worker_count)),
        ),
        download_timeout_seconds=max(
            0,
            int(
                live_raw.get(
                    "download_timeout_seconds",
                    twitch.live_download_timeout_seconds,
                )
            ),
        ),
    )

    control_raw = raw.get("control", {})
    control = ControlConfig(
        enabled=bool(control_raw.get("enabled", False)),
        api_base=_optional_api_base(
            control_raw.get("api_base", ""),
            label="control.api_base",
        ),
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

    sources_raw = raw.get("sources", {})
    if not isinstance(sources_raw, dict):
        raise ValueError("sources must be a table")
    sources_path_value = str(
        os.environ.get("ASMR_TG_BACKUP_SOURCES_PATH")
        or sources_raw.get("path")
        or "sources.toml"
    ).strip()
    if not sources_path_value:
        raise ValueError("sources.path must not be empty")
    sources_path = Path(sources_path_value).expanduser()
    if not sources_path.is_absolute():
        sources_path = config_path.parent / sources_path
    sources = SourcesConfig(path=sources_path)

    managed_extensions = load_managed_extension_state(config_path)
    extensions = _load_extensions(
        merge_extension_tables(raw.get("extensions", {}), managed_extensions),
        config_path.parent,
    )

    origins = _load_origins(raw.get("origins", []), channels, raw_feeds)
    legacy_sources_declared = any(
        key in raw for key in ("origins", "channels", "feeds")
    )

    return Config(
        path=config_path,
        rsshub=rsshub,
        channels=channels,
        app=app,
        feeds=feeds,
        download=download,
        storage=storage,
        telegram=telegram,
        control=control,
        sources=sources,
        extensions=extensions,
        live=live,
        origins=origins,
        legacy_sources_declared=legacy_sources_declared,
        twitch=twitch,
    )


_EXTENSION_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _load_extensions(raw: object, config_dir: Path) -> ExtensionsConfig:
    if raw is None:
        return ExtensionsConfig()
    if not isinstance(raw, dict):
        raise ValueError("extensions must be a table")
    enabled_raw = raw.get("enabled", [])
    if not isinstance(enabled_raw, list) or not all(
        isinstance(item, str) for item in enabled_raw
    ):
        raise ValueError("extensions.enabled must be an array of extension ids")
    enabled: list[str] = []
    seen: set[str] = set()
    for item in enabled_raw:
        extension_id = item.strip().lower()
        if not _EXTENSION_ID.fullmatch(extension_id):
            raise ValueError(f"invalid extension id: {item!r}")
        if extension_id in seen:
            raise ValueError(f"duplicate extension id: {extension_id}")
        seen.add(extension_id)
        enabled.append(extension_id)

    settings: dict[str, ExtensionSettings] = {}
    for key, value in raw.items():
        if key == "enabled":
            continue
        extension_id = str(key).strip().lower()
        if not _EXTENSION_ID.fullmatch(extension_id):
            raise ValueError(f"invalid extension settings id: {key!r}")
        if not isinstance(value, dict):
            raise ValueError(f"extensions.{key} must be a table")
        config_file: Path | None = None
        if value.get("config_file") is not None:
            config_value = str(value["config_file"]).strip()
            if not config_value:
                raise ValueError(f"extensions.{key}.config_file must not be empty")
            config_file = Path(config_value).expanduser()
            if not config_file.is_absolute():
                config_file = config_dir / config_file
        options = {
            str(option): option_value
            for option, option_value in value.items()
            if option not in {"required", "config_file"}
        }
        settings[extension_id] = ExtensionSettings(
            id=extension_id,
            required=_strict_bool(
                value.get("required", True),
                label=f"extensions.{key}.required",
            ),
            config_file=config_file,
            options=options,
        )
    for extension_id in enabled:
        settings.setdefault(extension_id, ExtensionSettings(id=extension_id))
    return ExtensionsConfig(enabled=tuple(enabled), settings=settings)


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
                enabled=bool(item.get("enabled", True)),
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


def _strict_non_negative_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _optional_archive_dir(value: object, *, label: str) -> Path | None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an absolute path or empty")
    normalized = value.strip()
    if not normalized:
        return None
    path = Path(normalized).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path or empty")
    return path


def _optional_api_base(value: object, *, label: str) -> str:
    normalized = str(value).strip().rstrip("/")
    if not normalized:
        return ""
    if any(character.isspace() for character in normalized):
        raise ValueError(
            f"{label} must be an http(s) URL without credentials, whitespace, query, or fragment"
        )
    try:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise ValueError(
            f"{label} must be an http(s) URL without credentials, whitespace, query, or fragment"
        ) from exc
    if not (
        parsed.scheme.lower() in {"http", "https"}
        and hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    ):
        raise ValueError(
            f"{label} must be an http(s) URL without credentials, whitespace, query, or fragment"
        )
    return normalized


def official_mtproto_credentials() -> tuple[int | None, str]:
    """Return the credential pair included only in official release artifacts."""
    try:
        from . import _official_defaults
    except ImportError:
        return None, ""
    return _parse_mtproto_credential_pair(
        getattr(_official_defaults, "MTPROTO_API_ID", None),
        getattr(_official_defaults, "MTPROTO_API_HASH", None),
        label="official MTProto defaults",
    )


def resolve_mtproto_credentials(raw: dict[str, Any]) -> tuple[int | None, str]:
    env_id = os.environ.get("ASMR_TG_MTPROTO_API_ID")
    env_hash = os.environ.get("ASMR_TG_MTPROTO_API_HASH")
    if _credential_source_is_present(env_id, env_hash):
        return _parse_mtproto_credential_pair(
            env_id,
            env_hash,
            label="ASMR_TG_MTPROTO_API_ID/ASMR_TG_MTPROTO_API_HASH",
        )

    config_id = raw.get("api_id")
    config_hash = raw.get("api_hash")
    if _credential_source_is_present(config_id, config_hash):
        return _parse_mtproto_credential_pair(
            config_id,
            config_hash,
            label="telegram.mtproto.api_id/api_hash",
        )

    return official_mtproto_credentials()


def _credential_source_is_present(api_id: object, api_hash: object) -> bool:
    return bool(str(api_id).strip() if api_id is not None else "") or bool(
        str(api_hash).strip() if api_hash is not None else ""
    )


def _parse_mtproto_credential_pair(
    api_id: object,
    api_hash: object,
    *,
    label: str,
) -> tuple[int | None, str]:
    id_value = str(api_id).strip() if api_id is not None else ""
    hash_value = str(api_hash).strip() if api_hash is not None else ""
    if bool(id_value) != bool(hash_value):
        raise ValueError(f"{label} must provide both values together")
    if not id_value:
        return None, ""
    if not id_value.isascii() or not id_value.isdecimal() or int(id_value) <= 0:
        raise ValueError(f"{label} API ID must be a positive integer")
    if len(hash_value) != 32 or not all(
        character in "0123456789abcdefABCDEF" for character in hash_value
    ):
        raise ValueError(f"{label} API hash must be a 32-character hexadecimal value")
    return int(id_value), hash_value


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
