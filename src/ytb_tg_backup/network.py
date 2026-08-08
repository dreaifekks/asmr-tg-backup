from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address, ip_address
from urllib.parse import urlsplit


_IPV6_LOOPBACK = IPv6Address("::1")


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
