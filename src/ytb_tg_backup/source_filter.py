from __future__ import annotations

import re
from typing import Any

from .config import FeedConfig
from .feed import FeedEntry


DEFAULT_SOURCE_FILTER_PATTERN = "ASMR"
SOURCE_FILTER_STATE_KEY = "source_filter_pattern"


def compile_source_filter(pattern: str | None) -> re.Pattern[str] | None:
    if not pattern:
        return None
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"invalid source regex: {exc}") from exc


def feed_matches_source_filter(feed: FeedConfig, source_filter: re.Pattern[str] | None) -> bool:
    return text_matches_source_filter(source_filter, feed.id, feed.name)


def entry_matches_source_filter(entry: FeedEntry, source_filter: re.Pattern[str] | None) -> bool:
    return text_matches_source_filter(source_filter, entry.feed_id, entry.feed_name, entry.title)


def row_matches_source_filter(row: Any, source_filter: re.Pattern[str] | None) -> bool:
    return text_matches_source_filter(source_filter, row["feed_id"], row["feed_name"], row["title"])


def text_matches_source_filter(source_filter: re.Pattern[str] | None, *values: object) -> bool:
    if source_filter is None:
        return True
    return any(source_filter.search(str(value)) for value in values if value is not None)


def format_source_filter(pattern: str | None) -> str:
    return "off" if not pattern else f"/{pattern}/i"
