from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import getpass
from pathlib import Path
import sys
import tomllib
from typing import Any

from .config import Config, load_config
from .extension_api import (
    ExtensionSetupContext,
    OriginSuggestion,
    SetupPrompter,
)
from .extension_catalog import TrustedExtension, resolve_trusted_extension
from .extension_install import (
    ExtensionInstallResult,
    ensure_extension_installed,
)
from .extension_state import (
    ManagedExtensionSettings,
    ManagedExtensionState,
    extension_private_config_path,
    extension_sidecar_path,
    extension_state_lock,
    load_managed_extension_state,
    render_extension_config,
    render_managed_extension_state,
    replace_private_file,
    restore_private_file,
)
from .extensions import doctor_extension_runtime, prepare_extension_setup
from .setup import restart_managed_application_service


class ExtensionManagementError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExtensionEnableResult:
    extension: TrustedExtension
    installed: bool
    configured: bool
    enabled: bool
    already_enabled: bool
    service_restarted: bool
    config_path: Path | None = None
    suggested_origins: tuple[OriginSuggestion, ...] = ()


class ConsoleSetupPrompter:
    def choose(
        self,
        message: str,
        choices: Mapping[str, str],
        *,
        default: str,
    ) -> str:
        if default not in choices:
            raise ValueError(f"unknown default choice: {default!r}")
        print(message)
        for key, description in choices.items():
            suffix = " (default)" if key == default else ""
            print(f"  {key}. {description}{suffix}")
        while True:
            value = input(f"Choose [{default}]: ").strip().lower() or default
            if value in choices:
                return value
            print("choose one of: " + ", ".join(choices), file=sys.stderr)

    def text(self, message: str, *, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        value = input(f"{message}{suffix}: ").strip()
        return value or default

    def secret(self, message: str) -> str:
        return getpass.getpass(f"{message}: ").strip()

    def confirm(self, message: str, *, default: bool = False) -> bool:
        hint = "Y/n" if default else "y/N"
        while True:
            value = input(f"{message} [{hint}]: ").strip().lower()
            if not value:
                return default
            if value in {"y", "yes"}:
                return True
            if value in {"n", "no"}:
                return False
            print("enter yes or no", file=sys.stderr)


Installer = Callable[[TrustedExtension], ExtensionInstallResult]
Doctor = Callable[[Config], None]
Restarter = Callable[[Path], bool]


def _restart_previous_service(path: Path) -> bool:
    return restart_managed_application_service(path, require_active=False)


def enable_extension(
    name: str,
    config_path: str | Path,
    *,
    reconfigure: bool = False,
    restart_service: bool = True,
    interactive: bool = True,
    prompter: SetupPrompter | None = None,
    installer: Installer = ensure_extension_installed,
    doctor: Doctor = doctor_extension_runtime,
    restarter: Restarter = restart_managed_application_service,
    service_restorer: Restarter = _restart_previous_service,
) -> ExtensionEnableResult:
    extension = resolve_trusted_extension(name)
    path = Path(config_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).absolute()
    if not path.is_file():
        raise ExtensionManagementError(f"application config not found: {path}")
    prompt_adapter = prompter or ConsoleSetupPrompter()

    with extension_state_lock(path):
        # Validate the base configuration before installing or writing anything.
        before_config = _load_application_config(path)
        state = load_managed_extension_state(path)
        was_enabled = extension.extension_id in before_config.extensions.enabled

        try:
            install_result = installer(extension)
        except Exception as exc:
            if isinstance(exc, ExtensionManagementError):
                raise
            raise ExtensionManagementError(str(exc)) from exc

        if was_enabled and not reconfigure:
            try:
                doctor(before_config)
                restarted = (
                    restarter(path)
                    if restart_service and install_result.installed
                    else False
                )
            except Exception as exc:
                raise ExtensionManagementError(
                    f"extension {extension.slug!r} is enabled but unhealthy: {exc}"
                ) from exc
            return ExtensionEnableResult(
                extension=extension,
                installed=install_result.installed,
                configured=False,
                enabled=False,
                already_enabled=True,
                service_restarted=restarted,
                config_path=before_config.extensions.settings[
                    extension.extension_id
                ].config_file,
            )

        sidecar = extension_sidecar_path(path)
        sidecar_before = _read_optional_private_file(sidecar)
        extension_config_path: Path | None = None
        extension_config_before: bytes | None = None
        extension_config_rendered: bytes | None = None
        suggested_origins: tuple[OriginSuggestion, ...] = ()

        existing_settings = before_config.extensions.settings.get(
            extension.extension_id
        )
        if existing_settings is not None and existing_settings.config_file is not None:
            extension_config_path = existing_settings.config_file

        should_run_setup = reconfigure or extension_config_path is None
        if extension_config_path is not None and not extension_config_path.exists():
            should_run_setup = True

        if should_run_setup:
            existing_config = (
                _load_extension_config(extension_config_path)
                if extension_config_path is not None and extension_config_path.exists()
                else {}
            )
            prepared = prepare_extension_setup(
                extension.extension_id,
                ExtensionSetupContext(
                    interactive=interactive,
                    reconfigure=reconfigure,
                    existing_config=existing_config,
                    prompts=prompt_adapter,
                ),
            )
            suggested_origins = prepared.manifest.suggested_origins
            if prepared.manifest.config_filename is not None:
                if extension_config_path is None:
                    extension_config_path = extension_private_config_path(
                        path,
                        extension.extension_id,
                    ).with_name(prepared.manifest.config_filename)
                result_config = prepared.result.config
                if result_config is None:
                    result_config = prepared.manifest.default_config
                extension_config_rendered = render_extension_config(result_config)

        if extension_config_path is not None:
            extension_config_before = _read_optional_private_file(extension_config_path)

        settings = dict(state.settings)
        managed_settings = settings.get(
            extension.extension_id,
            ManagedExtensionSettings(),
        )
        config_reference = managed_settings.config_file
        if extension_config_path is not None and (
            existing_settings is None or existing_settings.config_file is None
        ):
            try:
                config_reference = str(extension_config_path.relative_to(path.parent))
            except ValueError:
                config_reference = str(extension_config_path)
        settings[extension.extension_id] = ManagedExtensionSettings(
            required=True,
            config_file=config_reference,
        )
        enabled = list(state.enabled)
        if extension.extension_id not in enabled:
            enabled.append(extension.extension_id)
        updated_state = ManagedExtensionState(
            enabled=tuple(enabled),
            settings=settings,
        )

        state_changed = render_managed_extension_state(updated_state) != sidecar_before
        config_changed = (
            extension_config_path is not None
            and extension_config_rendered is not None
            and extension_config_rendered != extension_config_before
        )
        config_write_attempted = False
        state_write_attempted = False
        restart_attempted = False
        try:
            if config_changed:
                config_write_attempted = True
                replace_private_file(extension_config_path, extension_config_rendered)
            if state_changed:
                state_write_attempted = True
                replace_private_file(sidecar, render_managed_extension_state(updated_state))
            configured = _load_application_config(path)
            doctor(configured)
            restarted = False
            if restart_service and (state_changed or config_changed or install_result.installed):
                restart_attempted = True
                restarted = restarter(path)
        except BaseException as exc:
            rollback_issues: list[str] = []
            if state_write_attempted:
                _restore_with_report(sidecar, sidecar_before, rollback_issues)
            if config_write_attempted and extension_config_path is not None:
                _restore_with_report(
                    extension_config_path,
                    extension_config_before,
                    rollback_issues,
                )
            if restart_attempted:
                try:
                    if not service_restorer(path):
                        rollback_issues.append(
                            "the previous managed service was not restored"
                        )
                except Exception as restart_exc:
                    rollback_issues.append(
                        f"could not restart the previous service state: {restart_exc}"
                    )
            if isinstance(exc, (KeyboardInterrupt, EOFError)):
                raise
            detail = f"could not enable extension {extension.slug!r}: {exc}"
            if rollback_issues:
                detail += "; rollback incomplete: " + "; ".join(rollback_issues)
            raise ExtensionManagementError(detail) from exc

        return ExtensionEnableResult(
            extension=extension,
            installed=install_result.installed,
            configured=config_changed,
            enabled=state_changed,
            already_enabled=False,
            service_restarted=restarted,
            config_path=extension_config_path,
            suggested_origins=suggested_origins,
        )


def _load_application_config(path: Path) -> Config:
    try:
        return load_config(path)
    except (OSError, TypeError, ValueError) as exc:
        raise ExtensionManagementError(
            f"could not load application config {path}: {exc}"
        ) from exc


def _load_extension_config(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ExtensionManagementError(f"refusing symlink for extension config: {path}")
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ExtensionManagementError(f"cannot load extension config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ExtensionManagementError(f"extension config {path} must be a TOML table")
    return raw


def _read_optional_private_file(path: Path) -> bytes | None:
    if path.is_symlink():
        raise ExtensionManagementError(f"refusing symlink for private file: {path}")
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ExtensionManagementError(f"cannot read private file {path}: {exc}") from exc


def _restore_with_report(
    path: Path,
    previous: bytes | None,
    issues: list[str],
) -> None:
    try:
        restore_private_file(path, previous)
    except Exception as exc:
        issues.append(f"could not restore {path}: {exc}")
