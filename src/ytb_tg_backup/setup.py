from __future__ import annotations

from dataclasses import dataclass
import getpass
import json
import os
from pathlib import Path
import shutil
import socket
import string
import subprocess
import sys
import time
from urllib.parse import urlsplit

from .config import load_config, resolve_mtproto_credentials
from .network import is_loopback_url
from .service import BackupService


LOCAL_API_HOST = "127.0.0.1"
LOCAL_API_PORT = 18081
LOCAL_API_URL = f"http://{LOCAL_API_HOST}:{LOCAL_API_PORT}"
LOCAL_API_UNIT = "asmr-tg-backup-telegram-bot-api.service"
LOCAL_API_READY_TIMEOUT_SECONDS = 12.0
LOCAL_API_READY_POLL_SECONDS = 0.25
LOCAL_API_CONNECT_TIMEOUT_SECONDS = 0.5
LOCAL_API_REQUIRED_FLAGS = (
    "--api-id",
    "--api-hash",
    "--local",
    "--http-ip-address",
    "--http-port",
    "--dir",
    "--temp-dir",
)


class SetupError(RuntimeError):
    pass


class SetupTargetExistsError(SetupError):
    pass


@dataclass(frozen=True)
class LocalApiPaths:
    credentials: Path
    unit: Path
    data_dir: Path
    temp_dir: Path


@dataclass(frozen=True)
class LocalApiSetup:
    executable: Path
    systemctl: Path
    api_id: str
    api_hash: str
    paths: LocalApiPaths


@dataclass(frozen=True)
class SetupAnswers:
    profile: str
    upload_transport: str
    bot_token: str
    chat_id: str
    allowed_user_id: str
    mtproto_api_id: int | None = None
    mtproto_api_hash: str = ""
    api_base: str = "https://api.telegram.org"
    bot_api_max_upload_bytes: int = 49_000_000
    bot_api_split_large_audio: bool = True
    local_api: LocalApiSetup | None = None


@dataclass(frozen=True)
class SetupResult:
    config_path: Path
    db_path: Path
    profile: str
    local_service_unit: str | None


@dataclass(frozen=True)
class _InstalledLocalApi:
    setup: LocalApiSetup


def default_config_path() -> Path:
    return _config_home() / "asmr-tg-backup" / "config.toml"


def default_data_path() -> Path:
    return _data_home() / "asmr-tg-backup"


def local_api_paths() -> LocalApiPaths:
    root = default_data_path() / "telegram-bot-api"
    return LocalApiPaths(
        credentials=_config_home() / "asmr-tg-backup" / "telegram-bot-api.env",
        unit=_config_home() / "systemd" / "user" / LOCAL_API_UNIT,
        data_dir=root,
        temp_dir=root / "tmp",
    )


def run_interactive_setup(output_path: Path) -> SetupResult:
    output_path = output_path.expanduser()
    _require_unused_target(output_path, "application config")
    answers = prompt_setup()

    installed: _InstalledLocalApi | None = None
    config_created = False
    try:
        if answers.local_api is not None:
            installed = _install_local_api(answers.local_api)

        _write_setup_config(output_path, answers)
        config_created = True
        config = load_config(output_path)
        service = BackupService(config)
        try:
            service.initialize()
        except BaseException as initialize_exc:
            try:
                service.store.close()
            except BaseException as close_exc:
                raise SetupError(
                    f"database initialization failed: {initialize_exc}; "
                    f"additionally could not close the store: {close_exc}"
                ) from initialize_exc
            raise
        else:
            service.store.close()
    except BaseException as exc:
        cleanup_issues: list[str] = []
        if config_created:
            cleanup_issues.extend(
                _remove_created_file(output_path, label="application config")
            )
        if installed is not None:
            cleanup_issues.extend(_remove_local_api(installed.setup))
        if cleanup_issues:
            raise SetupError(
                f"setup failed: {exc}; rollback incomplete; manual cleanup required: "
                + "; ".join(cleanup_issues)
            ) from exc
        if isinstance(exc, (EOFError, KeyboardInterrupt, SetupError)):
            raise
        raise SetupError(f"setup failed: {exc}") from exc

    return SetupResult(
        config_path=output_path,
        db_path=config.db_path,
        profile=answers.profile,
        local_service_unit=LOCAL_API_UNIT if installed is not None else None,
    )


def prompt_setup() -> SetupAnswers:
    print("Telegram upload mode:")
    print("  1. MTProto direct upload (default)")
    print("  2. Bot API - custom URL, local service, or 49 MB splitting")
    while True:
        choice = input("Choose mode [1]: ").strip().lower()
        if choice in {"", "1", "mtproto", "mtp", "direct"}:
            return _prompt_mtproto_setup()
        if choice in {"2", "api", "bot-api", "bot_api"}:
            return _prompt_bot_api_setup()
        print("enter 1 for MTProto or 2 for Bot API", file=sys.stderr)


def _prompt_mtproto_setup() -> SetupAnswers:
    try:
        api_id, api_hash = resolve_mtproto_credentials({})
    except ValueError as exc:
        raise SetupError(str(exc)) from exc
    if api_id is not None and api_hash:
        return _finish_answers(
            profile="mtproto-defaults",
            upload_transport="mtproto",
        )

    print("This source build has no bundled Telegram application credentials.")
    print("  a. Enter your own Telegram API ID and hash (default)")
    print("  b. Configure a Bot API transport instead")
    while True:
        choice = input("Choose credential source [a]: ").strip().lower()
        if choice in {"", "a", "1", "own", "credentials"}:
            return _finish_answers(
                profile="mtproto-own",
                upload_transport="mtproto",
                mtproto_api_id=int(_prompt_api_id()),
                mtproto_api_hash=_prompt_api_hash(),
            )
        if choice in {"b", "2", "api", "bot-api", "bot_api"}:
            return _prompt_bot_api_setup()
        print("enter a for your own credentials or b for Bot API", file=sys.stderr)


def _prompt_bot_api_setup() -> SetupAnswers:
    print("Bot API source:")
    print("  a. Use an existing trusted API URL")
    print("  b. Register a local telegram-bot-api user service")
    print("  c. Use api.telegram.org with 49 MB audio parts")
    while True:
        choice = input("Choose API source [a]: ").strip().lower()
        if choice in {"", "a", "1", "existing", "url"}:
            return _finish_answers(
                profile="custom-api-single",
                upload_transport="bot_api",
                api_base=_prompt_api_base(),
                bot_api_max_upload_bytes=1_990_000_000,
                bot_api_split_large_audio=False,
            )
        if choice in {"b", "2", "local", "systemd"}:
            local_api = _prompt_local_api_setup()
            return _finish_answers(
                profile="local-api-single",
                upload_transport="bot_api",
                api_base=LOCAL_API_URL,
                bot_api_max_upload_bytes=1_990_000_000,
                bot_api_split_large_audio=False,
                local_api=local_api,
            )
        if choice in {"c", "3", "official", "split", "official-split"}:
            return _finish_answers(
                profile="official-api-split",
                upload_transport="bot_api",
            )
        print("enter a for an existing URL, b for a local service, or c for splitting", file=sys.stderr)


def _prompt_local_api_setup() -> LocalApiSetup:
    paths = local_api_paths()
    _require_unused_target(paths.credentials, "local Bot API credentials")
    _require_unused_target(paths.unit, "local Bot API user unit")
    _assert_local_port_available()
    systemctl = _find_systemctl()
    executable = _find_local_bot_api_executable()
    api_id = _prompt_api_id()
    api_hash = _prompt_api_hash()
    return LocalApiSetup(
        executable=executable,
        systemctl=systemctl,
        api_id=api_id,
        api_hash=api_hash,
        paths=paths,
    )


def _finish_answers(
    *,
    profile: str,
    upload_transport: str,
    mtproto_api_id: int | None = None,
    mtproto_api_hash: str = "",
    api_base: str = "https://api.telegram.org",
    bot_api_max_upload_bytes: int = 49_000_000,
    bot_api_split_large_audio: bool = True,
    local_api: LocalApiSetup | None = None,
) -> SetupAnswers:
    bot_token = _prompt_secret("Telegram bot token (hidden): ", label="bot token")
    chat_id = _prompt_plain_value(
        "Destination chat ID or @channel: ",
        label="destination chat",
    )
    allowed_user_id = _prompt_telegram_user_id()
    return SetupAnswers(
        profile=profile,
        upload_transport=upload_transport,
        bot_token=bot_token,
        chat_id=chat_id,
        allowed_user_id=allowed_user_id,
        mtproto_api_id=mtproto_api_id,
        mtproto_api_hash=mtproto_api_hash,
        api_base=api_base,
        bot_api_max_upload_bytes=bot_api_max_upload_bytes,
        bot_api_split_large_audio=bot_api_split_large_audio,
        local_api=local_api,
    )


def _prompt_api_id() -> str:
    while True:
        value = input("Telegram API ID from my.telegram.org: ").strip()
        if value.isascii() and value.isdecimal() and int(value) > 0:
            return value
        print("Telegram API ID must be a positive integer", file=sys.stderr)


def _prompt_api_hash() -> str:
    while True:
        value = getpass.getpass("Telegram API hash (hidden): ")
        if len(value) == 32 and all(character in string.hexdigits for character in value):
            return value
        print(
            "Telegram API hash must be a 32-character hexadecimal value",
            file=sys.stderr,
        )


def _prompt_secret(prompt: str, *, label: str) -> str:
    while True:
        value = getpass.getpass(prompt)
        if value and not any(character.isspace() for character in value):
            return value
        print(f"{label} must be non-empty and contain no whitespace", file=sys.stderr)


def _prompt_plain_value(prompt: str, *, label: str) -> str:
    while True:
        value = input(prompt).strip()
        if value and not any(character.isspace() for character in value):
            return value
        print(f"{label} must be non-empty and contain no whitespace", file=sys.stderr)


def _prompt_telegram_user_id() -> str:
    while True:
        value = input("Numeric Telegram user ID allowed to use /panel: ").strip()
        if value.isascii() and value.isdecimal() and int(value) > 0:
            return value
        print("Telegram user ID must be a positive integer", file=sys.stderr)


def _prompt_api_base() -> str:
    print("The custom endpoint receives the bot token; use only a trusted Bot API server.")
    while True:
        value = input("Existing Bot API base URL: ").strip().rstrip("/")
        if _valid_api_base(value):
            if urlsplit(value).scheme.lower() == "http" and not is_loopback_url(value):
                print(
                    "warning: this non-loopback HTTP endpoint receives the bot token "
                    "without transport encryption",
                    file=sys.stderr,
                )
                confirmation = input("Use this HTTP endpoint anyway? [y/N]: ").strip().lower()
                if confirmation not in {"y", "yes"}:
                    continue
            return value
        print(
            "API base must be an http(s) URL without credentials, whitespace, query, or fragment",
            file=sys.stderr,
        )


def _valid_api_base(value: str) -> bool:
    if not value or any(character.isspace() for character in value):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme in {"http", "https"}
        and hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def _find_systemctl() -> Path:
    candidate = shutil.which("systemctl")
    if not candidate:
        raise SetupError("systemctl is required to register a user service")
    path = Path(candidate).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise SetupError(f"systemctl is not executable: {path}")
    return path


def _find_local_bot_api_executable() -> Path:
    detected = shutil.which("telegram-bot-api")
    if detected:
        try:
            return _validate_local_bot_api_executable(Path(detected))
        except SetupError as exc:
            print(f"detected telegram-bot-api is not usable: {exc}", file=sys.stderr)

    print("Install the official telegram-bot-api binary first; setup never downloads it.")
    while True:
        value = input("Absolute path to telegram-bot-api executable: ").strip()
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            print("telegram-bot-api path must be absolute", file=sys.stderr)
            continue
        try:
            return _validate_local_bot_api_executable(candidate)
        except SetupError as exc:
            print(str(exc), file=sys.stderr)


def _validate_local_bot_api_executable(path: Path) -> Path:
    if not path.is_absolute():
        raise SetupError("telegram-bot-api path must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SetupError(f"telegram-bot-api executable not found: {path}") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise SetupError(f"telegram-bot-api is not an executable file: {resolved}")
    try:
        result = subprocess.run(
            [str(resolved), "--help"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(f"could not inspect telegram-bot-api executable: {resolved}") from exc
    help_text = f"{result.stdout}\n{result.stderr}"
    missing = [flag for flag in LOCAL_API_REQUIRED_FLAGS if flag not in help_text]
    if result.returncode != 0 or missing:
        detail = ", ".join(missing) if missing else f"exit code {result.returncode}"
        raise SetupError(f"executable does not match the official telegram-bot-api CLI ({detail})")
    return resolved


def _assert_local_port_available() -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((LOCAL_API_HOST, LOCAL_API_PORT))
    except OSError as exc:
        raise SetupError(f"{LOCAL_API_HOST}:{LOCAL_API_PORT} is already in use") from exc


def _wait_local_api_ready(
    *,
    timeout_seconds: float = LOCAL_API_READY_TIMEOUT_SECONDS,
    poll_interval_seconds: float = LOCAL_API_READY_POLL_SECONDS,
) -> None:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    connect_timeout = min(
        LOCAL_API_CONNECT_TIMEOUT_SECONDS,
        max(0.05, timeout_seconds),
    )
    last_error: OSError | None = None

    while True:
        try:
            with socket.create_connection(
                (LOCAL_API_HOST, LOCAL_API_PORT),
                timeout=connect_timeout,
            ):
                return
        except OSError as exc:
            last_error = exc

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            detail = f": {last_error}" if last_error else ""
            raise SetupError(
                f"local Bot API did not become ready at {LOCAL_API_HOST}:{LOCAL_API_PORT} "
                f"within {timeout_seconds:g} seconds{detail}"
            ) from last_error
        time.sleep(min(poll_interval_seconds, remaining))


def _install_local_api(setup: LocalApiSetup) -> _InstalledLocalApi:
    paths = setup.paths
    _require_unused_target(paths.credentials, "local Bot API credentials")
    _require_unused_target(paths.unit, "local Bot API user unit")
    _assert_local_port_available()

    _ensure_owned_private_directory(paths.credentials.parent)
    _ensure_directory(paths.unit.parent)
    _ensure_owned_private_directory(paths.data_dir)
    _ensure_owned_private_directory(paths.temp_dir)

    credentials_created = False
    unit_created = False
    reload_attempted = False
    enable_attempted = False
    try:
        _write_private_file(
            paths.credentials,
            _render_local_api_credentials(setup).encode("utf-8"),
        )
        credentials_created = True
        _write_private_file(paths.unit, _render_local_api_unit(setup).encode("utf-8"))
        unit_created = True
        reload_attempted = True
        _run_systemctl_user(setup.systemctl, "daemon-reload")
        enable_attempted = True
        _run_systemctl_user(setup.systemctl, "enable", "--now", LOCAL_API_UNIT)
        _run_systemctl_user(setup.systemctl, "is-active", "--quiet", LOCAL_API_UNIT)
        _wait_local_api_ready()
        return _InstalledLocalApi(setup=setup)
    except BaseException as exc:
        cleanup_issues: list[str] = []
        if enable_attempted:
            result = _run_systemctl_user(
                setup.systemctl,
                "disable",
                "--now",
                LOCAL_API_UNIT,
                check=False,
            )
            cleanup_issues.extend(
                _systemctl_cleanup_issue("disable --now", result)
            )
        if unit_created:
            cleanup_issues.extend(_remove_created_file(paths.unit, label="user unit"))
        if credentials_created:
            cleanup_issues.extend(
                _remove_created_file(paths.credentials, label="credentials file")
            )
        if reload_attempted:
            reload_result = _run_systemctl_user(
                setup.systemctl,
                "daemon-reload",
                check=False,
            )
            cleanup_issues.extend(
                _systemctl_cleanup_issue("daemon-reload", reload_result)
            )
        if cleanup_issues:
            raise SetupError(
                f"could not register local Bot API user service: {exc}; "
                "rollback incomplete; manual cleanup required: "
                + "; ".join(cleanup_issues)
            ) from exc
        if isinstance(exc, (EOFError, KeyboardInterrupt, SetupError)):
            raise
        raise SetupError(f"could not register local Bot API user service: {exc}") from exc


def _remove_local_api(setup: LocalApiSetup) -> list[str]:
    issues: list[str] = []
    result = _run_systemctl_user(
        setup.systemctl,
        "disable",
        "--now",
        LOCAL_API_UNIT,
        check=False,
    )
    issues.extend(_systemctl_cleanup_issue("disable --now", result))
    issues.extend(_remove_created_file(setup.paths.unit, label="user unit"))
    issues.extend(
        _remove_created_file(setup.paths.credentials, label="credentials file")
    )
    reload_result = _run_systemctl_user(
        setup.systemctl,
        "daemon-reload",
        check=False,
    )
    issues.extend(_systemctl_cleanup_issue("daemon-reload", reload_result))
    return issues


def _systemctl_cleanup_issue(
    action: str,
    result: subprocess.CompletedProcess[str],
) -> list[str]:
    if result.returncode == 0:
        return []
    detail = (result.stderr or result.stdout).strip()[:500]
    suffix = f": {detail}" if detail else ""
    return [f"systemctl --user {action} failed with code {result.returncode}{suffix}"]


def _remove_created_file(path: Path, *, label: str) -> list[str]:
    try:
        path.unlink()
    except FileNotFoundError:
        return []
    except OSError as exc:
        return [f"could not remove {label} {path}: {exc}"]
    return []


def _run_systemctl_user(
    systemctl: Path,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            [str(systemctl), "--user", *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if check:
            raise SetupError(f"systemctl --user {' '.join(arguments)} failed: {exc}") from exc
        return subprocess.CompletedProcess([], 1, "", str(exc))
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:1000]
        suffix = f": {detail}" if detail else ""
        raise SetupError(
            f"systemctl --user {' '.join(arguments)} failed with code {result.returncode}{suffix}"
        )
    return result


def _render_local_api_credentials(setup: LocalApiSetup) -> str:
    return (
        "# Private credentials for the official telegram-bot-api server.\n"
        f"TELEGRAM_API_ID={setup.api_id}\n"
        f"TELEGRAM_API_HASH={setup.api_hash}\n"
    )


def _render_local_api_unit(setup: LocalApiSetup) -> str:
    paths = setup.paths
    exec_arguments = (
        _unit_exec_quote(str(setup.executable)),
        "--local",
        f"--http-ip-address={LOCAL_API_HOST}",
        f"--http-port={LOCAL_API_PORT}",
        _unit_exec_quote(f"--dir={paths.data_dir}"),
        _unit_exec_quote(f"--temp-dir={paths.temp_dir}"),
    )
    return (
        "[Unit]\n"
        "Description=Telegram Bot API server for asmr-tg-backup\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=exec\n"
        f"EnvironmentFile={_unit_environment_file_path(paths.credentials)}\n"
        f"ExecStart={' '.join(exec_arguments)}\n"
        "Restart=on-failure\n"
        "RestartSec=5s\n"
        "NoNewPrivileges=true\n"
        "UMask=0077\n"
        "TimeoutStopSec=30s\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _write_setup_config(output_path: Path, setup: SetupAnswers) -> None:
    split_enabled = "true" if setup.bot_api_split_large_audio else "false"
    private_pair = ""
    if setup.mtproto_api_id is not None:
        private_pair = (
            f"api_id = {setup.mtproto_api_id}\n"
            f"api_hash = {_toml_string(setup.mtproto_api_hash)}\n"
        )
    content = (
        "# Generated by `asmr-tg-backup setup`. Keep this file private.\n"
        f"# Setup profile: {setup.profile}\n\n"
        "[app]\n"
        f"data_dir = {_toml_string(str(default_data_path()))}\n\n"
        "[telegram]\n"
        "enabled = true\n"
        f"bot_token = {_toml_string(setup.bot_token)}\n"
        f"chat_id = {_toml_string(setup.chat_id)}\n"
        f"upload_transport = {_toml_string(setup.upload_transport)}\n"
        'media_type = "audio"\n'
        "send_as_document = false\n"
        "upload_timeout_seconds = 7200\n\n"
        "[telegram.mtproto]\n"
        f"{private_pair}"
        f"session_path = {_toml_string(str(default_data_path() / 'telegram-mtproto.session'))}\n"
        "max_upload_bytes = 1990000000\n\n"
        "[telegram.bot_api]\n"
        f"api_base = {_toml_string(setup.api_base)}\n"
        f"max_upload_bytes = {setup.bot_api_max_upload_bytes}\n"
        f"split_large_audio = {split_enabled}\n"
        "max_upload_parts = 10\n\n"
        "[control]\n"
        "enabled = true\n"
        "allow_disk_delete = false\n"
        f"allowed_user_ids = [{_toml_string(setup.allowed_user_id)}]\n"
        "allowed_chat_ids = []\n"
        "allowed_message_thread_ids = []\n"
    ).encode("utf-8")
    _write_private_file(output_path, content)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _write_private_file(path: Path, content: bytes) -> None:
    _ensure_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise SetupTargetExistsError(f"refusing to overwrite existing file: {path}") from exc
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        path.chmod(0o600)
    except BaseException as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        cleanup_issues = _remove_created_file(path, label="partial private file")
        if cleanup_issues:
            raise SetupError(
                f"could not finish private file {path}: {exc}; "
                "rollback incomplete; manual cleanup required: "
                + "; ".join(cleanup_issues)
            ) from exc
        raise


def _ensure_directory(path: Path) -> None:
    already_exists = path.exists()
    previous_umask = os.umask(0o077)
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    finally:
        os.umask(previous_umask)
    if already_exists:
        return
    try:
        path.chmod(0o700)
    except OSError as exc:
        raise SetupError(f"could not make directory private: {path}: {exc}") from exc


def _ensure_owned_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise SetupError(f"refusing symlink for private directory: {path}")
    _ensure_directory(path)
    try:
        path.chmod(0o700)
    except OSError as exc:
        raise SetupError(f"could not make directory private: {path}: {exc}") from exc


def _require_unused_target(path: Path, label: str) -> None:
    if path.exists() or path.is_symlink():
        raise SetupTargetExistsError(f"refusing to overwrite existing {label}: {path}")


def _unit_value_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def _unit_environment_file_path(path: Path) -> str:
    """Render an EnvironmentFile path without systemd treating quotes literally."""
    value = str(path)
    if not path.is_absolute():
        raise SetupError(f"systemd EnvironmentFile path must be absolute: {path}")
    if any(character in value for character in ("\x00", "\r", "\n", "*", "?", "[", "]")):
        raise SetupError(f"unsupported character in systemd EnvironmentFile path: {path}")

    escaped: list[str] = []
    for character in value:
        if character == "%":
            escaped.append("%%")
        elif character == " ":
            escaped.append("\\x20")
        elif character == "\t":
            escaped.append("\\x09")
        elif character == "\\":
            escaped.append("\\x5c")
        elif character in {'"', "'"}:
            escaped.append(f"\\x{ord(character):02x}")
        else:
            escaped.append(character)
    return "".join(escaped)


def _unit_exec_quote(value: str) -> str:
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise SetupError("unsupported control character in systemd ExecStart argument")
    return _unit_value_quote(value.replace("$", "$$"))


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser().resolve()


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or "~/.local/share").expanduser().resolve()
