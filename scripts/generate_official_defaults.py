#!/usr/bin/env python3
"""Generate release-only defaults without writing their values to stdout."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re


DEFAULT_OUTPUT = Path("src/ytb_tg_backup/_official_defaults.py")
API_ID_ENV = "ASMR_TG_MTPROTO_API_ID"
API_HASH_ENV = "ASMR_TG_MTPROTO_API_HASH"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    api_id_text = os.environ.get(API_ID_ENV, "").strip()
    api_hash = os.environ.get(API_HASH_ENV, "").strip()

    if not api_id_text or not api_hash:
        raise SystemExit(f"{API_ID_ENV} and {API_HASH_ENV} must both be set")
    if not api_id_text.isascii() or not api_id_text.isdecimal() or int(api_id_text) <= 0:
        raise SystemExit(f"{API_ID_ENV} must be a positive integer")
    if re.fullmatch(r"[0-9a-fA-F]{32}", api_hash) is None:
        raise SystemExit(f"{API_HASH_ENV} must be a 32-character hexadecimal value")

    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(
        "\n".join(
            (
                '"""Generated defaults for an official release artifact."""',
                "",
                f"MTPROTO_API_ID: int | None = {int(api_id_text)}",
                f"MTPROTO_API_HASH: str | None = {api_hash!r}",
                "",
            )
        ),
        encoding="utf-8",
    )
    os.replace(temporary, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
