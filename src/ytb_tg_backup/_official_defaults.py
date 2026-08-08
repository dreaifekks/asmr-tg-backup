"""Defaults populated only while building official release artifacts.

Source checkouts intentionally keep these values empty. Runtime environment
variables take precedence over the values in this module.
"""

MTPROTO_API_ID: int | None = None
MTPROTO_API_HASH: str | None = None
