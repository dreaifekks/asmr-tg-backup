from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import re
import secrets
import shlex
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .config import Config
from .models import Origin
from .source_filter import (
    DEFAULT_SOURCE_FILTER_PATTERN,
    SOURCE_FILTER_STATE_KEY,
    compile_source_filter,
    format_source_filter,
)
from .store import Store
from .youtube import resolve_channel_id


PANEL_STATE_PREFIX = "control_panel_v1"
PANEL_PAGE_SIZE = 6
PANEL_SNAPSHOT_MAX_AGE_SECONDS = 30
PANEL_REVISION_SEPARATOR = "~"
TWITCH_KINDS = {"vods", "highlights", "uploads"}
TWITCH_LOGIN_PATTERN = re.compile(r"[a-zA-Z0-9_]{1,25}")


class ControlBot:
    def __init__(self, config: Config, store: Store, logger: logging.Logger):
        self.config = config
        self.store = store
        self.logger = logger

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
                    {"command": "sub", "description": "YouTube compatibility commands"},
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
        if command == "/sub":
            return self._sub(args, message)
        if command in {"/sub_add", "/add"}:
            return self._sub_add(args, message)
        if command in {"/sub_del", "/del"}:
            return self._sub_del(args)
        if command in {"/sub_list", "/list"}:
            return self._sub_list()
        if command in {"/source_filter", "/filter"}:
            return self._source_filter(args)
        if command in {"/stats", "/count"}:
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
            row = next(
                (item for item in self.store.list_origin_statuses() if item["id"] == rest[0]),
                None,
            )
            if enabled and row is not None and row["provider"] == "twitch" and not self._twitch_credentials_ready():
                return "cannot enable Twitch origin until service credentials are configured"
            if self.store.set_control_origin_enabled(rest[0], enabled):
                return f"{'enabled' if enabled else 'disabled'}: {rest[0]}"
            return f"not found or config-managed: {rest[0]}"
        if action in {"mode", "recording_mode"}:
            if len(rest) != 2 or rest[1].lower() not in {"vod", "live"}:
                return "usage: /origin mode <origin_id> <vod|live>"
            mode = rest[1].lower()
            if self.store.set_control_twitch_recording_mode(rest[0], mode):
                return f"recording mode={mode}: {rest[0]}"
            return f"not found, config-managed, or not twitch/vods: {rest[0]}"
        if action in {"del", "delete", "rm", "remove"}:
            if len(rest) != 1:
                return "usage: /origin del <origin_id>"
            if self.store.delete_control_origin(rest[0]):
                return f"deleted origin: {rest[0]}"
            return f"not found or config-managed: {rest[0]}"
        return self._origin_usage()

    def _origin_add(
        self,
        args: list[str],
        message: dict[str, Any],
        *,
        recording_mode: str | None = None,
    ) -> str:
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
            external_id = resolve_channel_id(source_ref, self.config.download.yt_dlp)
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
            return "panel currently supports youtube and twitch origins"

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
        existing = next(
            (
                row
                for row in self.store.list_origin_statuses()
                if row["managed_by"] == "control"
                and row["provider"] == provider
                and row["kind"] == kind
                and str(row["external_id"]).lower() == external_id.lower()
            ),
            None,
        )
        if existing is not None:
            if recording_mode is not None and effective_recording_mode is not None:
                if not self.store.set_control_twitch_recording_mode(
                    str(existing["id"]),
                    effective_recording_mode,
                ):
                    raise ValueError("existing Twitch origin is no longer editable")
            self.store.set_control_origin_enabled(str(existing["id"]), True)
            mode_suffix = (
                f" mode={effective_recording_mode}"
                if recording_mode is not None and effective_recording_mode is not None
                else ""
            )
            return f"already exists; enabled: {existing['id']}{mode_suffix}"

        credentials_ready = self._twitch_credentials_ready()
        enabled = provider != "twitch" or credentials_ready
        options: dict[str, Any] = {"created_from": "telegram_panel"}
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
        created_by = str((message.get("from") or {}).get("id") or "")
        created = self.store.upsert_control_origin(
            origin,
            created_by=created_by,
            max_failures=self.config.app.max_attempts,
        )
        action = "added" if created else "updated"
        suffix = ""
        if provider == "twitch" and not credentials_ready:
            suffix = "; disabled until Twitch credentials are configured and it is enabled"
        mode_suffix = (
            f" mode={effective_recording_mode}"
            if effective_recording_mode is not None
            else ""
        )
        return (
            f"{action}: {origin.id} -> {provider}/{kind}:{external_id}"
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

    def _origin_list(self) -> str:
        rows = self.store.list_origin_statuses()
        lines = [f"source_filter={format_source_filter(self._source_filter_pattern())}", "origins:"]
        for row in rows:
            state = "on" if row["enabled"] else "off"
            owner = "bot" if row["managed_by"] == "control" else "config"
            error = f" error={row['last_error_code']}" if row["last_error_code"] else ""
            mode = self._effective_twitch_recording_mode(row)
            mode_text = f" mode={mode}" if mode else ""
            lines.append(
                f"- {row['id']} [{owner}:{state}] {row['provider']}/{row['kind']} "
                f"{row['name']} -> {row['external_id']}{mode_text} "
                f"items={row['item_count']}{error}"
            )
        if len(lines) == 2:
            lines.append("(none)")
        return "\n".join(lines)

    @staticmethod
    def _origin_usage() -> str:
        return "\n".join(
            [
                "origin commands:",
                "/origin add youtube <@handle|channel_id> [name]",
                "/origin add twitch [vods|highlights|uploads] <login|user_id> [name]",
                "/origin list",
                "/origin enable|disable <origin_id>",
                "/origin mode <origin_id> <vod|live>",
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
            state["flash"] = f"操作失败：{exc}"
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
        if action == "home":
            state.update({"view": "home", "awaiting": None, "twitch_mode": None})
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
        if action == "filter":
            state.update({"view": "filter", "awaiting": None})
            return
        if action == "addyt":
            state.update({"view": "input", "awaiting": "add_youtube"})
            return
        if action == "addtw":
            state.update(
                {
                    "view": "twitch_mode",
                    "awaiting": None,
                    "twitch_mode": None,
                }
            )
            return
        if action == "addtwmode":
            if len(parts) != 3 or parts[2] not in {"vod", "live"}:
                raise ValueError("invalid Twitch recording mode")
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
            self.store.set_bot_state(SOURCE_FILTER_STATE_KEY, "")
            state.update({"view": "filter", "awaiting": None, "flash": "过滤器已关闭"})
            return
        if action == "filterreset":
            self.store.set_bot_state(SOURCE_FILTER_STATE_KEY, DEFAULT_SOURCE_FILTER_PATTERN)
            state.update({"view": "filter", "awaiting": None, "flash": "过滤器已恢复默认"})
            return
        if action == "cancel":
            state.update(
                {
                    "view": "home",
                    "awaiting": None,
                    "twitch_mode": None,
                    "flash": "已取消输入",
                }
            )
            return
        if action == "twmode":
            if len(parts) != 4 or parts[3] not in {"vod", "live"}:
                raise ValueError("invalid Twitch mode action")
            row = self._resolve_control_origin(parts[2])
            mode = parts[3]
            if not self.store.set_control_twitch_recording_mode(
                str(row["id"]),
                mode,
            ):
                raise ValueError("origin is no longer editable as twitch/vods")
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
            row = self._resolve_control_origin(parts[2])
            origin_id = str(row["id"])
            if action == "toggle":
                enabled = not bool(row["enabled"])
                if enabled and row["provider"] == "twitch" and not self._twitch_credentials_ready():
                    raise ValueError("请先在服务环境中配置 Twitch 凭据")
                if not self.store.set_control_origin_enabled(origin_id, enabled):
                    raise ValueError("origin is no longer editable")
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
            if not self.store.delete_control_origin(origin_id):
                raise ValueError("origin is no longer editable")
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
            state.update(
                {
                    "view": "home",
                    "awaiting": None,
                    "twitch_mode": None,
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
                mode = str(state.get("twitch_mode") or "")
                if mode not in {"vod", "live"}:
                    raise ValueError("请先选择 Twitch 录制模式")
                reply = self._origin_add(
                    ["twitch", *args],
                    message,
                    recording_mode=mode,
                )
                state.update(
                    {
                        "view": "origins",
                        "awaiting": None,
                        "twitch_mode": None,
                        "flash": reply,
                    }
                )
            elif awaiting == "set_filter":
                reply = self._source_filter([text])
                if reply.startswith("error:"):
                    raise ValueError(reply.removeprefix("error: "))
                state.update({"view": "filter", "awaiting": None, "flash": reply})
            else:
                raise ValueError("unknown pending panel input")
        except Exception as exc:
            state["flash"] = f"输入无效：{exc}"
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
        state.update(
            {
                "view": "home",
                "awaiting": None,
                "twitch_mode": None,
                "flash": "已取消输入",
            }
        )
        self._render_panel_message(message, state)
        return True

    def _open_panel(self, message: dict[str, Any]) -> None:
        state = self._load_panel_state(message)
        state.update({"view": "home", "awaiting": None, "twitch_mode": None})
        self._render_panel_message(message, state)

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
                    "本频道模式："
                    f"{'直播中录制' if state.get('twitch_mode') == 'live' else '直播结束后下载'}。"
                ),
                "set_filter": (
                    "设置全局来源过滤器\n\n"
                    "请发送正则表达式；匹配不区分大小写。"
                ),
            }
            text = prompts.get(str(awaiting), "等待输入")
            if flash:
                text = f"⚠️ {flash}\n\n{text}"
            return text, _inline_keyboard([[ _button("取消", "p:cancel") ]])

        view = str(state.get("view") or "home")
        if view == "origins":
            text, keyboard = self._render_origins_panel(state)
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
        if flash:
            text = f"✅ {flash}\n\n{text}"
        return text, _inline_keyboard(keyboard)

    def _render_home_panel(self) -> tuple[str, list[list[dict[str, str]]]]:
        snapshot = self._panel_snapshot()
        origins = snapshot["origins"]
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
                f"失败：{summary['failed']}  阻断：{summary['blocked']}",
                f"过滤器：{format_source_filter(snapshot.get('source_filter_pattern'))}",
                f"面板：{panel_expiry}",
                f"快照：{_format_snapshot_time(str(snapshot['generated_at']))}",
            ]
        )
        return text, [
            [_button("📚 来源", "p:origins:0"), _button("📊 状态", "p:stats")],
            [_button("➕ YouTube", "p:addyt"), _button("➕ Twitch", "p:addtw")],
            [_button("🔎 过滤器", "p:filter"), _button("🔄 刷新", "p:refresh")],
        ]

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

    def _render_origins_panel(
        self,
        state: dict[str, Any],
    ) -> tuple[str, list[list[dict[str, str]]]]:
        rows = self._panel_snapshot()["origins"]
        page_count = max(1, (len(rows) + PANEL_PAGE_SIZE - 1) // PANEL_PAGE_SIZE)
        page = min(max(0, int(state.get("page") or 0)), page_count - 1)
        state["page"] = page
        selected = rows[page * PANEL_PAGE_SIZE : (page + 1) * PANEL_PAGE_SIZE]
        lines = [f"📚 来源列表  {page + 1}/{page_count}", ""]
        keyboard: list[list[dict[str, str]]] = []
        for index, row in enumerate(selected, start=page * PANEL_PAGE_SIZE + 1):
            icon = "✅" if row["enabled"] else "⏸"
            owner = "bot" if row["managed_by"] == "control" else "config"
            error = f" · ⚠️{row['last_error_code']}" if row["last_error_code"] else ""
            mode = self._effective_twitch_recording_mode(row)
            mode_text = (
                f" · {'🔴 LIVE' if mode == 'live' else '📼 VOD'}"
                if mode
                else ""
            )
            lines.append(
                f"{index}. {icon} {row['name']}\n"
                f"   {row['provider']}/{row['kind']} · {owner}{mode_text} "
                f"· items={row['item_count']}{error}"
            )
            if row["managed_by"] == "control":
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
        keyboard.extend(
            [
                [_button("➕ YouTube", "p:addyt"), _button("➕ Twitch", "p:addtw")],
                [_button("🏠 返回", "p:home"), _button("🔄 刷新", f"p:originsrefresh:{page}")],
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
        row = self._resolve_control_origin(token)
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

    def _resolve_control_origin(self, token: str) -> Any:
        matches = [
            row
            for row in self.store.list_origin_statuses()
            if row["managed_by"] == "control" and _origin_token(str(row["id"])) == token
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

    def _sub(self, args: list[str], message: dict[str, Any]) -> str:
        if not args:
            return self._help()
        action = args[0].lower()
        rest = args[1:]
        if action == "add":
            return self._sub_add_short(rest, message)
        if action in {"del", "delete", "rm", "remove"}:
            return self._sub_del(rest)
        if action in {"list", "ls"}:
            return self._sub_list()
        if action in {"filter", "source_filter"}:
            return self._source_filter(rest)
        return self._help()

    def _sub_add_short(self, args: list[str], message: dict[str, Any]) -> str:
        if not args:
            return "usage: /sub add [live|channel] <@handle|channel_id> [name]"
        route = "live"
        if args[0] in {"live", "channel"}:
            route = args[0]
            args = args[1:]
        if not args:
            return "usage: /sub add [live|channel] <@handle|channel_id> [name]"

        channel_ref = args[0]
        channel_id = resolve_channel_id(channel_ref, self.config.download.yt_dlp)
        sub_id = _subscription_id(route, channel_ref)
        name = " ".join(args[1:]) if len(args) > 1 else _default_name(channel_ref)
        return self._save_subscription(
            sub_id=sub_id,
            name=name,
            channel_id=channel_id,
            routes=[route],
            message=message,
        )

    def _sub_add(self, args: list[str], message: dict[str, Any]) -> str:
        if len(args) < 2:
            return "usage: /sub_add <id> <channel_id|@handle> [routes=live,channel] [name]"
        sub_id = _validate_id(args[0])
        channel_id = resolve_channel_id(args[1], self.config.download.yt_dlp)
        routes = list(self.config.control.default_routes)
        name_parts: list[str] = []
        for arg in args[2:]:
            if arg.startswith("routes="):
                routes = [item.strip() for item in arg.split("=", 1)[1].split(",") if item.strip()]
            else:
                name_parts.append(arg)
        name = " ".join(name_parts) if name_parts else sub_id
        return self._save_subscription(
            sub_id=sub_id,
            name=name,
            channel_id=channel_id,
            routes=routes,
            message=message,
        )

    def _save_subscription(
        self,
        *,
        sub_id: str,
        name: str,
        channel_id: str,
        routes: list[str],
        message: dict[str, Any],
    ) -> str:
        created_by = str((message.get("from") or {}).get("id") or "")
        created = self.store.upsert_subscription(
            sub_id=sub_id,
            name=name,
            channel_id=channel_id,
            routes=routes,
            created_by=created_by,
        )
        action = "added" if created else "updated"
        return f"{action}: {sub_id} -> {channel_id} routes={','.join(routes)}"

    def _sub_del(self, args: list[str]) -> str:
        if len(args) != 1:
            return "usage: /sub_del <id>"
        sub_id = args[0]
        if self.store.delete_subscription(sub_id):
            return f"deleted: {sub_id}"
        return f"not found: {sub_id}"

    def _sub_list(self) -> str:
        lines = [f"source_filter={format_source_filter(self._source_filter_pattern())}", "subscriptions:"]
        static_channels = [channel for channel in self.config.channels if channel.enabled]
        for channel in static_channels:
            lines.append(
                f"- {channel.id} [config] {channel.name} {channel.channel_id} routes={','.join(channel.routes)}"
            )
        for sub in self.store.list_subscriptions():
            state = "on" if sub.enabled else "off"
            lines.append(f"- {sub.id} [db:{state}] {sub.name} {sub.channel_id} routes={','.join(sub.routes)}")
        if len(lines) == 2:
            lines.append("(none)")
        return "\n".join(lines)

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
            self.store.set_bot_state(SOURCE_FILTER_STATE_KEY, "")
            return "source_filter=off; all sources enabled"
        if action in {"reset", "default"}:
            self.store.set_bot_state(SOURCE_FILTER_STATE_KEY, DEFAULT_SOURCE_FILTER_PATTERN)
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
        self.store.set_bot_state(SOURCE_FILTER_STATE_KEY, pattern)
        return f"source_filter={format_source_filter(pattern)}"

    def _source_filter_pattern(self) -> str | None:
        pattern = self.store.get_bot_state(SOURCE_FILTER_STATE_KEY)
        if pattern is None:
            return DEFAULT_SOURCE_FILTER_PATTERN
        return pattern or None

    def _panel_snapshot(self, *, force: bool = False) -> dict[str, Any]:
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
        return "\n".join(
            [
                "Media audio backup bot",
                "",
                "Recommended:",
                "/panel - open the single-message control panel",
                "",
                "Provider-neutral origins:",
                "/origin add youtube @handle [name]",
                "/origin add twitch [vods|highlights|uploads] login [name]",
                "/origin list",
                "/origin enable|disable <origin_id>",
                "/origin mode <origin_id> <vod|live>",
                "/origin del <origin_id>",
                "",
                "YouTube compatibility:",
                "/sub add @handle",
                "/sub add live @handle",
                "/sub add channel @handle",
                "/sub del <id>",
                "/sub list",
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
        endpoint = f"{self.config.telegram.api_base.rstrip('/')}/bot{self.config.telegram.bot_token}/{method}"
        request = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=request_timeout_seconds) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram API HTTP {exc.code}: {body[:500]}") from exc
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


def _subscription_id(route: str, channel_id: str) -> str:
    normalized = _default_name(channel_id).strip()
    safe = "".join(char if char.isalnum() or char in {"_", "-", "@"} else "_" for char in normalized)
    return _validate_id(f"{route}@{safe}")


def _normalize_twitch_source(value: str) -> str:
    candidate = value.strip().removeprefix("@").lower()
    if candidate.isdigit():
        return candidate
    if not TWITCH_LOGIN_PATTERN.fullmatch(candidate):
        raise ValueError("Twitch source must be a numeric user ID or a 1-25 character login")
    return candidate


def _dynamic_origin_id(provider: str, kind: str, external_id: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", external_id).strip("-").lower()[:32] or "origin"
    digest = hashlib.sha256(f"{provider}\0{kind}\0{external_id}".encode("utf-8")).hexdigest()[:10]
    return _validate_id(f"db:{provider}:{kind}:{slug}-{digest}")


def _origin_token(origin_id: str) -> str:
    return hashlib.sha256(origin_id.encode("utf-8")).hexdigest()[:16]


def _compact_button_label(value: str) -> str:
    return " ".join(value.replace("\n", " ").split())[:24] or "origin"


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
