"""Strict single-video URL parsing; never resolve a playlist or channel."""

import re
from urllib.parse import parse_qs, urlsplit

from .models import MediaCandidate


def split_media_url(url: str):
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValueError("视频 URL 无效") from exc
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port is not None
        or any(char.isspace() or ord(char) < 32 for char in url)
        or "\\" in url
    ):
        raise ValueError("请使用不带账号、密码或端口的 HTTPS 视频链接")
    return parts


def youtube_video(url: str) -> MediaCandidate | None:
    parts = split_media_url(url)
    host = parts.hostname
    path = parts.path.rstrip("/")
    video_id = None
    if host == "youtu.be":
        video_id = path.removeprefix("/")
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        if path == "/watch":
            ids = parse_qs(parts.query).get("v", [])
            if len(ids) == 1:
                video_id = ids[0]
        elif re.fullmatch(r"/(shorts|live|embed)/[^/]+", path):
            video_id = path.rsplit("/", 1)[1]
        elif re.fullmatch(r"/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)(/(videos|streams|shorts))?", path):
            return None
    if not video_id or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("请提供 YouTube 单视频链接，不支持播放列表或其他网站")
    return MediaCandidate(
        provider="youtube", content_kind="video", external_id=video_id,
        title=video_id, url=f"https://www.youtube.com/watch?v={video_id}",
        published_at=None,
    )


def twitch_video_id(url: str) -> str:
    parts = split_media_url(url)
    match = re.fullmatch(r"/videos/([0-9]+)/?", parts.path)
    if parts.hostname not in {"twitch.tv", "www.twitch.tv", "m.twitch.tv"} or not match:
        raise ValueError("请提供 Twitch 视频链接：https://www.twitch.tv/videos/123456；不支持频道或 Clips 链接")
    return match[1]
