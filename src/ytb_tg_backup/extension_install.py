from __future__ import annotations

from dataclasses import dataclass
from importlib import invalidate_caches, metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Callable, Literal

from . import __version__
from .extension_api import (
    EXTENSION_API_LEVEL,
    EXTENSION_ENTRY_POINT_GROUP,
    EXTENSION_SETUP_API_LEVEL,
    EXTENSION_SETUP_ENTRY_POINT_GROUP,
)
from .extension_catalog import TrustedExtension, normalize_distribution_name


class ExtensionInstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class InstallTarget:
    kind: Literal["pipx", "venv", "container", "unsupported"]
    python: Path
    pipx: Path | None = None
    environment: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class ExtensionInstallResult:
    installed: bool
    distribution: str
    version: str
    command: tuple[str, ...] = ()


Runner = Callable[..., subprocess.CompletedProcess[str]]
_URL_USERINFO = re.compile(r"(?P<scheme>https?://)[^\s/@]+(?::[^\s/@]*)?@")
_CORE_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def detect_install_target(
    *,
    prefix: Path | None = None,
    base_prefix: Path | None = None,
    python: Path | None = None,
    environ: dict[str, str] | None = None,
    container: bool | None = None,
    runner: Runner = subprocess.run,
) -> InstallTarget:
    current_prefix = Path(prefix or sys.prefix)
    current_base = Path(base_prefix or sys.base_prefix)
    current_python = Path(python or sys.executable)
    current_environ = os.environ if environ is None else environ

    in_container = container
    if in_container is None:
        in_container = (
            current_environ.get("ASMR_TG_BACKUP_CONTAINER") == "1"
            or Path("/.dockerenv").exists()
            or Path("/run/.containerenv").exists()
        )
    if in_container:
        return InstallTarget(
            kind="container",
            python=current_python,
            reason="install the extension while building a derived image",
        )

    metadata_path = current_prefix / "pipx_metadata.json"
    if metadata_path.is_file():
        try:
            raw = json.loads(metadata_path.read_text(encoding="utf-8"))
            main_package = raw["main_package"]["package"]
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise ExtensionInstallError(
                f"cannot read pipx environment metadata {metadata_path}: {exc}"
            ) from exc
        if normalize_distribution_name(str(main_package)) != "asmr-tg-backup":
            raise ExtensionInstallError(
                f"pipx environment {current_prefix.name!r} belongs to "
                f"{main_package!r}, not asmr-tg-backup"
            )
        pipx = shutil.which("pipx")
        if not pipx:
            raise ExtensionInstallError(
                "this command runs from pipx, but the pipx executable is not on PATH"
            )
        return InstallTarget(
            kind="pipx",
            python=current_python,
            pipx=Path(pipx),
            environment=current_prefix.name,
        )

    if current_prefix != current_base and (current_prefix / "pyvenv.cfg").is_file():
        try:
            result = runner(
                [str(current_python), "-m", "pip", "--version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExtensionInstallError(
                f"cannot inspect pip in virtual environment {current_prefix}: {exc}"
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ExtensionInstallError(
                f"virtual environment {current_prefix} has no usable pip"
                + (f": {detail}" if detail else "")
            )
        return InstallTarget(kind="venv", python=current_python)

    return InstallTarget(
        kind="unsupported",
        python=current_python,
        reason=(
            "automatic extension installation is supported only for pipx or a "
            "virtual environment; create a venv instead of modifying system Python"
        ),
    )


def ensure_extension_installed(
    extension: TrustedExtension,
    *,
    runner: Runner = subprocess.run,
    target: InstallTarget | None = None,
) -> ExtensionInstallResult:
    validate_extension_compatibility(extension)
    installed_version = _distribution_version(extension.distribution)
    if installed_version == extension.version:
        verify_extension_distribution(extension)
        return ExtensionInstallResult(
            installed=False,
            distribution=extension.distribution,
            version=installed_version,
        )

    install_target = target or detect_install_target(runner=runner)
    replacing = installed_version is not None
    if install_target.kind == "pipx":
        if install_target.pipx is None or install_target.environment is None:
            raise ExtensionInstallError("invalid pipx installation target")
        command = [
            str(install_target.pipx),
            "inject",
            install_target.environment,
            extension.requirement,
        ]
        if replacing:
            command.append("--force")
    elif install_target.kind == "venv":
        command = [
            str(install_target.python),
            "-m",
            "pip",
            "install",
            extension.requirement,
        ]
        if replacing:
            command.extend(["--upgrade", "--upgrade-strategy", "only-if-needed"])
    elif install_target.kind == "container":
        raise ExtensionInstallError(
            f"{extension.distribution} is not included in this image; "
            f"add `RUN python -m pip install {extension.requirement}` to a derived image"
        )
    else:
        raise ExtensionInstallError(install_target.reason or "unsupported Python environment")

    try:
        result = runner(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=900,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExtensionInstallError(
            f"could not install {extension.slug}: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = _redact_subprocess_detail(
            (result.stderr or result.stdout).strip()[-2000:]
        )
        suffix = f": {detail}" if detail else ""
        raise ExtensionInstallError(
            f"extension install command failed with code {result.returncode}{suffix}"
        )

    invalidate_caches()
    verify_extension_distribution(extension)
    return ExtensionInstallResult(
        installed=True,
        distribution=extension.distribution,
        version=extension.version,
        command=tuple(command),
    )


def verify_extension_distribution(extension: TrustedExtension) -> None:
    validate_extension_compatibility(extension)
    try:
        distribution = metadata.distribution(extension.distribution)
    except metadata.PackageNotFoundError as exc:
        raise ExtensionInstallError(
            f"extension distribution is not installed: {extension.distribution}"
        ) from exc
    actual_name = distribution.metadata.get("Name") or extension.distribution
    if normalize_distribution_name(actual_name) != normalize_distribution_name(
        extension.distribution
    ):
        raise ExtensionInstallError(
            f"extension distribution identity mismatch: {actual_name!r}"
        )
    if distribution.version != extension.version:
        raise ExtensionInstallError(
            f"{extension.distribution} {distribution.version} is installed; "
            f"trusted catalog requires {extension.version}"
        )

    _verify_entry_point(
        extension,
        group=EXTENSION_ENTRY_POINT_GROUP,
        expected_value=extension.runtime_entry_point,
        required=True,
    )
    _verify_entry_point(
        extension,
        group=EXTENSION_SETUP_ENTRY_POINT_GROUP,
        expected_value=extension.setup_entry_point,
        required=extension.setup_entry_point is not None,
    )


def validate_extension_compatibility(
    extension: TrustedExtension,
    *,
    core_version: str = __version__,
    runtime_api_level: int = EXTENSION_API_LEVEL,
    setup_api_level: int = EXTENSION_SETUP_API_LEVEL,
) -> None:
    match = _CORE_VERSION.match(core_version)
    if match is None:
        raise ExtensionInstallError(
            f"cannot compare core version {core_version!r} with "
            f"{extension.core_requires}"
        )
    current = tuple(int(part) for part in match.groups())
    if not extension.minimum_core_version <= current < extension.maximum_core_version:
        raise ExtensionInstallError(
            f"{extension.slug} requires asmr-tg-backup {extension.core_requires}; "
            f"this process is {core_version}"
        )
    if extension.runtime_api_level != runtime_api_level:
        raise ExtensionInstallError(
            f"{extension.slug} requires extension runtime API "
            f"{extension.runtime_api_level}; this process provides {runtime_api_level}"
        )
    if (
        extension.setup_entry_point is not None
        and extension.setup_api_level != setup_api_level
    ):
        raise ExtensionInstallError(
            f"{extension.slug} requires extension setup API "
            f"{extension.setup_api_level}; this process provides {setup_api_level}"
        )


def _verify_entry_point(
    extension: TrustedExtension,
    *,
    group: str,
    expected_value: str | None,
    required: bool,
) -> None:
    matches = [
        entry_point
        for entry_point in metadata.entry_points(group=group)
        if entry_point.name.strip().lower() == extension.extension_id
    ]
    if not matches:
        if required:
            raise ExtensionInstallError(
                f"{extension.distribution} does not expose {group} "
                f"entry point {extension.extension_id!r}"
            )
        return
    if len(matches) != 1:
        raise ExtensionInstallError(
            f"duplicate {group} entry point {extension.extension_id!r}"
        )
    entry_point = matches[0]
    distribution = entry_point.dist
    actual_distribution = distribution.name if distribution is not None else ""
    if normalize_distribution_name(actual_distribution) != normalize_distribution_name(
        extension.distribution
    ):
        raise ExtensionInstallError(
            f"entry point {extension.extension_id!r} belongs to unexpected distribution "
            f"{actual_distribution or '<unknown>'!r}"
        )
    if expected_value is not None and entry_point.value != expected_value:
        raise ExtensionInstallError(
            f"entry point {extension.extension_id!r} has unexpected target "
            f"{entry_point.value!r}"
        )


def _distribution_version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def _redact_subprocess_detail(value: str) -> str:
    return _URL_USERINFO.sub(r"\g<scheme><redacted>@", value)
