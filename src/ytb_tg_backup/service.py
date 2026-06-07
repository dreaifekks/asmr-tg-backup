from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import sqlite3
import subprocess
import time
from urllib.error import HTTPError

from .config import ChannelConfig, Config, expand_channel_feeds
from .control import ControlBot
from .downloader import Downloader, WAIT_LIVE_STATUSES
from .feed import fetch_feed, parse_feed
from .store import Store
from .telegram import TelegramUploader, TelegramUploadError


class BackupService:
    def __init__(self, config: Config):
        self.config = config
        self.logger = logging.getLogger("ytb_tg_backup")
        self.store = Store(config.db_path)
        self.downloader = Downloader(config, self.logger)
        self.telegram = TelegramUploader(config.telegram)
        self.control_bot = ControlBot(config, self.store, self.logger)

    def initialize(self) -> None:
        self.config.app.data_dir.mkdir(parents=True, exist_ok=True)
        self.config.download_dir.mkdir(parents=True, exist_ok=True)
        self.store.initialize()

    def run_forever(self) -> None:
        self.initialize()
        self._log_startup_warnings()
        if self.config.control.enabled:
            try:
                self.control_bot.register_commands()
            except Exception:
                self.logger.warning("failed to register Telegram bot commands", exc_info=True)
        next_feed_poll = 0.0
        next_control_poll = 0.0
        while True:
            now = time.monotonic()
            if self.config.control.enabled and now >= next_control_poll:
                try:
                    self.control_bot.process_once()
                except Exception as exc:
                    self.logger.warning("control bot cycle failed: %s", exc)
                finally:
                    next_control_poll = now + self.config.control.poll_interval_seconds
            if now >= next_feed_poll:
                try:
                    self.poll_once(process=True)
                except Exception:
                    self.logger.exception("feed poll cycle failed")
                finally:
                    next_feed_poll = now + self.config.app.poll_interval_seconds
            time.sleep(1)

    def poll_once(self, *, process: bool) -> None:
        self.initialize()
        for feed in self._all_feeds():
            if not feed.enabled:
                self.logger.debug("feed disabled id=%s", feed.id)
                continue
            try:
                xml_bytes = fetch_feed(feed.url)
                entries = parse_feed(xml_bytes, feed.id, feed.name)
            except HTTPError as exc:
                if "/youtube/live/" in feed.url and exc.code in {404, 503}:
                    self.logger.info("live feed unavailable id=%s status=%s; skipping", feed.id, exc.code)
                    continue
                self.logger.exception("failed to fetch feed id=%s url=%s", feed.id, feed.url)
                continue
            except Exception:
                self.logger.exception("failed to fetch feed id=%s url=%s", feed.id, feed.url)
                continue
            feed_seeded = self.store.has_entries_for_feed(feed.id)
            new_count = 0
            ignored_seed_count = 0
            for index, entry in enumerate(entries):
                status = "seen"
                last_error = None
                if not feed_seeded and index > 0:
                    status = "ignored"
                    last_error = "initial feed seed ignored; kept latest entry only"
                if self.store.upsert_entry(entry, status=status, last_error=last_error):
                    if status == "ignored":
                        ignored_seed_count += 1
                    else:
                        new_count += 1
            self.logger.info(
                "feed id=%s entries=%d new=%d ignored_seed=%d",
                feed.id,
                len(entries),
                new_count,
                ignored_seed_count,
            )

        if process:
            self.process_pending()

    def process_pending(self) -> None:
        self._log_startup_warnings()
        rows = self.store.list_pending(
            limit=self.config.app.max_items_per_poll,
            max_attempts=self.config.app.max_attempts,
            include_downloaded=self.config.telegram.enabled,
        )
        for row in rows:
            self._process_row(row)

    def _process_row(self, row: sqlite3.Row) -> None:
        video_id = str(row["video_id"])
        file_path = Path(row["file_path"]) if row["file_path"] else None
        title = str(row["title"])
        if row["status"] != "downloaded":
            if not self._download_delay_elapsed(row):
                self.store.mark_waiting(video_id, "waiting for download delay", 60)
                return
            try:
                probe = self.downloader.probe(str(row["url"]))
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
                self.store.mark_failed(video_id, f"yt-dlp probe failed: {exc}", self.config.app.retry_seconds)
                return
            if probe.title:
                title = probe.title
                self.store.update_title(video_id, probe.title)
            wait_live_statuses = WAIT_LIVE_STATUSES
            if str(row["feed_id"]) == "manual":
                wait_live_statuses = WAIT_LIVE_STATUSES - {"post_live"}
            if probe.live_status in wait_live_statuses:
                self.store.mark_waiting(
                    video_id,
                    f"live_status={probe.live_status}; waiting for VOD readiness",
                    self.config.app.live_retry_seconds,
                )
                return
            try:
                self.store.begin_download(video_id)
                result = self.downloader.download(video_id, str(row["url"]))
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, RuntimeError) as exc:
                self.store.mark_failed(video_id, f"download failed: {exc}", self.config.app.retry_seconds)
                return
            self.store.mark_downloaded(video_id, result.file_path, result.file_size)
            file_path = result.file_path

        if not self.config.telegram.enabled:
            self.logger.info("telegram disabled; leaving video_id=%s as downloaded", video_id)
            return
        if file_path is None or not file_path.exists():
            self.store.mark_failed(video_id, "downloaded file path is missing", self.config.app.retry_seconds)
            return
        try:
            if file_path.stat().st_size > self.config.telegram.max_upload_bytes:
                shrunk = self.downloader.shrink_audio_for_upload(file_path, self.config.telegram.max_upload_bytes)
                if shrunk.file_path != file_path:
                    self.store.mark_downloaded(video_id, shrunk.file_path, shrunk.file_size)
                    file_path = shrunk.file_path
            thumbnail_path = self.downloader.prepare_thumbnail_for_upload(video_id)
            message_id = self.telegram.upload(
                file_path,
                title=title,
                url=str(row["url"]),
                feed_name=str(row["feed_name"]),
                video_id=video_id,
                thumbnail_path=thumbnail_path,
            )
        except TelegramUploadError as exc:
            if "exceeds telegram.max_upload_bytes" in str(exc):
                self.store.mark_blocked(video_id, str(exc))
            else:
                self.store.mark_failed(video_id, f"telegram upload failed: {exc}", self.config.app.retry_seconds)
            return
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            self.store.mark_failed(
                video_id,
                f"telegram upload command failed: {stderr or stdout or exc}",
                self.config.app.retry_seconds,
            )
            return
        self.store.mark_uploaded(video_id, message_id)

    def _download_delay_elapsed(self, row: sqlite3.Row) -> bool:
        first_seen = datetime.fromisoformat(str(row["first_seen_at"]))
        if first_seen.tzinfo is None:
            first_seen = first_seen.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - first_seen.astimezone(timezone.utc)).total_seconds()
        return age >= self.config.app.download_delay_seconds

    def _log_startup_warnings(self) -> None:
        missing = self.downloader.check_tools()
        if missing:
            self.logger.warning("missing host tools: %s", ", ".join(missing))
        try:
            self.telegram.validate()
        except TelegramUploadError as exc:
            self.logger.warning("%s", exc)

    def _all_feeds(self):
        feeds = list(self.config.feeds)
        subscriptions = self.store.list_subscriptions()
        dynamic_channels = [
            ChannelConfig(
                id=sub.id,
                name=sub.name,
                channel_id=sub.channel_id,
                routes=sub.routes,
                enabled=sub.enabled,
            )
            for sub in subscriptions
        ]
        feeds.extend(expand_channel_feeds(self.config.rsshub, dynamic_channels, prefix="db:"))
        return feeds
