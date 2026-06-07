from __future__ import annotations

import re
import subprocess
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen


YOUTUBE_FEED_BASE_URL = "https://www.youtube.com/feeds/videos.xml"
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{20,}$")


def is_channel_id(value: str) -> bool:
    return bool(CHANNEL_ID_RE.match(value.strip()))


def youtube_channel_feed_url(channel_id: str) -> str:
    return f"{YOUTUBE_FEED_BASE_URL}?channel_id={quote(channel_id.strip(), safe='')}"


def resolve_channel_id(channel_ref: str, yt_dlp: str) -> str:
    value = channel_ref.strip()
    if is_channel_id(value):
        return value
    if not value:
        raise ValueError("channel reference is empty")

    if value.startswith("@"):
        url = f"https://www.youtube.com/{value}"
    elif value.startswith(("https://", "http://")):
        url = value
    else:
        raise ValueError("official YouTube feed requires a UC channel_id or @handle")

    html_channel_id = _resolve_channel_id_from_html(url)
    if html_channel_id:
        return html_channel_id

    cmd = [
        yt_dlp,
        "--no-warnings",
        "--skip-download",
        "--no-playlist",
        "--print",
        "%(channel_id)s",
        url,
    ]
    try:
        completed = subprocess.run(cmd, check=True, text=True, capture_output=True, timeout=120)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise RuntimeError(f"failed to resolve YouTube channel id for {value}: {exc}") from exc

    for line in completed.stdout.splitlines():
        candidate = line.strip()
        if is_channel_id(candidate):
            return candidate
    raise RuntimeError(f"yt-dlp did not return a UC channel_id for {value}")


def _resolve_channel_id_from_html(url: str) -> str | None:
    request = Request(_quote_url(url), headers={"User-Agent": "ytb-tg-backup/0.1"})
    try:
        with urlopen(request, timeout=30) as response:
            html = response.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    patterns = (
        r"feeds/videos\.xml\?channel_id=(UC[A-Za-z0-9_-]{20,})",
        r'"channelId"\s*:\s*"(UC[A-Za-z0-9_-]{20,})"',
        r'"externalId"\s*:\s*"(UC[A-Za-z0-9_-]{20,})"',
    )
    for pattern in patterns:
        match = re.search(pattern, html)
        if match and is_channel_id(match.group(1)):
            return match.group(1)
    return None


def _quote_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            quote(parts.path, safe="/%:@"),
            quote(parts.query, safe="=&%/:?@"),
            parts.fragment,
        )
    )
