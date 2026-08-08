from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
import fcntl
import json
import math
import os
from pathlib import Path
import re
import tempfile
import tomllib
from typing import TYPE_CHECKING, Any

from .models import Origin
from .source_filter import (
    DEFAULT_SOURCE_FILTER_PATTERN,
    SOURCE_FILTER_STATE_KEY,
    compile_source_filter,
)

if TYPE_CHECKING:
    from .store import Store


CATALOG_VERSION = 1
SUPPORTED_SOURCE_KINDS = {
    "youtube": frozenset({"uploads", "vod_after_live"}),
    "twitch": frozenset({"vods", "highlights", "uploads"}),
    # Kept for existing installations and internal integrations. The public
    # onboarding flow intentionally focuses on YouTube and Twitch.
    "rss": frozenset({"feed"}),
}
_ORIGIN_FIELDS = {
    "id",
    "provider",
    "kind",
    "name",
    "external_id",
    "url",
    "enabled",
    "bootstrap",
    "credential_ref",
}
_BARE_TOML_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


class SourceCatalogError(ValueError):
    pass


@dataclass(frozen=True)
class SourceCatalog:
    version: int = CATALOG_VERSION
    source_filter: str = ""
    origins: tuple[Origin, ...] = ()


def load_source_catalog(path: str | Path) -> SourceCatalog:
    catalog_path = Path(path).expanduser()
    try:
        with catalog_path.open("rb") as file_handle:
            raw = tomllib.load(file_handle)
    except FileNotFoundError:
        raise
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SourceCatalogError(
            f"cannot load source catalog {catalog_path}: {exc}"
        ) from exc

    if set(raw) - {"version", "source_filter", "origins"}:
        unknown = ", ".join(sorted(set(raw) - {"version", "source_filter", "origins"}))
        raise SourceCatalogError(f"unknown source catalog field(s): {unknown}")
    if "version" not in raw:
        raise SourceCatalogError("source catalog requires version = 1")
    if "source_filter" not in raw:
        raise SourceCatalogError("source catalog requires source_filter")
    raw_origins = raw.get("origins", [])
    if not isinstance(raw_origins, list):
        raise SourceCatalogError("origins must be an array of tables")

    origins: list[Origin] = []
    for index, item in enumerate(raw_origins):
        if not isinstance(item, dict):
            raise SourceCatalogError(f"origins[{index}] must be a table")
        origins.append(_origin_from_mapping(item, index=index))
    return validate_source_catalog(
        SourceCatalog(
            version=raw["version"],
            source_filter=raw["source_filter"],
            origins=tuple(origins),
        )
    )


def validate_source_catalog(catalog: SourceCatalog) -> SourceCatalog:
    if not isinstance(catalog, SourceCatalog):
        raise SourceCatalogError("catalog must be a SourceCatalog")
    if type(catalog.version) is not int or catalog.version != CATALOG_VERSION:
        raise SourceCatalogError(
            f"unsupported source catalog version: {catalog.version!r}; expected 1"
        )
    if not isinstance(catalog.source_filter, str):
        raise SourceCatalogError("source_filter must be a string")
    try:
        compile_source_filter(catalog.source_filter)
    except ValueError as exc:
        raise SourceCatalogError(str(exc)) from exc

    origins: list[Origin] = []
    ids: set[str] = set()
    identities: dict[tuple[str, str, str, str], str] = {}
    for index, raw_origin in enumerate(catalog.origins):
        origin = _validate_origin(raw_origin, index=index)
        if origin.id in ids:
            raise SourceCatalogError(f"duplicate origin id: {origin.id}")
        ids.add(origin.id)
        identity = normalized_source_identity(origin)
        previous_id = identities.get(identity)
        if previous_id is not None and previous_id != origin.id:
            raise SourceCatalogError(
                f"origins {previous_id!r} and {origin.id!r} have the same source identity"
            )
        identities[identity] = origin.id
        origins.append(origin)
    return SourceCatalog(
        version=CATALOG_VERSION,
        source_filter=catalog.source_filter,
        origins=tuple(origins),
    )


def render_source_catalog(catalog: SourceCatalog) -> str:
    catalog = validate_source_catalog(catalog)
    lines = [
        f"version = {CATALOG_VERSION}",
        f"source_filter = {_toml_value(catalog.source_filter)}",
    ]
    for origin in catalog.origins:
        lines.extend(
            [
                "",
                "[[origins]]",
                f"id = {_toml_value(origin.id)}",
                f"provider = {_toml_value(origin.provider)}",
                f"kind = {_toml_value(origin.kind)}",
                f"name = {_toml_value(origin.name)}",
                f"external_id = {_toml_value(origin.external_id)}",
                f"enabled = {_toml_value(origin.enabled)}",
                f"bootstrap = {_toml_value(origin.bootstrap)}",
            ]
        )
        if origin.credential_ref is not None:
            lines.append(f"credential_ref = {_toml_value(origin.credential_ref)}")
        for key in sorted(origin.options):
            lines.append(f"{_toml_key(key)} = {_toml_value(origin.options[key])}")
    return "\n".join(lines) + "\n"


def write_source_catalog(path: str | Path, catalog: SourceCatalog) -> None:
    catalog_path = Path(path).expanduser()
    rendered = render_source_catalog(catalog).encode("utf-8")
    with _directory_lock(catalog_path.parent):
        _atomic_replace(catalog_path, rendered)


# Short aliases make the module convenient for integrations without hiding
# the more descriptive public names above.
load = load_source_catalog
render = render_source_catalog
validate = validate_source_catalog


class SourceCatalogManager:
    def __init__(self, path: str | Path, store: Store, max_failures: int = 5):
        self.path = Path(path).expanduser()
        self.store = store
        self.max_failures = max_failures
        if self.max_failures <= 0:
            raise SourceCatalogError("max_failures must be positive")

    def ensure(
        self,
        legacy_origins: Sequence[Origin] = (),
        *,
        legacy_declared: bool = False,
    ) -> SourceCatalog:
        """Create a missing catalog from durable state, then apply it."""

        with _directory_lock(self.path.parent):
            if self.path.exists():
                catalog = self._load_for_apply()
                self._reconcile(catalog)
                return catalog

            database_origins: dict[str, Origin] = {}
            database_managers = (
                ("catalog", "control")
                if legacy_declared
                else ("catalog", "control", "config")
            )
            for manager in database_managers:
                for origin in self.store.list_origins(managed_by=manager):
                    database_origins[origin.id] = origin
            durable_origins = tuple(
                database_origins[key] for key in sorted(database_origins)
            )
            seed_origins = (
                _merge_migration_origins(durable_origins, legacy_origins)
                if legacy_declared
                else durable_origins
            )
            stored_filter = self.store.get_bot_state(SOURCE_FILTER_STATE_KEY)
            catalog = validate_source_catalog(
                SourceCatalog(
                    source_filter=(
                        DEFAULT_SOURCE_FILTER_PATTERN
                        if stored_filter is None
                        else stored_filter
                    ),
                    origins=seed_origins,
                )
            )
            self._write_and_reconcile(catalog, previous=None)
            return catalog

    def apply(self) -> SourceCatalog:
        with _directory_lock(self.path.parent):
            catalog = self._load_for_apply()
            self._reconcile(catalog)
            return catalog

    def replace(self, catalog: SourceCatalog) -> SourceCatalog:
        """Atomically replace and apply the canonical catalog."""

        updated = validate_source_catalog(catalog)
        with _directory_lock(self.path.parent):
            previous = self.path.read_bytes() if self.path.exists() else None
            self._write_and_reconcile(updated, previous=previous)
            return updated

    def list(self) -> list[Origin]:
        with _directory_lock(self.path.parent, exclusive=False):
            return list(load_source_catalog(self.path).origins)

    def add(self, origin: Origin) -> SourceCatalog:
        def add_origin(catalog: SourceCatalog) -> SourceCatalog:
            if any(item.id == origin.id for item in catalog.origins):
                raise SourceCatalogError(f"origin {origin.id!r} already exists")
            return replace(catalog, origins=(*catalog.origins, origin))

        return self._mutate(add_origin)

    def set_enabled(self, origin_id: str, enabled: bool) -> SourceCatalog:
        if not isinstance(enabled, bool):
            raise SourceCatalogError("enabled must be true or false")
        return self._update_origin(
            origin_id,
            lambda origin: replace(origin, enabled=enabled),
        )

    def set_recording_mode(self, origin_id: str, recording_mode: str) -> SourceCatalog:
        mode = str(recording_mode).strip().lower()
        if mode not in {"vod", "live"}:
            raise SourceCatalogError("recording_mode must be 'vod' or 'live'")

        def update(origin: Origin) -> Origin:
            if origin.provider != "twitch" or origin.kind != "vods":
                raise SourceCatalogError(
                    "recording_mode is only valid for a Twitch vods origin"
                )
            options = dict(origin.options)
            options["recording_mode"] = mode
            return replace(origin, options=options)

        return self._update_origin(origin_id, update)

    def rename(self, origin_id: str, name: str) -> SourceCatalog:
        normalized_name = str(name).strip()
        if not normalized_name:
            raise SourceCatalogError("origin name must not be empty")
        return self._update_origin(
            origin_id,
            lambda origin: replace(origin, name=normalized_name),
        )

    def request_backfill(self, origin_id: str) -> SourceCatalog:
        return self._update_origin(
            origin_id,
            lambda origin: replace(origin, bootstrap="all"),
        )

    def delete(self, origin_id: str) -> SourceCatalog:
        def delete_origin(catalog: SourceCatalog) -> SourceCatalog:
            if not any(origin.id == origin_id for origin in catalog.origins):
                raise SourceCatalogError(f"origin {origin_id!r} does not exist")
            return replace(
                catalog,
                origins=tuple(
                    origin for origin in catalog.origins if origin.id != origin_id
                ),
            )

        return self._mutate(delete_origin)

    def set_filter(self, pattern: str) -> SourceCatalog:
        if not isinstance(pattern, str):
            raise SourceCatalogError("source_filter must be a string")
        try:
            compile_source_filter(pattern)
        except ValueError as exc:
            raise SourceCatalogError(str(exc)) from exc
        return self._mutate(lambda catalog: replace(catalog, source_filter=pattern))

    def _update_origin(
        self,
        origin_id: str,
        update: Callable[[Origin], Origin],
    ) -> SourceCatalog:
        def update_catalog(catalog: SourceCatalog) -> SourceCatalog:
            found = False
            origins: list[Origin] = []
            for origin in catalog.origins:
                if origin.id == origin_id:
                    found = True
                    origins.append(update(origin))
                else:
                    origins.append(origin)
            if not found:
                raise SourceCatalogError(f"origin {origin_id!r} does not exist")
            return replace(catalog, origins=tuple(origins))

        return self._mutate(update_catalog)

    def _mutate(
        self,
        mutation: Callable[[SourceCatalog], SourceCatalog],
    ) -> SourceCatalog:
        with _directory_lock(self.path.parent):
            previous_bytes = self.path.read_bytes()
            current = load_source_catalog(self.path)
            updated = validate_source_catalog(mutation(current))
            self._write_and_reconcile(updated, previous=previous_bytes)
            return updated

    def _write_and_reconcile(
        self,
        catalog: SourceCatalog,
        *,
        previous: bytes | None,
    ) -> None:
        rendered = render_source_catalog(catalog).encode("utf-8")
        try:
            _atomic_replace(self.path, rendered)
            self._reconcile(catalog)
        except BaseException as exc:
            try:
                if previous is None:
                    self.path.unlink(missing_ok=True)
                    _fsync_directory(self.path.parent)
                else:
                    _atomic_replace(self.path, previous)
            except OSError as rollback_exc:
                exc.add_note(f"failed to restore source catalog: {rollback_exc}")
            raise

    def _reconcile(self, catalog: SourceCatalog) -> None:
        try:
            self.store.reconcile_source_catalog(
                catalog.origins,
                catalog.source_filter,
                max_failures=self.max_failures,
            )
        except SourceCatalogError:
            raise
        except ValueError as exc:
            raise SourceCatalogError(str(exc)) from exc

    def _load_for_apply(self) -> SourceCatalog:
        catalog = load_source_catalog(self.path)
        try:
            self.path.chmod(0o600)
        except OSError as exc:
            raise SourceCatalogError(
                f"cannot set source catalog permissions to 0600 for {self.path}: {exc}"
            ) from exc
        return catalog


def _merge_migration_origins(
    durable_origins: Sequence[Origin],
    legacy_origins: Sequence[Origin],
) -> tuple[Origin, ...]:
    """Merge explicit legacy declarations without overriding Panel/catalog state."""

    merged_by_id: dict[str, Origin] = {}
    identities: dict[tuple[str, str, str, str], str] = {}

    def add(origin: Origin, *, legacy: bool) -> None:
        normalized = validate_source_catalog(
            SourceCatalog(origins=(origin,))
        ).origins[0]
        existing = merged_by_id.get(normalized.id)
        if existing is not None:
            if existing == normalized:
                return
            source = "legacy declaration" if legacy else "database source"
            raise SourceCatalogError(
                f"{source} {normalized.id!r} conflicts with an existing "
                "Panel/catalog source using the same id"
            )

        identity = normalized_source_identity(normalized)
        existing_id = identities.get(identity)
        if existing_id is not None:
            source = "legacy declaration" if legacy else "database source"
            raise SourceCatalogError(
                f"{source} {normalized.id!r} conflicts with existing "
                f"Panel/catalog source {existing_id!r} using the same source identity"
            )
        merged_by_id[normalized.id] = normalized
        identities[identity] = normalized.id

    for origin in durable_origins:
        add(origin, legacy=False)
    for origin in legacy_origins:
        add(origin, legacy=True)
    return tuple(merged_by_id[key] for key in sorted(merged_by_id))


def normalized_source_identity(origin: Origin) -> tuple[str, str, str, str]:
    provider = origin.provider.strip().casefold()
    kind = origin.kind.strip().casefold()
    external_id = origin.external_id.strip()
    if provider == "twitch":
        external_id = external_id.casefold()
    variant = ""
    if provider == "twitch" and kind == "vods":
        variant = str(origin.options.get("recording_mode") or "vod").strip().casefold()
    return provider, kind, external_id, variant


def _origin_from_mapping(raw: dict[str, Any], *, index: int) -> Origin:
    try:
        origin_id = _mapping_string(raw["id"], f"origins[{index}].id")
        provider = _mapping_string(raw["provider"], f"origins[{index}].provider")
    except KeyError as exc:
        raise SourceCatalogError(
            f"origins[{index}] requires {exc.args[0]}"
        ) from exc
    external_id = raw.get("external_id", raw.get("url"))
    if external_id is None:
        raise SourceCatalogError(f"origins[{index}] requires external_id")
    external_id = _mapping_string(external_id, f"origins[{index}].external_id")
    provider_text = provider.strip().lower()
    kind = (
        _mapping_string(raw["kind"], f"origins[{index}].kind")
        if "kind" in raw
        else _default_origin_kind(provider_text)
    )
    name = (
        _mapping_string(raw["name"], f"origins[{index}].name")
        if "name" in raw
        else origin_id
    )
    bootstrap = (
        _mapping_string(raw["bootstrap"], f"origins[{index}].bootstrap")
        if "bootstrap" in raw
        else "latest"
    )
    credential_ref = None
    if "credential_ref" in raw:
        credential_ref = _mapping_string(
            raw["credential_ref"],
            f"origins[{index}].credential_ref",
        )
    if "enabled" in raw and not isinstance(raw["enabled"], bool):
        raise SourceCatalogError(f"origins[{index}].enabled must be true or false")
    options = {
        key: value
        for key, value in raw.items()
        if key not in _ORIGIN_FIELDS
    }
    return Origin(
        id=origin_id,
        provider=provider_text,
        kind=kind,
        name=name,
        external_id=external_id,
        enabled=raw.get("enabled", True),
        bootstrap=bootstrap,
        credential_ref=credential_ref,
        options=options,
    )


def _mapping_string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise SourceCatalogError(f"{label} must be a string")
    return value


def _validate_origin(origin: Origin, *, index: int) -> Origin:
    if not isinstance(origin, Origin):
        raise SourceCatalogError(f"origins[{index}] must be an Origin")
    origin_id = _nonempty_string(origin.id, f"origins[{index}].id")
    provider = _nonempty_string(origin.provider, f"origin {origin_id!r} provider").lower()
    kind = _nonempty_string(origin.kind, f"origin {origin_id!r} kind").lower()
    supported_kinds = SUPPORTED_SOURCE_KINDS.get(provider)
    if supported_kinds is None:
        raise SourceCatalogError(
            f"origin {origin_id!r} has unsupported provider {provider!r}"
        )
    if kind not in supported_kinds:
        choices = ", ".join(sorted(supported_kinds))
        raise SourceCatalogError(
            f"origin {origin_id!r} has unsupported kind {kind!r} for "
            f"provider {provider!r}; expected one of: {choices}"
        )
    name = _nonempty_string(origin.name, f"origin {origin_id!r} name")
    external_id = _nonempty_string(
        origin.external_id,
        f"origin {origin_id!r} external_id",
    )
    if not isinstance(origin.enabled, bool):
        raise SourceCatalogError(f"origin {origin_id!r} enabled must be true or false")
    bootstrap = str(origin.bootstrap).strip().lower()
    if bootstrap not in {"latest", "all"}:
        raise SourceCatalogError(
            f"origin {origin_id!r} bootstrap must be 'latest' or 'all'"
        )
    credential_ref = origin.credential_ref
    if credential_ref is not None:
        credential_ref = _nonempty_string(
            credential_ref,
            f"origin {origin_id!r} credential_ref",
        )
    if not isinstance(origin.options, dict):
        raise SourceCatalogError(f"origin {origin_id!r} options must be a mapping")
    options: dict[str, Any] = {}
    for key, value in origin.options.items():
        if not isinstance(key, str) or not key:
            raise SourceCatalogError(f"origin {origin_id!r} option names must be non-empty strings")
        if key in _ORIGIN_FIELDS:
            raise SourceCatalogError(
                f"origin {origin_id!r} option {key!r} conflicts with a catalog field"
            )
        _validate_option_value(value, label=f"origin {origin_id!r} option {key!r}")
        options[key] = list(value) if isinstance(value, tuple) else value

    if "recording_mode" in options:
        if provider != "twitch" or kind != "vods":
            raise SourceCatalogError(
                f"origin {origin_id!r} recording_mode is only valid for Twitch kind='vods'"
            )
        mode = str(options["recording_mode"]).strip().lower()
        if mode not in {"vod", "live"}:
            raise SourceCatalogError(
                f"origin {origin_id!r} recording_mode must be 'vod' or 'live'"
            )
        options["recording_mode"] = mode

    if provider == "rss":
        if "allowed_media_hosts" in options:
            allowed_hosts = options["allowed_media_hosts"]
            if not isinstance(allowed_hosts, list) or not all(
                isinstance(item, str) for item in allowed_hosts
            ):
                raise SourceCatalogError(
                    f"origin {origin_id!r} allowed_media_hosts must be an array of strings"
                )
        if "allow_private_media" in options and not isinstance(
            options["allow_private_media"],
            bool,
        ):
            raise SourceCatalogError(
                f"origin {origin_id!r} allow_private_media must be true or false"
            )

    return Origin(
        id=origin_id,
        provider=provider,
        kind=kind,
        name=name,
        external_id=external_id,
        enabled=origin.enabled,
        bootstrap=bootstrap,
        credential_ref=credential_ref,
        options=options,
    )


def _validate_option_value(value: object, *, label: str) -> None:
    if value is None or isinstance(value, dict):
        raise SourceCatalogError(f"{label} must be a flat TOML value")
    if isinstance(value, float) and not math.isfinite(value):
        raise SourceCatalogError(f"{label} must be finite")
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (list, tuple, dict)) or item is None:
                raise SourceCatalogError(f"{label} must be a flat TOML array")
            _validate_option_value(item, label=label)
        return
    if not isinstance(value, (str, bool, int, float)):
        raise SourceCatalogError(f"{label} has an unsupported TOML value")


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise SourceCatalogError(f"{label} must be a string")
    normalized = value.strip()
    if not normalized:
        raise SourceCatalogError(f"{label} must not be empty")
    return normalized


def _default_origin_kind(provider: str) -> str:
    if provider == "youtube":
        return "uploads"
    if provider == "twitch":
        return "vods"
    return "feed"


def _toml_key(key: str) -> str:
    return key if _BARE_TOML_KEY.fullmatch(key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value: object) -> str:
    _validate_option_value(value, label="catalog value")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise SourceCatalogError("unsupported TOML value")


@contextmanager
def _directory_lock(directory: Path, *, exclusive: bool = True) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(directory, flags)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _atomic_replace(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as file_handle:
            descriptor = -1
            file_handle.write(content)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
