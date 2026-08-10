from __future__ import annotations

from dataclasses import dataclass
import difflib
import re


_NORMALIZE_DISTRIBUTION = re.compile(r"[-_.]+")


@dataclass(frozen=True)
class TrustedExtension:
    slug: str
    aliases: tuple[str, ...]
    extension_id: str
    distribution: str
    version: str
    runtime_entry_point: str
    setup_entry_point: str | None = None
    runtime_api_level: int = 1
    setup_api_level: int | None = None
    minimum_core_version: tuple[int, int, int] = (0, 6, 0)
    maximum_core_version: tuple[int, int, int] = (0, 7, 0)

    @property
    def requirement(self) -> str:
        return f"{self.distribution}=={self.version}"

    @property
    def core_requires(self) -> str:
        minimum = ".".join(str(part) for part in self.minimum_core_version)
        maximum = ".".join(str(part) for part in self.maximum_core_version)
        return f">={minimum},<{maximum}"

    @property
    def names(self) -> tuple[str, ...]:
        return (self.slug, *self.aliases, self.extension_id, self.distribution)


TRUSTED_EXTENSIONS = (
    TrustedExtension(
        slug="proxy-router",
        aliases=("proxy",),
        extension_id="dreaife.proxy-router",
        distribution="asmr-tg-backup-ext-proxy-router",
        version="0.2.0",
        runtime_entry_point="asmr_tg_proxy_router:create_extension",
        setup_entry_point="asmr_tg_proxy_router.setup:create_configurator",
        setup_api_level=1,
    ),
    TrustedExtension(
        slug="niconico-origin",
        aliases=("niconico", "nico"),
        extension_id="dreaife.niconico-origin",
        distribution="asmr-tg-backup-ext-niconico-origin",
        version="0.2.0",
        runtime_entry_point="asmr_tg_niconico_origin:create_extension",
        setup_entry_point="asmr_tg_niconico_origin.setup:create_setup_manifest",
        setup_api_level=1,
    ),
)


def normalize_distribution_name(value: str) -> str:
    return _NORMALIZE_DISTRIBUTION.sub("-", value).lower()


def resolve_trusted_extension(value: str) -> TrustedExtension:
    candidate = value.strip().lower()
    for item in TRUSTED_EXTENSIONS:
        if any(candidate == name.lower() for name in item.names):
            return item
    suggestions = difflib.get_close_matches(
        candidate,
        [name for item in TRUSTED_EXTENSIONS for name in item.names],
        n=3,
        cutoff=0.55,
    )
    hint = f"; did you mean {', '.join(repr(item) for item in suggestions)}?" if suggestions else ""
    raise ValueError(f"unknown trusted extension: {value!r}{hint}")


def trusted_extension_by_id(extension_id: str) -> TrustedExtension | None:
    candidate = extension_id.strip().lower()
    return next(
        (item for item in TRUSTED_EXTENSIONS if item.extension_id == candidate),
        None,
    )
