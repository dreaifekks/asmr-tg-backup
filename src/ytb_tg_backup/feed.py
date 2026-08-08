from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re
from typing import Iterable
from urllib.parse import parse_qs, quote, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from .proxy import UrlOpener


ATOM_NS = "{http://www.w3.org/2005/Atom}"
YT_NS = "{http://www.youtube.com/xml/schemas/2015}"
MAX_FEED_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class FeedEntry:
    feed_id: str
    feed_name: str
    video_id: str
    title: str
    url: str
    published_at: str | None
    updated_at: str | None


def fetch_feed(
    url: str,
    timeout: int = 30,
    *,
    opener: UrlOpener | None = None,
) -> bytes:
    request = Request(_quote_url_for_request(url), headers={"User-Agent": "asmr-tg-backup/0.1"})
    with (opener or urlopen)(request, timeout=timeout) as response:
        payload = response.read(MAX_FEED_BYTES + 1)
    if len(payload) > MAX_FEED_BYTES:
        raise ValueError(f"feed response exceeds {MAX_FEED_BYTES} bytes")
    return payload


def parse_feed(xml_bytes: bytes, feed_id: str, feed_name: str) -> list[FeedEntry]:
    root = ET.fromstring(xml_bytes)
    if _strip_ns(root.tag) == "rss":
        nodes = root.findall("./channel/item")
        parsed = [_parse_rss_item(node, feed_id, feed_name) for node in nodes]
        return [item for item in parsed if item is not None]

    entries = root.findall(f"{ATOM_NS}entry")
    if not entries and _strip_ns(root.tag) == "feed":
        entries = list(root.findall("./entry"))
    parsed = [_parse_atom_entry(node, feed_id, feed_name) for node in entries]
    return [item for item in parsed if item is not None]


def _parse_atom_entry(node: ET.Element, feed_id: str, feed_name: str) -> FeedEntry | None:
    youtube_video_id = _first_text(node, [f"{YT_NS}videoId", "videoId"])
    entry_id = _first_text(node, [f"{ATOM_NS}id", "id"])
    title = _first_text(node, [f"{ATOM_NS}title", "title"]) or "(untitled)"
    link = _atom_link(node)
    video_id = youtube_video_id or extract_video_id(entry_id or "") or extract_video_id(link or "") or entry_id or link
    if not video_id:
        return None
    url = link or (f"https://www.youtube.com/watch?v={video_id}" if youtube_video_id else "")
    if not url:
        return None
    return FeedEntry(
        feed_id=feed_id,
        feed_name=feed_name,
        video_id=video_id,
        title=title,
        url=url,
        published_at=_normalize_datetime(_first_text(node, [f"{ATOM_NS}published", "published"])),
        updated_at=_normalize_datetime(_first_text(node, [f"{ATOM_NS}updated", "updated"])),
    )


def _parse_rss_item(node: ET.Element, feed_id: str, feed_name: str) -> FeedEntry | None:
    title = _first_text(node, ["title"]) or "(untitled)"
    link = _first_text(node, ["link"]) or ""
    guid = _first_text(node, ["guid"]) or ""
    youtube_video_id = _first_text(node, [f"{YT_NS}videoId", "videoId"]) or extract_video_id(link) or extract_video_id(guid)
    video_id = youtube_video_id or guid or link
    if not video_id or not link:
        return None
    return FeedEntry(
        feed_id=feed_id,
        feed_name=feed_name,
        video_id=video_id,
        title=title,
        url=link,
        published_at=_normalize_datetime(_first_text(node, ["pubDate", "published"])),
        updated_at=_normalize_datetime(_first_text(node, ["updated"])),
    )


def extract_video_id(value: str) -> str | None:
    if not value:
        return None
    if value.startswith("yt:video:"):
        return value.rsplit(":", 1)[-1]

    parsed = urlparse(value)
    query_id = parse_qs(parsed.query).get("v", [None])[0]
    if query_id:
        return query_id

    patterns: Iterable[str] = (
        r"(?:youtu\.be/)([A-Za-z0-9_-]{6,})",
        r"(?:/shorts/)([A-Za-z0-9_-]{6,})",
        r"(?:/live/)([A-Za-z0-9_-]{6,})",
        r"(?:/embed/)([A-Za-z0-9_-]{6,})",
        r"(?:video:)([A-Za-z0-9_-]{6,})",
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    return None


def _atom_link(node: ET.Element) -> str | None:
    for link in list(node.findall(f"{ATOM_NS}link")) + list(node.findall("link")):
        href = link.attrib.get("href")
        if href and link.attrib.get("rel", "alternate") == "alternate":
            return href
    return None


def _first_text(node: ET.Element, names: list[str]) -> str | None:
    for name in names:
        found = node.find(name)
        if found is not None and found.text:
            return found.text.strip()
    return None


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _quote_url_for_request(url: str) -> str:
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


def _normalize_datetime(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    try:
        if text.endswith("Z"):
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()
