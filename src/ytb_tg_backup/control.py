from __future__ import annotations

import json
import logging
import shlex
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .config import Config
from .source_filter import (
    DEFAULT_SOURCE_FILTER_PATTERN,
    SOURCE_FILTER_STATE_KEY,
    compile_source_filter,
    format_source_filter,
)
from .store import Store
from .youtube import resolve_channel_id


class ControlBot:
    def __init__(self, config: Config, store: Store, logger: logging.Logger):
        self.config = config
        self.store = store
        self.logger = logger

    def process_once(self) -> None:
        if not self.config.control.enabled:
            return
        if not self.config.telegram.bot_token:
            self.logger.warning("control enabled but telegram.bot_token is empty")
            return

        offset = self.store.get_bot_offset()
        payload: dict[str, Any] = {"timeout": 0, "limit": 20, "allowed_updates": ["message"]}
        if offset:
            payload["offset"] = offset + 1
        response = self._api("getUpdates", payload)
        for update in response.get("result", []):
            update_id = int(update["update_id"])
            try:
                self._handle_update(update)
            finally:
                self.store.set_bot_offset(update_id)

    def register_commands(self) -> None:
        if not self.config.control.enabled or not self.config.telegram.bot_token:
            return
        if self.config.control.delete_webhook_on_startup:
            self._api("deleteWebhook", {"drop_pending_updates": False})
        self._api(
            "setMyCommands",
            {
                "commands": [
                    {"command": "start", "description": "Show help"},
                    {"command": "help", "description": "Show help"},
                    {"command": "sub", "description": "Manage subscriptions"},
                    {"command": "source_filter", "description": "Filter sources by regex"},
                    {"command": "stats", "description": "Show backup counts"},
                ]
            },
        )

    def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not message:
            return
        text = str(message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        if not self._authorized(message):
            self.logger.warning("unauthorized control command from %s", self._principal(message))
            self._reply(message, "unauthorized")
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
        if command in {"/help", "/start"}:
            return self._help()
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

    def _stats(self) -> str:
        summary = self.store.backup_summary()
        return "\n".join(
            [
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
            ]
        )

    def _help(self) -> str:
        return "\n".join(
            [
                "YouTube ASMR backup bot",
                "",
                "Add subscriptions:",
                "/sub add @handle",
                "/sub add live @handle",
                "/sub add channel @handle",
                "/sub add channel @handle Display Name",
                "",
                "Manage:",
                "/sub del <id>",
                "/sub list",
                "/source_filter <regex|off|reset>",
                "/stats",
                "",
                "Default route is live. IDs are generated like live@handle or channel@handle.",
                "Default source filter is /ASMR/i. Matching is regex-based and case-insensitive.",
                "Push caption: title, YouTube URL, then #tag.",
            ]
        )

    def _authorized(self, message: dict[str, Any]) -> bool:
        control = self.config.control
        if not (control.allowed_user_ids or control.allowed_chat_ids or control.allowed_message_thread_ids):
            return False

        from_id = str((message.get("from") or {}).get("id") or "")
        chat_id = str((message.get("chat") or {}).get("id") or "")
        thread_id = str(message.get("message_thread_id") or "")

        return (
            (bool(control.allowed_user_ids) and from_id in control.allowed_user_ids)
            or (bool(control.allowed_chat_ids) and chat_id in control.allowed_chat_ids)
            or (bool(control.allowed_message_thread_ids) and thread_id in control.allowed_message_thread_ids)
        )

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

    def _api(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        endpoint = f"{self.config.telegram.api_base.rstrip('/')}/bot{self.config.telegram.bot_token}/{method}"
        request = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram API HTTP {exc.code}: {body[:500]}") from exc
        if not parsed.get("ok"):
            raise RuntimeError(f"Telegram API error: {parsed}")
        return parsed


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
