"""Compatibility entry point for the development membership observer.

Prefer ``python -m ytb_tg_backup.dev.member_observer`` for new usage.
"""

from .dev.member_observer import *  # noqa: F401,F403
from .dev.member_observer import main


if __name__ == "__main__":
    raise SystemExit(main())
