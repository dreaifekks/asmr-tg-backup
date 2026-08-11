from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import re
import secrets
import shlex
from typing import Any
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from .config import Config
from .extension_api import HttpRequest, RouteRequest
from .extensions import SourceProviderCatalog
from .models import Origin
from .network import ConnectionRuntime, NetworkScope, is_loopback_url
from .source_filter import (
    DEFAULT_SOURCE_FILTER_PATTERN,
    SOURCE_FILTER_STATE_KEY,
    compile_source_filter,
    format_source_filter,
)
from .source_catalog import (
    SourceCatalogError,
    SourceCatalogManager,
    normalized_source_identity,
)
from .store import Store
from .youtube import resolve_channel_id


PANEL_STATE_PREFIX = "control_panel_v1"
PANEL_PAGE_SIZE = 6
RESOURCE_PAGE_SIZE = 6
PANEL_SNAPSHOT_MAX_AGE_SECONDS = 30
PANEL_REVISION_SEPARATOR = "~"
TWITCH_KINDS = {"vods", "highlights", "uploads"}
TWITCH_LOGIN_PATTERN = re.compile(r"[a-zA-Z0-9_]{1,25}")
BUILTIN_PANEL_PROVIDERS = frozenset({"youtube", "twitch", "rss"})


class ControlBot:
    def __init__(
        self,
        config: Config,
        store: Store,
        logger: logging.Logger,
        connection: ConnectionRuntime | None = None,
        providers=None,
    ):
        self.config = config
        self.store = store
        self.logger = logger
        self.connection = connection
        self.source_catalog = SourceCatalogManager(
            config.sources.path,
            store,
            max_failures=config.app.max_attempts,
            providers=providers,
        )
        self._source_catalog_ready = False

    def process_once(self, timeout_seconds: int | None = None) -> None:
        if not self.config.control.enabled:
            return
        if not self.config.telegram.bot_token:
            self.logger.warning("control enabled but telegram.bot_token is empty")
            return

        offset = self.store.get_bot_offset()
        long_poll_seconds = max(
            1,
            min(
                30,
                int(
                    self.config.control.poll_interval_seconds
                    if timeout_seconds is None
                    else timeout_seconds
                ),
            ),
        )
        payload: dict[str, Any] = {
            "timeout": long_poll_seconds,
            "limit": 20,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset:
            payload["offset"] = offset + 1
        response = self._api(
            "getUpdates",
            payload,
            request_timeout_seconds=long_poll_seconds + 5,
        )
        for update in response.get("result", []):
            update_id = int(update["update_id"])
            try:
                self._handle_update(update)
            finally:
                self.store.set_bot_offset(update_id)
        self.expire_idle_panels()

    def register_commands(self) -> None:
        if not self.config.control.enabled or not self.config.telegram.bot_token:
            return
        if self.config.control.delete_webhook_on_startup:
            self._api("deleteWebhook", {"drop_pending_updates": False})
        self._api(
            "setMyCommands",
            {
                "commands": [
                    {"command": "start", "description": "Open control panel"},
                    {"command": "panel", "description": "Open control panel"},
                    {"command": "origin", "description": "Manage media origins"},
                    {"command": "source_filter", "description": "Filter sources by regex"},
                    {"command": "stats", "description": "Show backup counts"},
                    {"command": "help", "description": "Show command help"},
                ]
            },
        )

    def _handle_update(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query")
        if callback:
            self._handle_callback(callback)
            return
        message = update.get("message")
        if not message:
            return
        text = str(message.get("text") or "").strip()
        if not text:
            return
        if not text.startswith("/"):
            self._handle_panel_input(message, text)
            return
        if not self._authorized(message):
            self.logger.warning("unauthorized control command from %s", self._principal(message))
            self._reply(message, "unauthorized")
            return
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        if command in {"/start", "/panel"}:
            self._open_panel(message)
            return
        if command == "/cancel" and self._cancel_panel_input(message):
            return
        try:
            reply = self._execute(text, message)
        except Exception as exc:
            self.logger.exception("control command failed")
            reply = f"error: {exc}"
        self._reply(message, reply)

    def _execute(self, text: str, message: dict[str, Any]) -> str:
        parts = shlex.split(text)
        if not parts:
            return self._help()
        command = parts[0].split("@", 1)[0].lower()
        args = parts[1:]
        if command in {"/help", "/start", "/panel"}:
            return self._help()
        if command == "/origin":
            return self._origin(args, message)
        if command in {"/source_filter", "/filter"}:
            return self._source_filter(args)
        if command == "/stats":
            return self._stats()
        return self._help()

    def _origin(self, args: list[str], message: dict[str, Any]) -> str:
        if not args or args[0].lower() in {"list", "ls"}:
            return self._origin_list()
        action = args[0].lower()
        rest = args[1:]
        if action == "add":
            return self._origin_add(rest, message)
        if action in {"enable", "on", "disable", "off"}:
            if len(rest) != 1:
                return f"usage: /origin {action} <origin_id>"
            enabled = action in {"enable", "on"}
            self._ensure_source_catalog()
            row = next(
                (
                    item
                    for item in self.store.list_origin_statuses()
                    if item["id"] == rest[0] and item["managed_by"] == "catalog"
                ),
                None,
            )
            if enabled and row is not None and row["provider"] == "twitch" and not self._twitch_credentials_ready():
                return "cannot enable Twitch origin until service credentials are configured"
            try:
                self.source_catalog.set_enabled(rest[0], enabled)
                return f"{'enabled' if enabled else 'disabled'}: {rest[0]}"
            except SourceCatalogError as exc:
                return f"error: {exc}"
        if action in {"mode", "recording_mode"}:
            if len(rest) != 2 or rest[1].lower() not in {"vod", "live"}:
                return "usage: /origin mode <origin_id> <vod|live>"
            mode = rest[1].lower()
            try:
                self._ensure_source_catalog()
                self.source_catalog.set_recording_mode(rest[0], mode)
                return f"recording mode={mode}: {rest[0]}"
            except SourceCatalogError as exc:
                return f"error: {exc}"
        if action in {"rename", "name"}:
            if len(rest) < 2:
                return "usage: /origin rename <origin_id> <name>"
            origin_id = rest[0]
            name = " ".join(rest[1:]).strip()
            try:
                self._ensure_source_catalog()
                self.source_catalog.rename(origin_id, name)
                return f"renamed: {origin_id} -> {name}"
            except SourceCatalogError as exc:
                return f"error: {exc}"
        if action in {"history", "backfill", "import-history", "import_history"}:
            if len(rest) != 1:
                return "usage: /origin history <origin_id>"
            try:
                self._ensure_source_catalog()
                self.source_catalog.request_backfill(rest[0])
                return f"historical import requested: {rest[0]} (latest -> all)"
            except SourceCatalogError as exc:
                return f"error: {exc}"
        if action in {"del", "delete", "rm", "remove"}:
            if len(rest) != 1:
                return "usage: /origin del <origin_id>"
            try:
                self._ensure_source_catalog()
                self.source_catalog.delete(rest[0])
                return f"deleted origin: {rest[0]}"
            except SourceCatalogError as exc:
                return f"error: {exc}"
        return self._origin_usage()

    def _origin_add(
        self,
        args: list[str],
        message: dict[str, Any],
        *,
        recording_mode: str | None = None,
    ) -> str:
        self._ensure_source_catalog()
        if len(args) < 2:
            return self._origin_usage()
        provider = args[0].lower()
        remaining = list(args[1:])
        if provider == "youtube":
            kind = "uploads"
            if remaining and remaining[0].lower() == "uploads":
                remaining = remaining[1:]
            if not remaining:
                return "usage: /origin add youtube [uploads] <@handle|channel_id> [name]"
            source_ref = remaining[0]
            if self.connection is None:
                external_id = resolve_channel_id(
                    source_ref,
                    self.config.download.yt_dlp,
                )
            else:
                external_id = resolve_channel_id(
                    source_ref,
                    self.config.download.yt_dlp,
                    self.connection,
                )
        elif provider == "twitch":
            kind = "vods"
            if remaining and remaining[0].lower() in TWITCH_KINDS:
                kind = remaining[0].lower()
                remaining = remaining[1:]
            if not remaining:
                return "usage: /origin add twitch [vods|highlights|uploads] <login|user_id> [name]"
            source_ref = remaining[0]
            external_id = _normalize_twitch_source(source_ref)
        else:
            if provider not in self._panel_extension_providers():
                available = ", ".join(
                    ("youtube", "twitch", *self._panel_extension_providers())
                )
                return (
                    f"unsupported panel source provider: {provider}; "
                    f"available: {available}"
                )
            definition = self.source_catalog.providers.definition(provider)
            kind = definition.default_kind
            source_ref = remaining[0].strip()
            if not source_ref:
                return f"usage: /origin add {provider} <external_id> [name]"
            external_id = source_ref

        effective_recording_mode: str | None = None
        if provider == "twitch" and kind == "vods":
            effective_recording_mode = (
                recording_mode or self.config.twitch.recording_mode
            ).lower().strip()
            if effective_recording_mode not in {"vod", "live"}:
                raise ValueError("recording_mode must be 'vod' or 'live'")
        elif recording_mode is not None:
            raise ValueError("recording_mode is only supported for twitch/vods")

        name = " ".join(remaining[1:]).strip() or _default_name(source_ref)
        credentials_ready = self._twitch_credentials_ready()
        enabled = provider != "twitch" or credentials_ready
        extension_provider = provider in self._panel_extension_providers()
        options: dict[str, Any] = (
            {} if extension_provider else {"created_from": "telegram_panel"}
        )
        if effective_recording_mode is not None:
            options["recording_mode"] = effective_recording_mode
        origin = Origin(
            id=_dynamic_origin_id(provider, kind, external_id),
            provider=provider,
            kind=kind,
            name=name,
            external_id=external_id,
            enabled=enabled,
            bootstrap="latest",
            options=options,
        )
        if extension_provider:
            providers = self.source_catalog.providers
            origin = providers.validate_origin(origin)
            if providers.is_live_origin(origin):
                origin = replace(origin, bootstrap="all")

        source_identity = _panel_source_identity(
            origin,
            providers=self.source_catalog.providers,
        )
        existing = next(
            (
                candidate
                for candidate in self.store.list_origins(managed_by="catalog")
                if _panel_source_identity(
                    candidate,
                    providers=self.source_catalog.providers,
                )
                == source_identity
            ),
            None,
        )
        if existing is not None:
            if recording_mode is not None and effective_recording_mode is not None:
                self.source_catalog.set_recording_mode(
                    existing.id,
                    effective_recording_mode,
                )
            self.source_catalog.set_enabled(existing.id, True)
            mode_suffix = (
                f" mode={effective_recording_mode}"
                if recording_mode is not None and effective_recording_mode is not None
                else ""
            )
            return f"already exists; enabled: {existing.id}{mode_suffix}"

        self.source_catalog.add(origin)
        suffix = ""
        if provider == "twitch" and not credentials_ready:
            suffix = "; disabled until Twitch credentials are configured and it is enabled"
        mode_suffix = (
            f" mode={effective_recording_mode}"
            if effective_recording_mode is not None
            else ""
        )
        return (
            f"added: {origin.id} -> {provider}/{kind}:{external_id}"
            f"{mode_suffix}{suffix}"
        )

    def _twitch_credentials_ready(self) -> bool:
        return bool(
            self.config.twitch.client_id
            and (self.config.twitch.access_token or self.config.twitch.client_secret)
        )

    def _effective_twitch_recording_mode(self, row: Any) -> str | None:
        if str(row["provider"]) != "twitch" or str(row["kind"]) != "vods":
            return None
        override: object = None
        try:
            override = row["recording_mode"]
        except (IndexError, KeyError, TypeError):
            try:
                options = json.loads(str(row["options_json"] or "{}"))
            except (IndexError, KeyError, TypeError, json.JSONDecodeError):
                options = {}
            if isinstance(options, dict):
                override = options.get("recording_mode")
        mode = str(override or self.config.twitch.recording_mode).lower().strip()
        return mode if mode in {"vod", "live"} else self.config.twitch.recording_mode

    def _panel_extension_providers(self) -> tuple[str, ...]:
        providers = self.source_catalog.providers
        if providers is None:
            return ()
        return tuple(
            provider
            for provider in providers.providers
            if provider not in BUILTIN_PANEL_PROVIDERS
        )

    def _resolve_panel_provider(self, token: str) -> str:
        matched = tuple(
            provider
            for provider in self._panel_extension_providers()
            if _provider_token(provider) == token
        )
        if len(matched) != 1:
            raise ValueError("source provider is no longer available")
        return matched[0]

    def _extension_provider_button_rows(
        self,
    ) -> list[list[dict[str, str]]]:
        buttons = [
            _button(
                f"➕ {_provider_label(provider)}",
                f"p:addprovider:{_provider_token(provider)}",
            )
            for provider in self._panel_extension_providers()
        ]
        return [buttons[index : index + 2] for index in range(0, len(buttons), 2)]

    def _origin_list(self) -> str:
        self._ensure_source_catalog()
        rows = [
            row
            for row in self.store.list_origin_statuses()
            if row["managed_by"] == "catalog"
        ]
        lines = [f"source_filter={format_source_filter(self._source_filter_pattern())}", "origins:"]
        for row in rows:
            state = "on" if row["enabled"] else "off"
            error = f" error={row['last_error_code']}" if row["last_error_code"] else ""
            mode = self._effective_twitch_recording_mode(row)
            mode_text = f" mode={mode}" if mode else ""
            lines.append(
                f"- {row['id']} [catalog:{state}] {row['provider']}/{row['kind']} "
                f"{row['name']} -> {row['external_id']}{mode_text} "
                f"items={row['item_count']}{error}"
            )
        if len(lines) == 2:
            lines.append("(none)")
        return "\n".join(lines)

    def _origin_usage(self) -> str:
        add_commands = [
            "/origin add youtube <@handle|channel_id> [name]",
            "/origin add twitch [vods|highlights|uploads] <login|user_id> [name]",
            *(
                f"/origin add {provider} <external_id> [name]"
                for provider in self._panel_extension_providers()
            ),
        ]
        return "\n".join(
            [
                "origin commands:",
                *add_commands,
                "/origin list",
                "/origin enable|disable <origin_id>",
                "/origin mode <origin_id> <vod|live>",
                "/origin rename <origin_id> <name>",
                "/origin history <origin_id>",
                "/origin del <origin_id>",
            ]
        )

    def _handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = str(callback.get("id") or "")
        callback_message = callback.get("message") or {}
        message = dict(callback_message)
        message["from"] = callback.get("from") or {}
        if not self._authorized(message):
            self.logger.warning("unauthorized panel callback from %s", self._principal(message))
            self._api(
                "answerCallbackQuery",
                {"callback_query_id": callback_id, "text": "unauthorized", "show_alert": True},
            )
            return
        raw_data = str(callback.get("data") or "")
        state = self._load_panel_state(message)
        callback_message_id = callback_message.get("message_id")
        current_message_id = state.get("message_id")
        if (
            callback_message_id is None
            or current_message_id is None
            or str(callback_message_id) != str(current_message_id)
        ):
            self._answer_inactive_panel_callback(
                callback_id,
                "这个面板不是你当前的会话，请发送 /panel 重新打开。",
            )
            return
        if not bool(state.get("active", True)):
            self._answer_inactive_panel_callback(
                callback_id,
                "控制面板已关闭，请发送 /panel 重新打开。",
            )
            return
        if self._panel_state_is_expired(state):
            self._answer_inactive_panel_callback(
                callback_id,
                "控制面板已过期，请发送 /panel 重新打开。",
            )
            self._close_panel_state(
                self._panel_state_key(message),
                state,
            )
            return
        data, separator, revision = raw_data.rpartition(PANEL_REVISION_SEPARATOR)
        if (
            not separator
            or not revision
            or revision != str(state.get("panel_revision") or "")
        ):
            self._answer_inactive_panel_callback(
                callback_id,
                "控制面板已经刷新，请使用消息上当前显示的按钮。",
            )
            return
        try:
            self._api("answerCallbackQuery", {"callback_query_id": callback_id})
        except Exception as exc:
            # A delayed callback acknowledgement may expire, but the authorized
            # panel action should still be applied and rendered.
            self.logger.info("could not acknowledge panel callback id=%s: %s", callback_id, exc)
        try:
            self._apply_panel_action(data, state, message)
        except Exception as exc:
            self.logger.exception("panel callback failed action=%s", data)
            state["flash_error"] = f"操作失败：{exc}"
        self._render_panel_message(message, state)

    def _apply_panel_action(
        self,
        data: str,
        state: dict[str, Any],
        message: dict[str, Any],
    ) -> None:
        parts = data.split(":")
        if not parts or parts[0] != "p":
            raise ValueError("invalid panel action")
        action = parts[1] if len(parts) > 1 else "home"
        state.pop("flash", None)
        state.pop("flash_error", None)
        if action == "home":
            state.update(
                {
                    "view": "home",
                    "awaiting": None,
                    "twitch_kind": None,
                    "twitch_mode": None,
                    "source_provider": None,
                }
            )
            return
        if action == "refresh":
            self._panel_snapshot(force=True)
            state.update({"view": "home", "awaiting": None})
            return
        if action == "origins":
            page = int(parts[2]) if len(parts) > 2 else 0
            state.update({"view": "origins", "page": max(0, page), "awaiting": None})
            return
        if action == "originsrefresh":
            page = int(parts[2]) if len(parts) > 2 else 0
            self._panel_snapshot(force=True)
            state.update({"view": "origins", "page": max(0, page), "awaiting": None})
            return
        if action == "stats":
            state.update({"view": "stats", "awaiting": None})
            return
        if action == "statsrefresh":
            self._panel_snapshot(force=True)
            state.update({"view": "stats", "awaiting": None})
            return
        if action == "resources":
            page = int(parts[2]) if len(parts) > 2 else 0
            state.update(
                {
                    "view": "resources",
                    "resource_page": max(0, page),
                    "awaiting": None,
                }
            )
            return
        if action == "resourcesrefresh":
            page = int(parts[2]) if len(parts) > 2 else 0
            state.update(
                {
                    "view": "resources",
                    "resource_page": max(0, page),
                    "awaiting": None,
                }
            )
            return
        if action == "ressearch":
            state.update({"view": "input", "awaiting": "resource_search"})
            return
        if action == "resclear":
            state.update(
                {
                    "view": "resources",
                    "resource_page": 0,
                    "resource_query": "",
                    "awaiting": None,
                }
            )
            return
        if action in {"resource", "resdelask", "resdelete"}:
            if len(parts) != 3 or not parts[2].isdigit():
                raise ValueError("invalid resource action")
            artifact_id = int(parts[2])
            if action == "resource":
                resource = self.store.get_disk_resource(
                    artifact_id,
                    self.config.managed_storage_roots,
                )
                if resource is None:
                    raise ValueError("resource no longer exists")
                state.update(
                    {
                        "view": "resource_detail",
                        "target_artifact_id": artifact_id,
                        "awaiting": None,
                    }
                )
                return
            if not self.config.control.allow_disk_delete:
                raise ValueError(
                    "本地删除未启用；请在 [control] 设置 allow_disk_delete=true"
                )
            resource = self.store.get_disk_resource(
                artifact_id,
                self.config.managed_storage_roots,
            )
            if resource is None:
                raise ValueError("resource no longer exists")
            if action == "resdelask":
                if bool(resource["running"]):
                    raise ValueError("资源正在下载或投递，请稍后再试")
                if int(resource["unsafe_file_count"]) > 0:
                    raise ValueError("资源包含受管存储目录外或不安全的路径")
                state.update(
                    {
                        "view": "resource_delete_confirm",
                        "target_artifact_id": artifact_id,
                        "target_resource_revision": resource[
                            "resource_revision"
                        ],
                        "awaiting": None,
                    }
                )
                return
            if artifact_id != int(state.get("target_artifact_id") or -1):
                raise ValueError("delete confirmation no longer matches this resource")
            expected_revision = str(
                state.get("target_resource_revision") or ""
            )
            if not expected_revision:
                raise ValueError("delete confirmation has expired")
            result = self.store.purge_disk_resource(
                artifact_id,
                self.config.managed_storage_roots,
                expected_revision=expected_revision,
            )
            if bool(result["completed"]):
                state.update(
                    {
                        "view": "resources",
                        "awaiting": None,
                        "flash": (
                            f"已删除 {_compact_button_label(str(result['title']))}："
                            f"{result['deleted_files']} 个文件，释放 "
                            f"{_format_bytes(int(result['freed_bytes']))}"
                        ),
                    }
                )
                state.pop("target_artifact_id", None)
                state.pop("target_resource_revision", None)
            else:
                errors = "；".join(str(item) for item in result["errors"])
                state.update(
                    {
                        "view": "resource_delete_confirm",
                        "target_resource_revision": result[
                            "resource_revision"
                        ],
                        "flash_error": (
                            f"仅删除 {result['deleted_files']} 个文件；"
                            f"{result['failed_files']} 个失败"
                            + (f"：{errors}" if errors else "")
                        ),
                    }
                )
            return
        if action == "filter":
            state.update({"view": "filter", "awaiting": None})
            return
        if action == "addyt":
            state.update(
                {
                    "view": "input",
                    "awaiting": "add_youtube",
                    "source_provider": None,
                }
            )
            return
        if action == "addtw":
            state.update(
                {
                    "view": "twitch_kind",
                    "awaiting": None,
                    "twitch_kind": None,
                    "twitch_mode": None,
                    "source_provider": None,
                }
            )
            return
        if action == "addprovider":
            if len(parts) != 3:
                raise ValueError("invalid source provider action")
            provider = self._resolve_panel_provider(parts[2])
            state.update(
                {
                    "view": "input",
                    "awaiting": "add_provider",
                    "source_provider": provider,
                    "twitch_kind": None,
                    "twitch_mode": None,
                }
            )
            return
        if action == "addtwkind":
            if len(parts) != 3 or parts[2] not in TWITCH_KINDS:
                raise ValueError("invalid Twitch source kind")
            kind = parts[2]
            state.update(
                {
                    "view": "twitch_mode" if kind == "vods" else "input",
                    "awaiting": None if kind == "vods" else "add_twitch",
                    "twitch_kind": kind,
                    "twitch_mode": None,
                }
            )
            return
        if action == "addtwmode":
            if len(parts) != 3 or parts[2] not in {"vod", "live"}:
                raise ValueError("invalid Twitch recording mode")
            if state.get("twitch_kind") != "vods":
                raise ValueError("Twitch recording mode is only available for VOD sources")
            state.update(
                {
                    "view": "input",
                    "awaiting": "add_twitch",
                    "twitch_mode": parts[2],
                }
            )
            return
        if action == "filterset":
            state.update({"view": "input", "awaiting": "set_filter"})
            return
        if action == "filteroff":
            self._ensure_source_catalog()
            self.source_catalog.set_filter("")
            state.update({"view": "filter", "awaiting": None, "flash": "过滤器已关闭"})
            return
        if action == "filterreset":
            self._ensure_source_catalog()
            self.source_catalog.set_filter(DEFAULT_SOURCE_FILTER_PATTERN)
            state.update({"view": "filter", "awaiting": None, "flash": "过滤器已恢复默认"})
            return
        if action == "cancel":
            return_view = (
                "resources"
                if state.get("awaiting") == "resource_search"
                else "home"
            )
            state.update(
                {
                    "view": return_view,
                    "awaiting": None,
                    "twitch_kind": None,
                    "twitch_mode": None,
                    "source_provider": None,
                    "flash": "已取消输入",
                }
            )
            return
        if action == "twmode":
            if len(parts) != 4 or parts[3] not in {"vod", "live"}:
                raise ValueError("invalid Twitch mode action")
            row = self._resolve_catalog_origin(parts[2])
            mode = parts[3]
            self._ensure_source_catalog()
            self.source_catalog.set_recording_mode(str(row["id"]), mode)
            self._panel_snapshot(force=True)
            state.update(
                {
                    "view": "origins",
                    "awaiting": None,
                    "flash": (
                        f"{row['name']} 已切换为"
                        f"{'直播中录制' if mode == 'live' else '直播结束后下载'}"
                    ),
                }
            )
            return
        if action in {"toggle", "delask", "delete"}:
            if len(parts) != 3:
                raise ValueError("origin action is missing its token")
            row = self._resolve_catalog_origin(parts[2])
            origin_id = str(row["id"])
            if action == "toggle":
                enabled = not bool(row["enabled"])
                if enabled and row["provider"] == "twitch" and not self._twitch_credentials_ready():
                    raise ValueError("请先在服务环境中配置 Twitch 凭据")
                self._ensure_source_catalog()
                self.source_catalog.set_enabled(origin_id, enabled)
                state.update(
                    {
                        "view": "origins",
                        "awaiting": None,
                        "flash": f"已{'启用' if enabled else '停用'} {row['name']}",
                    }
                )
                return
            if action == "delask":
                state.update(
                    {
                        "view": "delete_confirm",
                        "awaiting": None,
                        "target_token": parts[2],
                    }
                )
                return
            self._ensure_source_catalog()
            self.source_catalog.delete(origin_id)
            state.update(
                {
                    "view": "origins",
                    "awaiting": None,
                    "flash": f"已删除来源 {row['name']}；历史媒体和归档保留",
                }
            )
            return
        raise ValueError("unknown panel action")

    def _handle_panel_input(self, message: dict[str, Any], text: str) -> None:
        if not self._authorized(message):
            return
        state = self._load_panel_state(message)
        if not bool(state.get("active", True)):
            return
        if self._panel_state_is_expired(state):
            self._close_panel_state(
                self._panel_state_key(message),
                state,
            )
            return
        awaiting = state.get("awaiting")
        if not awaiting:
            return
        if text.lower() in {"cancel", "取消"}:
            return_view = (
                "resources"
                if awaiting == "resource_search"
                else "home"
            )
            state.update(
                {
                    "view": return_view,
                    "awaiting": None,
                    "twitch_kind": None,
                    "twitch_mode": None,
                    "source_provider": None,
                    "flash": "已取消输入",
                }
            )
            self._render_panel_message(message, state)
            return
        try:
            if awaiting == "add_youtube":
                args = shlex.split(text)
                reply = self._origin_add(["youtube", *args], message)
                state.update({"view": "origins", "awaiting": None, "flash": reply})
            elif awaiting == "add_twitch":
                args = shlex.split(text)
                kind = str(state.get("twitch_kind") or "")
                if kind not in TWITCH_KINDS:
                    raise ValueError("请先选择 Twitch 来源类型")
                mode = str(state.get("twitch_mode") or "")
                if kind == "vods" and mode not in {"vod", "live"}:
                    raise ValueError("请先选择 Twitch 录制模式")
                reply = self._origin_add(
                    ["twitch", kind, *args],
                    message,
                    recording_mode=mode if kind == "vods" else None,
                )
                state.update(
                    {
                        "view": "origins",
                        "awaiting": None,
                        "twitch_kind": None,
                        "twitch_mode": None,
                        "source_provider": None,
                        "flash": reply,
                    }
                )
            elif awaiting == "add_provider":
                provider = str(state.get("source_provider") or "")
                if provider not in self._panel_extension_providers():
                    raise ValueError("来源插件已停用，请重新打开面板")
                args = shlex.split(text)
                reply = self._origin_add([provider, *args], message)
                state.update(
                    {
                        "view": "origins",
                        "awaiting": None,
                        "source_provider": None,
                        "flash": reply,
                    }
                )
            elif awaiting == "set_filter":
                reply = self._source_filter([text])
                if reply.startswith("error:"):
                    raise ValueError(reply.removeprefix("error: "))
                state.update({"view": "filter", "awaiting": None, "flash": reply})
            elif awaiting == "resource_search":
                query = " ".join(text.split())
                if len(query) > 100:
                    raise ValueError("搜索词最多 100 个字符")
                if query.lower() in {"*", "all"} or query == "全部":
                    query = ""
                state.update(
                    {
                        "view": "resources",
                        "resource_page": 0,
                        "resource_query": query,
                        "awaiting": None,
                        "flash": (
                            f"正在搜索：{query}"
                            if query
                            else "已显示全部本地资源"
                        ),
                    }
                )
            else:
                raise ValueError("unknown pending panel input")
        except Exception as exc:
            state["flash_error"] = f"输入无效：{exc}"
        self._render_panel_message(message, state)

    def _cancel_panel_input(self, message: dict[str, Any]) -> bool:
        state = self._load_panel_state(message)
        if not bool(state.get("active", True)):
            return True
        if self._panel_state_is_expired(state):
            self._close_panel_state(
                self._panel_state_key(message),
                state,
            )
            return True
        if not state.get("awaiting"):
            return False
        return_view = (
            "resources"
            if state.get("awaiting") == "resource_search"
            else "home"
        )
        state.update(
            {
                "view": return_view,
                "awaiting": None,
                "twitch_kind": None,
                "twitch_mode": None,
                "source_provider": None,
                "flash": "已取消输入",
            }
        )
        self._render_panel_message(message, state)
        return True

    def _open_panel(self, message: dict[str, Any]) -> None:
        previous_state = self._load_panel_state(message)
        state = {
            "view": "home",
            "awaiting": None,
            "twitch_kind": None,
            "twitch_mode": None,
            "source_provider": None,
        }
        self._render_panel_message(message, state)
        self._retire_replaced_panel_message(message, previous_state, state)

    def _retire_replaced_panel_message(
        self,
        message: dict[str, Any],
        previous_state: dict[str, Any],
        current_state: dict[str, Any],
    ) -> None:
        previous_message_id = previous_state.get("message_id")
        current_message_id = current_state.get("message_id")
        if (
            previous_message_id is None
            or current_message_id is None
            or str(previous_message_id) == str(current_message_id)
        ):
            return
        chat_id = previous_state.get("chat_id")
        if chat_id is None:
            chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None:
            return
        try:
            self._api(
                "editMessageReplyMarkup",
                {
                    "chat_id": chat_id,
                    "message_id": int(previous_message_id),
                    "reply_markup": _inline_keyboard([]),
                },
            )
        except Exception as exc:
            self.logger.info(
                "could not remove buttons from replaced panel message_id=%s: %s",
                previous_message_id,
                exc,
            )

    def _render_panel_message(self, message: dict[str, Any], state: dict[str, Any]) -> None:
        text, reply_markup = self._render_panel(state)
        revision = secrets.token_hex(4)
        _add_panel_revision(reply_markup, revision)
        state["panel_revision"] = revision
        message_id = state.get("message_id")
        if message_id is not None:
            try:
                self._api(
                    "editMessageText",
                    {
                        "chat_id": (message.get("chat") or {}).get("id"),
                        "message_id": int(message_id),
                        "text": text[:3900],
                        "reply_markup": reply_markup,
                    },
                )
                self._save_panel_state(message, state)
                return
            except RuntimeError as exc:
                if "message is not modified" in str(exc).lower():
                    self._save_panel_state(message, state)
                    return
                self.logger.info("existing panel could not be edited; creating a new panel: %s", exc)

        payload: dict[str, Any] = {
            "chat_id": (message.get("chat") or {}).get("id"),
            "text": text[:3900],
            "reply_markup": reply_markup,
        }
        if message.get("message_thread_id") is not None:
            payload["message_thread_id"] = message["message_thread_id"]
        response = self._api("sendMessage", payload)
        result = response.get("result") or {}
        if result.get("message_id") is None:
            raise RuntimeError("Telegram did not return a panel message_id")
        state["message_id"] = int(result["message_id"])
        self._save_panel_state(message, state)

    def _render_panel(self, state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        flash = str(state.pop("flash", "") or "")
        flash_error = str(state.pop("flash_error", "") or "")
        awaiting = state.get("awaiting")
        if awaiting:
            prompts = {
                "add_youtube": (
                    "添加 YouTube 来源\n\n"
                    "请发送：@handle [显示名称]\n"
                    "也可以发送 UC channel ID。"
                ),
                "add_twitch": (
                    "添加 Twitch 来源\n\n"
                    "请发送：主播登录名 [显示名称]\n"
                    f"来源类型：{_twitch_kind_label(str(state.get('twitch_kind') or ''))}。"
                    + (
                        "\n本频道模式："
                        f"{'直播中录制' if state.get('twitch_mode') == 'live' else '直播结束后下载'}。"
                        if state.get("twitch_kind") == "vods"
                        else ""
                    )
                ),
                "set_filter": (
                    "设置全局来源过滤器\n\n"
                    "请发送正则表达式；匹配不区分大小写。"
                ),
                "resource_search": (
                    "搜索本地资源\n\n"
                    "请发送标题、来源、平台或外部 ID 的关键字。\n"
                    "发送“全部”可清除搜索。"
                ),
            }
            if awaiting == "add_provider":
                provider = str(state.get("source_provider") or "")
                providers = self.source_catalog.providers
                if (
                    providers is None
                    or provider not in self._panel_extension_providers()
                ):
                    text = "来源插件已停用，请取消并重新打开面板。"
                else:
                    kind = providers.default_kind(provider)
                    text = (
                        f"添加 {_provider_label(provider)} 来源\n\n"
                        "请发送：<来源标识> [显示名称]\n"
                        f"来源类型：{provider}/{kind}\n"
                        "来源标识包含空格时，请使用引号包住。"
                    )
            else:
                text = prompts.get(str(awaiting), "等待输入")
            if flash_error:
                text = f"⚠️ {flash_error}\n\n{text}"
            elif flash:
                text = f"⚠️ {flash}\n\n{text}"
            return text, _inline_keyboard([[ _button("取消", "p:cancel") ]])

        view = str(state.get("view") or "home")
        if view == "origins":
            text, keyboard = self._render_origins_panel(state)
        elif view == "resources":
            text, keyboard = self._render_resources_panel(state)
        elif view == "resource_detail":
            text, keyboard = self._render_resource_detail_panel(state)
        elif view == "resource_delete_confirm":
            text, keyboard = self._render_resource_delete_confirm_panel(state)
        elif view == "twitch_kind":
            text, keyboard = self._render_twitch_kind_panel()
        elif view == "twitch_mode":
            text, keyboard = self._render_twitch_mode_panel()
        elif view == "stats":
            text, keyboard = self._render_stats_panel()
        elif view == "filter":
            text, keyboard = self._render_filter_panel()
        elif view == "delete_confirm":
            text, keyboard = self._render_delete_confirm_panel(state)
        else:
            text, keyboard = self._render_home_panel()
        if flash_error:
            text = f"⚠️ {flash_error}\n\n{text}"
        elif flash:
            text = f"✅ {flash}\n\n{text}"
        return text, _inline_keyboard(keyboard)

    def _render_home_panel(self) -> tuple[str, list[list[dict[str, str]]]]:
        snapshot = self._panel_snapshot()
        origins = [
            row
            for row in snapshot["origins"]
            if row["managed_by"] == "catalog"
        ]
        enabled = sum(bool(row["enabled"]) for row in origins)
        summary = snapshot["summary"]
        providers = snapshot["providers"]
        provider_text = ", ".join(f"{key}={value}" for key, value in providers.items()) or "none"
        panel_expiry = (
            f"{self._panel_timeout_text()}无操作后自动关闭"
            if self.config.control.panel_idle_timeout_seconds > 0
            else "不会自动关闭"
        )
        text = "\n".join(
            [
                "🎧 Media Backup 控制面板",
                "",
                f"来源：{enabled}/{len(origins)} 已启用",
                f"媒体：{summary['known']}（{provider_text}）",
                f"已上传：{summary['uploaded']}",
                (
                    f"可用主归档：{summary['file_count']} · "
                    f"{_format_bytes(int(summary['file_bytes']))}"
                ),
                f"失败：{summary['failed']}  阻断：{summary['blocked']}",
                f"过滤器：{format_source_filter(snapshot.get('source_filter_pattern'))}",
                f"面板：{panel_expiry}",
                f"快照：{_format_snapshot_time(str(snapshot['generated_at']))}",
            ]
        )
        keyboard = [
            [_button("📚 来源", "p:origins:0"), _button("💾 本地资源", "p:resources:0")],
            [_button("📊 状态", "p:stats"), _button("🔎 过滤器", "p:filter")],
            [_button("➕ YouTube", "p:addyt"), _button("➕ Twitch", "p:addtw")],
        ]
        keyboard.extend(self._extension_provider_button_rows())
        keyboard.append([_button("🔄 刷新", "p:refresh")])
        return text, keyboard

    def _render_twitch_mode_panel(
        self,
    ) -> tuple[str, list[list[dict[str, str]]]]:
        text = "\n".join(
            [
                "➕ 添加 Twitch 来源",
                "",
                "请选择这个频道的备份方式：",
                "",
                "🔴 直播中录制：检测开播后立即运行 yt-dlp。",
                "📼 结束后下载：等待 Twitch 发布 VOD 后再下载。",
            ]
        )
        return text, [
            [_button("🔴 直播中录制", "p:addtwmode:live")],
            [_button("📼 结束后下载", "p:addtwmode:vod")],
            [_button("🏠 返回", "p:home")],
        ]

    def _render_twitch_kind_panel(
        self,
    ) -> tuple[str, list[list[dict[str, str]]]]:
        text = "\n".join(
            [
                "➕ 添加 Twitch 来源",
                "",
                "请选择需要归档的内容类型：",
                "",
                "📼 VOD：完整直播回放，可选择直播中录制或结束后下载。",
                "✨ Highlights：主播发布的精选片段。",
                "⬆️ Uploads：主播单独上传的视频。",
            ]
        )
        return text, [
            [_button("📼 VOD", "p:addtwkind:vods")],
            [
                _button("✨ Highlights", "p:addtwkind:highlights"),
                _button("⬆️ Uploads", "p:addtwkind:uploads"),
            ],
            [_button("🏠 返回", "p:home")],
        ]

    def _render_origins_panel(
        self,
        state: dict[str, Any],
    ) -> tuple[str, list[list[dict[str, str]]]]:
        rows = [
            row
            for row in self._panel_snapshot()["origins"]
            if row["managed_by"] == "catalog"
        ]
        page_count = max(1, (len(rows) + PANEL_PAGE_SIZE - 1) // PANEL_PAGE_SIZE)
        page = min(max(0, int(state.get("page") or 0)), page_count - 1)
        state["page"] = page
        selected = rows[page * PANEL_PAGE_SIZE : (page + 1) * PANEL_PAGE_SIZE]
        lines = [f"📚 来源列表  {page + 1}/{page_count}", ""]
        keyboard: list[list[dict[str, str]]] = []
        for index, row in enumerate(selected, start=page * PANEL_PAGE_SIZE + 1):
            icon = "✅" if row["enabled"] else "⏸"
            error = f" · ⚠️{row['last_error_code']}" if row["last_error_code"] else ""
            mode = self._effective_twitch_recording_mode(row)
            mode_text = (
                f" · {'🔴 LIVE' if mode == 'live' else '📼 VOD'}"
                if mode
                else ""
            )
            lines.append(
                f"{index}. {icon} {row['name']}\n"
                f"   {row['provider']}/{row['kind']} · catalog{mode_text} "
                f"· items={row['item_count']}{error}"
            )
            token = _origin_token(str(row["id"]))
            toggle = "停用" if row["enabled"] else "启用"
            label = _compact_button_label(str(row["name"]))
            buttons = [
                _button(f"{toggle} {label}", f"p:toggle:{token}"),
            ]
            if mode:
                target_mode = "vod" if mode == "live" else "live"
                buttons.append(
                    _button(
                        "切换为 VOD" if target_mode == "vod" else "切换为 LIVE",
                        f"p:twmode:{token}:{target_mode}",
                    )
                )
            buttons.append(_button("删除", f"p:delask:{token}"))
            keyboard.append(buttons)
        if not selected:
            lines.append("(暂无来源)")
        navigation: list[dict[str, str]] = []
        if page > 0:
            navigation.append(_button("⬅️", f"p:origins:{page - 1}"))
        if page + 1 < page_count:
            navigation.append(_button("➡️", f"p:origins:{page + 1}"))
        if navigation:
            keyboard.append(navigation)
        keyboard.append(
            [_button("➕ YouTube", "p:addyt"), _button("➕ Twitch", "p:addtw")]
        )
        keyboard.extend(self._extension_provider_button_rows())
        keyboard.append(
            [_button("🏠 返回", "p:home"), _button("🔄 刷新", f"p:originsrefresh:{page}")]
        )
        return "\n".join(lines), keyboard

    def _render_resources_panel(
        self,
        state: dict[str, Any],
    ) -> tuple[str, list[list[dict[str, str]]]]:
        query = str(state.get("resource_query") or "").strip()
        page = max(0, int(state.get("resource_page") or 0))
        library = self.store.list_disk_resources(
            self.config.managed_storage_roots,
            limit=RESOURCE_PAGE_SIZE,
            offset=page * RESOURCE_PAGE_SIZE,
            query=query,
        )
        total = int(library["total"])
        page_count = max(1, (total + RESOURCE_PAGE_SIZE - 1) // RESOURCE_PAGE_SIZE)
        page = min(page, page_count - 1)
        if page != int(state.get("resource_page") or 0):
            library = self.store.list_disk_resources(
                self.config.managed_storage_roots,
                limit=RESOURCE_PAGE_SIZE,
                offset=page * RESOURCE_PAGE_SIZE,
                query=query,
            )
        state["resource_page"] = page

        lines = [
            f"💾 本地资源  {page + 1}/{page_count}",
            "",
            (
                f"受管资源：{total} · 登记文件 "
                f"{_format_bytes(int(library['recorded_bytes']))}"
            ),
            f"搜索：{_compact_text(query, 48) if query else '全部'}",
            "",
        ]
        keyboard: list[list[dict[str, str]]] = []
        items = list(library["items"])
        for index, resource in enumerate(
            items,
            start=page * RESOURCE_PAGE_SIZE + 1,
        ):
            status_icon, status_text = _resource_status(resource)
            title = _compact_text(str(resource["title"]), 66)
            date_text = _format_resource_date(
                resource.get("published_at")
                or resource.get("artifact_created_at")
            )
            lines.extend(
                [
                    f"{index}. {status_icon} {title}",
                    (
                        f"   {resource['provider']}/{resource['content_kind']} · "
                        f"{_compact_text(str(resource['origin_name']), 28)} · "
                        f"{date_text}"
                    ),
                    (
                        f"   {_format_bytes(int(resource['recorded_bytes']))} · "
                        f"{status_text}"
                    ),
                ]
            )
            keyboard.append(
                [
                    _button(
                        f"{index}. {_compact_text(str(resource['title']), 34)}",
                        f"p:resource:{resource['artifact_id']}",
                    )
                ]
            )
        if not items:
            lines.append("（没有匹配的受管本地资源）")

        navigation: list[dict[str, str]] = []
        if page > 0:
            navigation.append(_button("⬅️", f"p:resources:{page - 1}"))
        if page + 1 < page_count:
            navigation.append(_button("➡️", f"p:resources:{page + 1}"))
        if navigation:
            keyboard.append(navigation)
        search_row = [_button("🔍 搜索", "p:ressearch")]
        if query:
            search_row.append(_button("清除搜索", "p:resclear"))
        keyboard.append(search_row)
        keyboard.append(
            [
                _button("🏠 返回", "p:home"),
                _button("🔄 刷新", f"p:resourcesrefresh:{page}"),
            ]
        )
        return "\n".join(lines), keyboard

    def _render_resource_detail_panel(
        self,
        state: dict[str, Any],
    ) -> tuple[str, list[list[dict[str, str]]]]:
        artifact_id = int(state.get("target_artifact_id") or -1)
        resource = self.store.get_disk_resource(
            artifact_id,
            self.config.managed_storage_roots,
        )
        page = max(0, int(state.get("resource_page") or 0))
        if resource is None:
            state.update({"view": "resources", "target_artifact_id": None})
            return (
                "💾 该资源已不在本地资源库中。",
                [[_button("返回资源列表", f"p:resources:{page}")]],
            )

        _, status_text = _resource_status(resource)
        delivery = _resource_delivery_text(resource)
        role_counts: dict[str, int] = {}
        for file_info in resource["files"]:
            role = str(file_info["role"])
            role_counts[role] = role_counts.get(role, 0) + 1
        roles = "、".join(
            f"{role}×{count}" if count > 1 else role
            for role, count in role_counts.items()
        ) or "无"
        lines = [
            "🎧 本地资源详情",
            "",
            str(resource["title"]),
            "",
            f"来源：{resource['origin_name']}",
            f"平台：{resource['provider']}/{resource['content_kind']}",
            f"外部 ID：{resource['external_id']}",
            f"发布时间：{_format_resource_date(resource.get('published_at'))}",
            f"归档时间：{_format_resource_date(resource.get('artifact_created_at'))}",
            f"状态：{status_text}；{delivery}",
            (
                f"受管文件：{resource['existing_file_count']} 个 · "
                f"{_format_bytes(int(resource['actual_bytes']))}"
            ),
            f"角色：{roles}",
            f"锚点路径：{_compact_text(str(resource['relative_path']), 180)}",
        ]
        if str(resource.get("anchor_role") or "") == "live_segment":
            lines.append("归档形态：仅有未合并的直播片段（暂无完整备份文件）")
        if int(resource["missing_file_count"]) > 0:
            lines.append(f"缺失记录：{resource['missing_file_count']} 个文件")
        if int(resource["unsafe_file_count"]) > 0:
            lines.append(
                f"安全限制：{resource['unsafe_file_count']} 个路径不可由面板删除"
            )
        if bool(resource["irreplaceable_live"]):
            lines.append("⚠️ 这是直播录制，本地删除后通常无法重新获取。")
        lines.extend(["", str(resource["canonical_url"])])

        keyboard: list[list[dict[str, str]]] = []
        delete_ready = (
            self.config.control.allow_disk_delete
            and not bool(resource["running"])
            and int(resource["unsafe_file_count"]) == 0
        )
        if delete_ready:
            keyboard.append(
                [
                    _button(
                        "🗑 删除本地文件",
                        f"p:resdelask:{artifact_id}",
                    )
                ]
            )
        elif not self.config.control.allow_disk_delete:
            lines.extend(
                [
                    "",
                    "删除保护：当前为只读；需在 [control] 显式设置",
                    "allow_disk_delete = true 后才会显示删除按钮。",
                ]
            )
        elif bool(resource["running"]):
            lines.extend(["", "删除保护：资源正在下载或投递，请稍后再试。"])
        keyboard.append(
            [
                _button("⬅️ 返回列表", f"p:resources:{page}"),
                _button("🏠 首页", "p:home"),
            ]
        )
        return "\n".join(lines), keyboard

    def _render_resource_delete_confirm_panel(
        self,
        state: dict[str, Any],
    ) -> tuple[str, list[list[dict[str, str]]]]:
        artifact_id = int(state.get("target_artifact_id") or -1)
        page = max(0, int(state.get("resource_page") or 0))
        resource = self.store.get_disk_resource(
            artifact_id,
            self.config.managed_storage_roots,
        )
        if resource is None:
            state.update({"view": "resources", "target_artifact_id": None})
            return (
                "💾 该资源已经不在本地资源库中。",
                [[_button("返回资源列表", f"p:resources:{page}")]],
            )

        expected = str(state.get("target_resource_revision") or "")
        current = str(resource["resource_revision"])
        changed = not expected or expected != current
        lines = [
            "⚠️ 永久删除本地备份？",
            "",
            str(resource["title"]),
            "",
            (
                f"{resource['existing_file_count']} 个受管文件 · "
                f"{_format_bytes(int(resource['actual_bytes']))}"
            ),
            f"投递：{_resource_delivery_text(resource)}",
            "",
            "将删除数据库明确登记且位于 downloads/ 内的本机文件。",
            "媒体、来源、任务审计与 Telegram 投递历史会保留。",
            "Telegram 中已经发送的消息不会删除，也不会自动重新下载。",
            "未登记的 sidecar/orphan 文件不会被猜测或连带删除。",
        ]
        if bool(resource["irreplaceable_live"]):
            lines.extend(
                [
                    "",
                    "🚨 这是直播录制，删除后通常无法从平台重新获得。",
                ]
            )
        keyboard: list[list[dict[str, str]]] = []
        if changed:
            lines.extend(
                [
                    "",
                    "资源在确认期间发生了变化；请返回详情后重新确认。",
                ]
            )
        elif bool(resource["running"]):
            lines.extend(["", "资源正在下载或投递，当前不能删除。"])
        elif int(resource["unsafe_file_count"]) > 0:
            lines.extend(["", "资源含不安全路径，面板拒绝执行删除。"])
        elif not self.config.control.allow_disk_delete:
            lines.extend(["", "本地删除开关当前已关闭。"])
        else:
            keyboard.append(
                [
                    _button(
                        (
                            "确认永久删除 "
                            f"{_format_bytes(int(resource['actual_bytes']))}"
                        ),
                        f"p:resdelete:{artifact_id}",
                    )
                ]
            )
        keyboard.append(
            [
                _button("取消并返回详情", f"p:resource:{artifact_id}"),
                _button("返回列表", f"p:resources:{page}"),
            ]
        )
        return "\n".join(lines), keyboard

    def _render_stats_panel(self) -> tuple[str, list[list[dict[str, str]]]]:
        snapshot = self._panel_snapshot()
        summary = snapshot["summary"]
        providers = snapshot["providers"]
        jobs = snapshot["jobs"]
        lines = ["📊 备份状态", "", "媒体："]
        lines.extend(f"- {provider}: {count}" for provider, count in providers.items())
        if not providers:
            lines.append("- none")
        lines.extend(["", "任务："])
        lines.extend(f"- {key}: {count}" for key, count in jobs.items())
        if not jobs:
            lines.append("- none")
        lines.extend(
            [
                "",
                f"stored_bytes={summary['file_bytes']}",
                f"waiting_ready={summary['waiting_ready']}",
                f"failed={summary['failed']} blocked={summary['blocked']}",
                f"snapshot={_format_snapshot_time(str(snapshot['generated_at']))}",
            ]
        )
        return "\n".join(lines), [[_button("🏠 返回", "p:home"), _button("🔄 刷新", "p:statsrefresh")]]

    def _render_filter_panel(self) -> tuple[str, list[list[dict[str, str]]]]:
        text = "\n".join(
            [
                "🔎 全局来源过滤器",
                "",
                f"当前：{format_source_filter(self._source_filter_pattern())}",
                "过滤器会匹配来源 ID、名称或媒体标题。",
            ]
        )
        return text, [
            [_button("设置", "p:filterset"), _button("关闭", "p:filteroff")],
            [_button("恢复默认", "p:filterreset"), _button("🏠 返回", "p:home")],
        ]

    def _render_delete_confirm_panel(
        self,
        state: dict[str, Any],
    ) -> tuple[str, list[list[dict[str, str]]]]:
        token = str(state.get("target_token") or "")
        row = self._resolve_catalog_origin(token)
        text = "\n".join(
            [
                "⚠️ 删除来源？",
                "",
                f"{row['name']} ({row['provider']}/{row['kind']})",
                str(row["external_id"]),
                "",
                "只删除来源配置；历史媒体、归档和投递记录会保留。",
            ]
        )
        return text, [
            [_button("确认删除", f"p:delete:{token}")],
            [_button("取消", "p:origins:0")],
        ]

    def _resolve_catalog_origin(self, token: str) -> Any:
        self._ensure_source_catalog()
        matches = [
            row
            for row in self.store.list_origin_statuses()
            if row["managed_by"] == "catalog" and _origin_token(str(row["id"])) == token
        ]
        if len(matches) != 1:
            raise ValueError("origin no longer exists")
        return matches[0]

    def _load_panel_state(self, message: dict[str, Any]) -> dict[str, Any]:
        raw = self.store.get_bot_state(self._panel_state_key(message))
        if not raw:
            return {"view": "home", "awaiting": None}
        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            return {"view": "home", "awaiting": None}
        return state if isinstance(state, dict) else {"view": "home", "awaiting": None}

    def _save_panel_state(self, message: dict[str, Any], state: dict[str, Any]) -> None:
        now = _utcnow()
        timeout_seconds = self.config.control.panel_idle_timeout_seconds
        state.update(
            {
                "active": True,
                "chat_id": (message.get("chat") or {}).get("id"),
                "message_thread_id": message.get("message_thread_id"),
                "user_id": (message.get("from") or {}).get("id"),
                "last_activity_at": now.isoformat(),
            }
        )
        state.pop("closed_at", None)
        if timeout_seconds > 0:
            state["expires_at"] = (
                now + timedelta(seconds=timeout_seconds)
            ).isoformat()
        else:
            state.pop("expires_at", None)
        self.store.set_bot_state(
            self._panel_state_key(message),
            json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        )

    def expire_idle_panels(self, *, now: datetime | None = None) -> int:
        if self.config.control.panel_idle_timeout_seconds <= 0:
            return 0
        checked_at = now or _utcnow()
        closed = 0
        for key, raw in self.store.list_bot_states(f"{PANEL_STATE_PREFIX}:"):
            try:
                state = json.loads(raw)
            except json.JSONDecodeError:
                self.store.delete_bot_state(key)
                continue
            if not isinstance(state, dict):
                self.store.delete_bot_state(key)
                continue
            if not bool(state.get("active", True)):
                continue
            if not self._panel_state_is_expired(state, now=checked_at):
                continue
            self._close_panel_state(key, state, now=checked_at)
            closed += 1
        return closed

    def _panel_state_is_expired(
        self,
        state: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> bool:
        timeout_seconds = self.config.control.panel_idle_timeout_seconds
        if timeout_seconds <= 0:
            return False
        raw_last_activity = state.get("last_activity_at")
        if not raw_last_activity:
            return bool(state.get("message_id"))
        try:
            last_activity = datetime.fromisoformat(str(raw_last_activity))
        except ValueError:
            return True
        if last_activity.tzinfo is None:
            last_activity = last_activity.replace(tzinfo=timezone.utc)
        deadline = last_activity.astimezone(timezone.utc) + timedelta(
            seconds=timeout_seconds
        )
        return deadline <= (now or _utcnow()).astimezone(timezone.utc)

    def _close_panel_state(
        self,
        key: str,
        state: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> None:
        closed_at = now or _utcnow()
        state.update(
            {
                "active": False,
                "view": "closed",
                "awaiting": None,
                "twitch_kind": None,
                "twitch_mode": None,
                "closed_at": closed_at.isoformat(),
            }
        )
        state.pop("flash", None)
        state.pop("target_token", None)
        self.store.set_bot_state(
            key,
            json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        )

        message_id = state.get("message_id")
        chat_id = state.get("chat_id")
        if chat_id is None:
            chat_id = self._panel_state_chat_id(key)
        if message_id is None or chat_id is None:
            return
        try:
            self._api(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": int(message_id),
                    "text": (
                        "🔒 控制面板已自动关闭\n\n"
                        f"超过 {self._panel_timeout_text()}没有操作，按钮已失效。\n"
                        "发送 /panel 可以重新打开。"
                    ),
                    "reply_markup": _inline_keyboard([]),
                },
            )
        except Exception as exc:
            self.logger.info(
                "could not edit expired panel message key=%s: %s",
                key,
                exc,
            )

    def _answer_inactive_panel_callback(
        self,
        callback_id: str,
        text: str,
    ) -> None:
        try:
            self._api(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_id,
                    "text": text,
                    "show_alert": True,
                },
            )
        except Exception as exc:
            self.logger.info(
                "could not reject inactive panel callback id=%s: %s",
                callback_id,
                exc,
            )

    def _panel_timeout_text(self) -> str:
        seconds = self.config.control.panel_idle_timeout_seconds
        if seconds % 3600 == 0:
            return f"{seconds // 3600} 小时"
        if seconds % 60 == 0:
            return f"{seconds // 60} 分钟"
        return f"{seconds} 秒"

    @staticmethod
    def _panel_state_chat_id(key: str) -> str | None:
        prefix = f"{PANEL_STATE_PREFIX}:"
        if not key.startswith(prefix):
            return None
        parts = key[len(prefix) :].split(":", 2)
        return parts[0] if len(parts) == 3 and parts[0] else None

    @staticmethod
    def _panel_state_key(message: dict[str, Any]) -> str:
        user_id = str((message.get("from") or {}).get("id") or "")
        chat_id = str((message.get("chat") or {}).get("id") or "")
        thread_id = str(message.get("message_thread_id") or "")
        return f"{PANEL_STATE_PREFIX}:{chat_id}:{thread_id}:{user_id}"

    def _source_filter(self, args: list[str]) -> str:
        if not args or args[0].lower() in {"status", "show"}:
            return "\n".join(
                [
                    f"source_filter={format_source_filter(self._source_filter_pattern())}",
                    "usage: /source_filter <regex|off|reset>",
                    "matching is regex-based and case-insensitive",
                ]
            )

        action = args[0].lower()
        if action in {"off", "disable", "disabled", "none", "all", "clear"}:
            self._ensure_source_catalog()
            self.source_catalog.set_filter("")
            return "source_filter=off; all sources enabled"
        if action in {"reset", "default"}:
            self._ensure_source_catalog()
            self.source_catalog.set_filter(DEFAULT_SOURCE_FILTER_PATTERN)
            return f"source_filter={format_source_filter(DEFAULT_SOURCE_FILTER_PATTERN)}"
        if action == "set":
            args = args[1:]
            if not args:
                return "usage: /source_filter set <regex>"

        pattern = " ".join(args)
        try:
            compile_source_filter(pattern)
        except ValueError as exc:
            return f"error: {exc}"
        self._ensure_source_catalog()
        self.source_catalog.set_filter(pattern)
        return f"source_filter={format_source_filter(pattern)}"

    def _ensure_source_catalog(self) -> None:
        if self._source_catalog_ready:
            return
        catalog_existed = self.source_catalog.path.exists()
        self.source_catalog.ensure(
            legacy_origins=self.config.origins,
            legacy_declared=self.config.legacy_sources_declared,
        )
        if catalog_existed and self.config.legacy_sources_declared:
            self.logger.warning(
                "legacy source declarations in config.toml are ignored because "
                "sources.toml already exists"
            )
        self._source_catalog_ready = True

    def _source_filter_pattern(self) -> str | None:
        pattern = self.store.get_bot_state(SOURCE_FILTER_STATE_KEY)
        if pattern is None:
            return DEFAULT_SOURCE_FILTER_PATTERN
        return pattern or None

    def _panel_snapshot(self, *, force: bool = False) -> dict[str, Any]:
        self._ensure_source_catalog()
        return self.store.get_panel_snapshot(
            self._source_filter_pattern(),
            max_age_seconds=PANEL_SNAPSHOT_MAX_AGE_SECONDS,
            force=force,
        )

    def _stats(self) -> str:
        summary = self.store.backup_summary()
        lines = [
            "backup stats:",
            f"known={summary['known']}",
            f"backed_up={summary['backed_up']}",
            f"uploaded={summary['uploaded']}",
            f"downloaded_pending_upload={summary['downloaded']}",
            f"waiting_ready={summary['waiting_ready']}",
            f"ignored={summary['ignored']}",
            f"blocked={summary['blocked']}",
            f"failed={summary['failed']}",
            f"stored_bytes={summary['file_bytes']}",
            "providers:",
        ]
        providers = self.store.counts_by_provider()
        lines.extend(f"  {provider}={count}" for provider, count in providers.items())
        if not providers:
            lines.append("  none")
        return "\n".join(lines)

    def _help(self) -> str:
        extension_origin_commands = [
            f"/origin add {provider} <external_id> [name]"
            for provider in self._panel_extension_providers()
        ]
        return "\n".join(
            [
                "Media audio backup bot",
                "",
                "Recommended:",
                "/panel - open a fresh control panel below this command",
                "",
                "Provider-neutral origins:",
                "/origin add youtube @handle [name]",
                "/origin add twitch [vods|highlights|uploads] login [name]",
                *extension_origin_commands,
                "/origin list",
                "/origin enable|disable <origin_id>",
                "/origin mode <origin_id> <vod|live>",
                "/origin rename <origin_id> <name>",
                "/origin history <origin_id>",
                "/origin del <origin_id>",
                "",
                "Other commands:",
                "/source_filter <regex|off|reset>",
                "/stats",
                "",
                "Default source filter is /ASMR/i. Matching is regex-based and case-insensitive.",
                "Twitch credentials stay in the service environment, never in Telegram.",
            ]
        )

    def _authorized(self, message: dict[str, Any]) -> bool:
        control = self.config.control
        checks = (
            (control.allowed_user_ids, str((message.get("from") or {}).get("id") or "")),
            (control.allowed_chat_ids, str((message.get("chat") or {}).get("id") or "")),
            (control.allowed_message_thread_ids, str(message.get("message_thread_id") or "")),
        )
        if not any(allowed_ids for allowed_ids, _ in checks):
            return False
        return all(not allowed_ids or actual_id in allowed_ids for allowed_ids, actual_id in checks)

    def _principal(self, message: dict[str, Any]) -> str:
        from_id = str((message.get("from") or {}).get("id") or "")
        chat_id = str((message.get("chat") or {}).get("id") or "")
        thread_id = str(message.get("message_thread_id") or "")
        return f"user={from_id} chat={chat_id} thread={thread_id}"

    def _reply(self, message: dict[str, Any], text: str) -> None:
        chat = message.get("chat") or {}
        payload: dict[str, Any] = {"chat_id": chat.get("id"), "text": text[:3900]}
        if message.get("message_thread_id") is not None:
            payload["message_thread_id"] = message["message_thread_id"]
        self._api("sendMessage", payload)

    def _api(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        request_timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        api_base = (
            self.config.control.api_base
            or self.config.telegram.bot_api.api_base.rstrip("/")
        )
        endpoint = f"{api_base}/bot{self.config.telegram.bot_token}/{method}"
        body = json.dumps(payload).encode("utf-8")
        if self.connection is None:
            request = Request(
                endpoint,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                if is_loopback_url(api_base):
                    open_request = build_opener(ProxyHandler({})).open
                else:
                    open_request = urlopen
                with open_request(request, timeout=request_timeout_seconds) as response:
                    parsed = json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"Telegram API HTTP {exc.code}: {error_body[:500]}"
                ) from exc
        else:
            response = self.connection.request(
                HttpRequest(
                    url=endpoint,
                    method="POST",
                    headers={"Content-Type": "application/json"},
                    body=body,
                    timeout_seconds=request_timeout_seconds,
                ),
                RouteRequest(
                    scope=(
                        NetworkScope.TELEGRAM_CONTROL_RECEIVE
                        if method == "getUpdates"
                        else NetworkScope.TELEGRAM_CONTROL_SEND
                    ),
                    target_url=endpoint,
                    phase="receive" if method == "getUpdates" else "sending",
                    idempotent=method in {
                        "getUpdates",
                        "getMe",
                        "getChat",
                        "answerCallbackQuery",
                    },
                ),
            )
            if not 200 <= response.status < 300:
                error_body = response.body.decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"Telegram API HTTP {response.status}: {error_body[:500]}"
                )
            parsed = json.loads(response.body.decode("utf-8"))
        if not parsed.get("ok"):
            raise RuntimeError(f"Telegram API error: {parsed}")
        return parsed


def _format_snapshot_time(value: str) -> str:
    try:
        return value.replace("+00:00", "Z").split("T", 1)[1].split(".", 1)[0] + " UTC"
    except (AttributeError, IndexError):
        return value


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validate_id(value: str) -> str:
    if not value or any(char.isspace() for char in value) or len(value) > 100:
        raise ValueError("subscription id must be 1-100 non-whitespace characters")
    return value


def _default_name(channel_id: str) -> str:
    return channel_id[1:] if channel_id.startswith("@") else channel_id


def _normalize_twitch_source(value: str) -> str:
    candidate = value.strip().removeprefix("@").lower()
    if candidate.isdigit():
        return candidate
    if not TWITCH_LOGIN_PATTERN.fullmatch(candidate):
        raise ValueError("Twitch source must be a numeric user ID or a 1-25 character login")
    return candidate


def _twitch_kind_label(kind: str) -> str:
    return {
        "vods": "VOD",
        "highlights": "Highlights",
        "uploads": "Uploads",
    }.get(kind, kind or "未选择")


def _dynamic_origin_id(provider: str, kind: str, external_id: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", external_id).strip("-").lower()[:32] or "origin"
    digest = hashlib.sha256(f"{provider}\0{kind}\0{external_id}".encode("utf-8")).hexdigest()[:10]
    return _validate_id(f"source:{provider}:{kind}:{slug}-{digest}")


def _panel_source_identity(
    origin: Origin,
    *,
    providers: SourceProviderCatalog | None = None,
) -> tuple[str, str, str, str]:
    """Compare Panel sources using the catalog's provider-specific rules.

    Twitch VOD recording mode is intentionally excluded: selecting a new mode
    in the Panel updates the existing source instead of adding a parallel row.
    """

    identity = normalized_source_identity(origin, providers)
    if identity[0] == "twitch" and identity[1] == "vods":
        return identity[:3] + ("",)
    return identity


def _provider_token(provider: str) -> str:
    return hashlib.sha256(provider.encode("utf-8")).hexdigest()[:16]


def _provider_label(provider: str) -> str:
    words = [word for word in re.split(r"[._-]+", provider) if word]
    if not words:
        return "Provider"
    return " ".join(word[:1].upper() + word[1:] for word in words)


def _origin_token(origin_id: str) -> str:
    return hashlib.sha256(origin_id.encode("utf-8")).hexdigest()[:16]


def _compact_button_label(value: str) -> str:
    return " ".join(value.replace("\n", " ").split())[:24] or "origin"


def _compact_text(value: str, limit: int) -> str:
    compact = " ".join(value.replace("\n", " ").split())
    if len(compact) <= limit:
        return compact or "（未命名）"
    return compact[: max(1, limit - 1)].rstrip() + "…"


def _format_bytes(value: int) -> str:
    size = max(0, int(value))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(size)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{size} B"


def _format_resource_date(value: object) -> str:
    if not value:
        return "未知"
    text = str(value)
    return text[:10] if len(text) >= 10 else text


def _resource_delivery_text(resource: dict[str, object]) -> str:
    if bool(resource.get("delivered")):
        return "已投递 Telegram"
    state = str(resource.get("delivery_job_state") or "")
    return {
        "queued": "等待 Telegram 投递",
        "retry": "Telegram 投递待重试",
        "running": "正在投递 Telegram",
        "uncertain": "Telegram 投递结果不确定",
        "blocked": "Telegram 投递已阻断",
        "cancelled": "Telegram 投递已取消",
    }.get(state, "仅本地")


def _resource_status(
    resource: dict[str, object],
) -> tuple[str, str]:
    state = str(resource.get("master_state") or "")
    if state == "purging":
        return "⏳", "正在清理"
    if state == "purge_failed":
        return "⚠️", "上次清理未完成"
    if not bool(resource.get("master_safe")):
        return "🔒", "路径不安全，仅可查看"
    if not bool(resource.get("master_exists")):
        return "⚠️", "锚点文件缺失"
    if bool(resource.get("running")):
        return "🔄", "后台处理中"
    if str(resource.get("anchor_role") or "") == "live_segment":
        return "🚨", "仅有未合并直播片段"
    if bool(resource.get("delivered")):
        return "✅", "已投递 Telegram"
    if state == "suppressed":
        return "🟡", "本地保留，投递已抑制"
    if state == "staged":
        return "🟡", "本地暂存"
    return "💾", _resource_delivery_text(resource)


def _button(text: str, callback_data: str) -> dict[str, str]:
    if len(callback_data.encode("utf-8")) > 64:
        raise ValueError("Telegram callback_data exceeds 64 bytes")
    return {"text": text, "callback_data": callback_data}


def _inline_keyboard(rows: list[list[dict[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": rows}


def _add_panel_revision(reply_markup: dict[str, Any], revision: str) -> None:
    for row in reply_markup.get("inline_keyboard", []):
        for button in row:
            callback_data = button.get("callback_data")
            if callback_data is None:
                continue
            revised = (
                f"{callback_data}{PANEL_REVISION_SEPARATOR}{revision}"
            )
            if len(revised.encode("utf-8")) > 64:
                raise ValueError("Telegram callback_data exceeds 64 bytes")
            button["callback_data"] = revised
