from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tomllib

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
    extra_args: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    api_base: str = "https://api.telegram.org"
    media_type: str = "audio"
    send_as_document: bool = False
    max_upload_bytes: int = 52_428_800
    caption_template: str = "{title}\n\n#{tag}"


@dataclass(frozen=True)
class ControlConfig:
    enabled: bool = False
    poll_interval_seconds: int = 10
    delete_webhook_on_startup: bool = True
    default_routes: list[str] = field(default_factory=lambda: ["live"])
    allowed_user_ids: list[str] = field(default_factory=list)
    allowed_chat_ids: list[str] = field(default_factory=list)
    allowed_message_thread_ids: list[str] = field(default_factory=list)


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
    data_dir = Path(app_raw.get("data_dir", "~/.local/share/ytb-tg-backup")).expanduser()
    app = AppConfig(
        data_dir=data_dir,
        poll_interval_seconds=int(app_raw.get("poll_interval_seconds", 1800)),
        download_delay_seconds=int(app_raw.get("download_delay_seconds", 300)),
        max_items_per_poll=int(app_raw.get("max_items_per_poll", 3)),
        max_attempts=int(app_raw.get("max_attempts", 5)),
        retry_seconds=int(app_raw.get("retry_seconds", 1800)),
        live_retry_seconds=int(app_raw.get("live_retry_seconds", 900)),
        log_level=str(app_raw.get("log_level", "INFO")),
    )

    rsshub_raw = raw.get("rsshub", {})
    rsshub = RsshubConfig(base_url=str(rsshub_raw.get("base_url", "https://rss.dreaife.tokyo")).rstrip("/"))

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

    feeds = [
        FeedConfig(
            id=str(item["id"]),
            name=str(item.get("name") or item["id"]),
            url=str(item["url"]),
            enabled=bool(item.get("enabled", True)),
        )
        for item in raw.get("feeds", [])
    ]
    feeds.extend(_expand_channel_feeds(rsshub, channels))

    download_raw = raw.get("download", {})
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
        extra_args=[str(arg) for arg in download_raw.get("extra_args", [])],
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
        caption_template=str(telegram_raw.get("caption_template", TelegramConfig.caption_template)),
    )

    control_raw = raw.get("control", {})
    control = ControlConfig(
        enabled=bool(control_raw.get("enabled", False)),
        poll_interval_seconds=int(control_raw.get("poll_interval_seconds", 10)),
        delete_webhook_on_startup=bool(control_raw.get("delete_webhook_on_startup", True)),
        default_routes=[str(route).strip("/") for route in control_raw.get("default_routes", ["live"])],
        allowed_user_ids=[str(item) for item in control_raw.get("allowed_user_ids", [])],
        allowed_chat_ids=[str(item) for item in control_raw.get("allowed_chat_ids", [])],
        allowed_message_thread_ids=[str(item) for item in control_raw.get("allowed_message_thread_ids", [])],
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
    )


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
