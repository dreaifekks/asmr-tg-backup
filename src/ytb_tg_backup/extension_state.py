from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib
from typing import Any


MANAGED_EXTENSION_SCHEMA = 1
MANAGED_EXTENSION_MARKER = "# Managed by `asmr-tg-backup extensions`."
_EXTENSION_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_BARE_TOML_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


class ExtensionStateError(ValueError):
    pass


@dataclass(frozen=True)
class ManagedExtensionSettings:
    required: bool = True
    config_file: str | None = None


@dataclass(frozen=True)
class ManagedExtensionState:
    enabled: tuple[str, ...] = ()
    settings: Mapping[str, ManagedExtensionSettings] = field(default_factory=dict)


def extension_sidecar_path(config_path: str | Path) -> Path:
    path = Path(config_path).expanduser()
    if path.suffix.lower() == ".toml":
        return path.with_name(f"{path.stem}.extensions.toml")
    return path.with_name(f"{path.name}.extensions.toml")


def extension_private_config_path(config_path: str | Path, extension_id: str) -> Path:
    _validate_extension_id(extension_id)
    path = Path(config_path).expanduser()
    profile = path.stem if path.suffix else path.name
    return path.parent / "extensions" / profile / f"{extension_id}.toml"


def load_managed_extension_state(config_path: str | Path) -> ManagedExtensionState:
    path = extension_sidecar_path(config_path)
    if path.is_symlink():
        raise ExtensionStateError(f"refusing symlink for managed extension state: {path}")
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        return ManagedExtensionState()
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ExtensionStateError(f"cannot load managed extension state {path}: {exc}") from exc

    unknown = set(raw) - {"schema", "extensions"}
    if unknown:
        raise ExtensionStateError(
            "unknown managed extension state field(s): " + ", ".join(sorted(unknown))
        )
    if raw.get("schema") != MANAGED_EXTENSION_SCHEMA:
        raise ExtensionStateError(
            f"unsupported managed extension state schema: {raw.get('schema')!r}"
        )
    extensions = raw.get("extensions")
    if not isinstance(extensions, dict):
        raise ExtensionStateError("managed extension state requires [extensions]")
    enabled_raw = extensions.get("enabled", [])
    if not isinstance(enabled_raw, list) or not all(
        isinstance(item, str) for item in enabled_raw
    ):
        raise ExtensionStateError("managed extensions.enabled must be an array of IDs")

    enabled: list[str] = []
    seen: set[str] = set()
    for raw_id in enabled_raw:
        extension_id = raw_id.strip().lower()
        _validate_extension_id(extension_id)
        if extension_id in seen:
            raise ExtensionStateError(f"duplicate managed extension id: {extension_id}")
        seen.add(extension_id)
        enabled.append(extension_id)

    settings: dict[str, ManagedExtensionSettings] = {}
    for raw_id, value in extensions.items():
        if raw_id == "enabled":
            continue
        extension_id = str(raw_id).strip().lower()
        _validate_extension_id(extension_id)
        if not isinstance(value, dict):
            raise ExtensionStateError(
                f"managed extensions.{raw_id} must be a table"
            )
        unknown_settings = set(value) - {"required", "config_file"}
        if unknown_settings:
            raise ExtensionStateError(
                f"unknown managed settings for {extension_id}: "
                + ", ".join(sorted(unknown_settings))
            )
        required = value.get("required", True)
        if not isinstance(required, bool):
            raise ExtensionStateError(
                f"managed extensions.{extension_id}.required must be true or false"
            )
        config_file_raw = value.get("config_file")
        config_file = None
        if config_file_raw is not None:
            if not isinstance(config_file_raw, str) or not config_file_raw.strip():
                raise ExtensionStateError(
                    f"managed extensions.{extension_id}.config_file must be a path"
                )
            config_file = config_file_raw.strip()
        settings[extension_id] = ManagedExtensionSettings(
            required=required,
            config_file=config_file,
        )
    for extension_id in enabled:
        settings.setdefault(extension_id, ManagedExtensionSettings())
    return ManagedExtensionState(enabled=tuple(enabled), settings=settings)


def render_managed_extension_state(state: ManagedExtensionState) -> bytes:
    lines = [
        MANAGED_EXTENSION_MARKER,
        f"schema = {MANAGED_EXTENSION_SCHEMA}",
        "",
        "[extensions]",
        f"enabled = {_toml_value(list(state.enabled))}",
    ]
    for extension_id in sorted(state.settings):
        _validate_extension_id(extension_id)
        settings = state.settings[extension_id]
        lines.extend(
            [
                "",
                f"[extensions.{_toml_key(extension_id)}]",
                f"required = {_toml_value(settings.required)}",
            ]
        )
        if settings.config_file is not None:
            lines.append(f"config_file = {_toml_value(settings.config_file)}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def managed_extension_table(state: ManagedExtensionState) -> dict[str, Any]:
    table: dict[str, Any] = {"enabled": list(state.enabled)}
    for extension_id, settings in state.settings.items():
        values: dict[str, Any] = {"required": settings.required}
        if settings.config_file is not None:
            values["config_file"] = settings.config_file
        table[extension_id] = values
    return table


def merge_extension_tables(base: object, managed: ManagedExtensionState) -> dict[str, Any]:
    if base is None:
        base_table: dict[str, Any] = {}
    elif isinstance(base, dict):
        base_table = dict(base)
    else:
        raise ExtensionStateError("extensions must be a table")

    managed_table = managed_extension_table(managed)
    base_enabled = base_table.get("enabled", [])
    managed_enabled = managed_table.pop("enabled")
    if not isinstance(base_enabled, list):
        # Let the existing config validator produce its stable error message.
        return base_table
    merged_enabled = list(base_enabled)
    normalized_enabled = {
        item.strip().lower()
        for item in base_enabled
        if isinstance(item, str)
    }
    for extension_id in managed_enabled:
        if extension_id not in normalized_enabled:
            merged_enabled.append(extension_id)
            normalized_enabled.add(extension_id)

    merged: dict[str, Any] = {"enabled": merged_enabled}
    for extension_id, values in managed_table.items():
        merged[extension_id] = dict(values)
    for key, value in base_table.items():
        if key == "enabled":
            continue
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


@contextmanager
def extension_state_lock(config_path: str | Path) -> Iterator[None]:
    directory = extension_sidecar_path(config_path).parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(directory, flags)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def replace_private_file(path: Path, content: bytes) -> None:
    if path.is_symlink():
        raise ExtensionStateError(f"refusing symlink for private file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def restore_private_file(path: Path, previous: bytes | None) -> None:
    if previous is None:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        _fsync_directory(path.parent)
        return
    replace_private_file(path, previous)


def render_extension_config(config: Mapping[str, Any]) -> bytes:
    if not isinstance(config, Mapping):
        raise ExtensionStateError("extension setup config must be a mapping")
    lines: list[str] = []
    _render_mapping(lines, (), config)
    return (("\n".join(lines).rstrip() + "\n") if lines else "").encode("utf-8")


def _render_mapping(
    lines: list[str],
    path: tuple[str, ...],
    values: Mapping[str, Any],
) -> None:
    scalar_items: list[tuple[str, Any]] = []
    table_items: list[tuple[str, Mapping[str, Any]]] = []
    for raw_key, value in values.items():
        if not isinstance(raw_key, str) or not raw_key:
            raise ExtensionStateError("extension config keys must be non-empty strings")
        if isinstance(value, Mapping):
            table_items.append((raw_key, value))
        else:
            scalar_items.append((raw_key, value))
    for key, value in scalar_items:
        lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    for key, table in table_items:
        if lines and lines[-1] != "":
            lines.append("")
        table_path = (*path, key)
        lines.append("[" + ".".join(_toml_key(item) for item in table_path) + "]")
        _render_mapping(lines, table_path, table)


def _toml_key(value: str) -> str:
    return value if _BARE_TOML_KEY.fullmatch(value) else json.dumps(value, ensure_ascii=False)


def _toml_value(value: object) -> str:
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
    raise ExtensionStateError(
        f"unsupported extension config value: {type(value).__name__}"
    )


def _validate_extension_id(value: str) -> None:
    if not _EXTENSION_ID.fullmatch(value):
        raise ExtensionStateError(f"invalid extension id: {value!r}")


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
