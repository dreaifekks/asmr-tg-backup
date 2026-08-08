from __future__ import annotations

import os
from typing import Any, Callable
from urllib.request import ProxyHandler, build_opener, urlopen


UrlOpener = Callable[..., Any]


def build_url_opener(proxy_url: str) -> UrlOpener:
    """Return an isolated HTTP opener for source requests.

    The default urlopen behavior is preserved when no application proxy is
    configured. An explicit opener avoids mutating urllib's process-global
    opener while source and Telegram threads run concurrently.
    """
    if not proxy_url:
        return urlopen
    return build_opener(
        ProxyHandler({"http": proxy_url, "https": proxy_url})
    ).open


def build_proxy_env(proxy_url: str) -> dict[str, str] | None:
    """Build a child-only proxy environment without changing os.environ."""
    if not proxy_url:
        return None
    env = os.environ.copy()
    for name in (
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    ):
        env[name] = proxy_url
    return env
