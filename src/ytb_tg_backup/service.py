from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import threading
import time
from urllib.error import HTTPError
import uuid

from .config import Config
from .control import ControlBot
from .downloader import (
    DownloadCancelled,
    DownloadResult,
    Downloader,
    LiveDownloadError,
    ProbeResult,
    WAIT_LIVE_STATUSES,
)
from .feed import fetch_feed
from .models import ClaimedJob, MediaCandidate, Origin
from .source_filter import (
    DEFAULT_SOURCE_FILTER_PATTERN,
    SOURCE_FILTER_STATE_KEY,
    compile_source_filter,
    format_source_filter,
    text_matches_source_filter,
)
from .sources import (
    SourceError,
    SourceRegistry,
    twitch_recording_mode,
    validate_public_media_url,
)
from .store import Store, future_iso, now_iso
from .telegram import TelegramUploader, TelegramUploadError


class BackupService:
    def __init__(self, config: Config):
        os.umask(0o077)
        self.config = config
        self.logger = logging.getLogger("ytb_tg_backup")
        self.store = Store(config.db_path)
        self.downloader = Downloader(config, self.logger)
        self.telegram = TelegramUploader(config.telegram)
        self.control_bot = ControlBot(config, self.store, self.logger)
        self.sources = self._new_source_registry()
        self._stop_event = threading.Event()

    @property
    def telegram_destination_key(self) -> str:
        return f"telegram:{self.config.telegram.chat_id}"

    def initialize(self) -> None:
        self.config.app.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config.download_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in (self.config.app.data_dir, self.config.download_dir):
            try:
                path.chmod(0o700)
            except OSError:
                pass
        providers = {
            "youtube",
            *self.config.download.provider_profiles,
            *(origin.provider for origin in self.config.origins),
        }
        for archive_file in {
            self.downloader.archive_file_for_provider(provider)
            for provider in providers
        }:
            if not archive_file.exists():
                continue
            try:
                archive_file.chmod(0o600)
            except OSError:
                pass
        self.store.initialize()
        for origin in self.config.origins:
            self.store.upsert_origin(
                origin,
                managed_by="config",
                max_failures=self.config.app.max_attempts,
            )
        self.store.disable_missing_config_origins({origin.id for origin in self.config.origins})
        self.store.recover_stale_jobs()
        if self.config.telegram.enabled:
            self.store.adopt_legacy_delivery_destination(self.telegram_destination_key)
            self.store.reconcile_delivery_destination(self.telegram_destination_key)
            self.store.ensure_delivery_jobs_for_ready_artifacts(
                self.telegram_destination_key,
                max_failures=self.config.app.max_attempts,
            )

    def run_forever(self) -> None:
        self.initialize()
        self._log_startup_warnings()
        if self.config.control.enabled:
            try:
                self.control_bot.register_commands()
            except Exception:
                self.logger.warning("failed to register Telegram bot commands", exc_info=True)

        workers = [
            threading.Thread(
                target=self._worker_loop,
                args=(index, "standard"),
                name=f"backup-worker-{index}",
                daemon=True,
            )
            for index in range(self.config.app.worker_count)
        ]
        workers.extend(
            threading.Thread(
                target=self._worker_loop,
                args=(index, "live"),
                name=f"twitch-live-worker-{index}",
                daemon=True,
            )
            for index in range(self.config.twitch.live_worker_count)
        )
        workers.extend(
            (
                threading.Thread(
                    target=self._source_poll_loop,
                    args=(False, self.config.app.poll_interval_seconds),
                    name="source-poll-worker",
                    daemon=True,
                ),
                threading.Thread(
                    target=self._source_poll_loop,
                    args=(True, self.config.twitch.live_poll_interval_seconds),
                    name="twitch-live-poll-worker",
                    daemon=True,
                ),
            )
        )
        if self.config.control.enabled:
            workers.append(
                threading.Thread(
                    target=self._control_loop,
                    name="control-worker",
                    daemon=True,
                )
            )
        for worker in workers:
            worker.start()

        try:
            self._stop_event.wait()
        finally:
            self._stop_event.set()
            drain_deadline = time.monotonic() + 25
            for worker in workers:
                worker.join(timeout=max(0.0, drain_deadline - time.monotonic()))
                if worker.is_alive():
                    self.logger.warning("worker did not drain before shutdown name=%s", worker.name)

    def stop(self) -> None:
        self._stop_event.set()

    def poll_once(self, *, process: bool) -> None:
        self.initialize()
        self._poll_origins(live_recording=None)

        if process:
            self.process_pending()

    def _source_poll_loop(self, live_recording: bool, interval_seconds: int) -> None:
        label = "Twitch live" if live_recording else "source"
        while not self._stop_event.is_set():
            store: Store | None = None
            try:
                store = Store(self.config.db_path)
                store.initialize()
                sources = self._new_source_registry()
                while not self._stop_event.is_set():
                    started_at = time.monotonic()
                    try:
                        self._poll_origins(
                            live_recording=live_recording,
                            store=store,
                            sources=sources,
                        )
                    except Exception:
                        self.logger.exception("%s poll cycle failed", label)
                    elapsed = time.monotonic() - started_at
                    self._stop_event.wait(max(0.0, interval_seconds - elapsed))
            except Exception:
                self.logger.exception("%s poll worker crashed; reconnecting", label)
                self._stop_event.wait(min(10, interval_seconds))
            finally:
                if store is not None:
                    store.close()

    def _poll_origins(
        self,
        *,
        live_recording: bool | None,
        store: Store | None = None,
        sources: SourceRegistry | None = None,
    ) -> None:
        active_store = store or self.store
        active_sources = sources or self.sources
        source_filter_pattern, source_filter = self._compiled_source_filter(active_store)
        for origin in self._all_origins(active_store):
            if not origin.enabled:
                self.logger.debug("origin disabled id=%s", origin.id)
                continue
            is_live_origin = self._is_live_recording_origin(origin)
            if live_recording is not None and is_live_origin != live_recording:
                continue
            if origin.provider == "twitch" and origin.kind == "vods":
                mode = "live" if is_live_origin else "vod"
                if active_store.reconcile_origin_poll_mode(origin.id, mode):
                    self.logger.info(
                        "reset Twitch poll state after recording mode change "
                        "origin=%s mode=%s",
                        origin.id,
                        mode,
                    )
            if not active_store.origin_poll_due(origin.id):
                continue
            self._poll_origin(
                origin,
                source_filter_pattern,
                source_filter,
                store=active_store,
                sources=active_sources,
            )

    def _poll_origin(
        self,
        origin: Origin,
        source_filter_pattern: str | None,
        source_filter,
        *,
        store: Store | None = None,
        sources: SourceRegistry | None = None,
    ) -> None:
        active_store = store or self.store
        active_sources = sources or self.sources
        try:
            result = active_sources.get(origin.provider, origin.kind).discover(
                origin,
                active_store.get_origin_checkpoint(origin.id),
            )
        except SourceError as exc:
            if not self._origin_poll_configuration_is_current(
                active_store,
                origin,
            ):
                self.logger.info(
                    "discarded stale origin poll failure after configuration change "
                    "origin=%s provider=%s",
                    origin.id,
                    origin.provider,
                )
                return
            retry_seconds = exc.retry_after or (
                self.config.twitch.live_retry_seconds
                if self._is_live_recording_origin(origin)
                else self.config.app.retry_seconds
            )
            active_store.record_origin_poll_failure(
                origin.id,
                error_code=exc.code,
                error=self._safe_error(exc),
                retry_seconds=retry_seconds,
            )
            self.logger.warning("origin poll failed id=%s provider=%s code=%s: %s", origin.id, origin.provider, exc.code, exc)
            return
        except HTTPError as exc:
            if not self._origin_poll_configuration_is_current(
                active_store,
                origin,
            ):
                self.logger.info(
                    "discarded stale origin poll failure after configuration change "
                    "origin=%s provider=%s",
                    origin.id,
                    origin.provider,
                )
                return
            active_store.record_origin_poll_failure(
                origin.id,
                error_code=f"http_{exc.code}",
                error=f"HTTP {exc.code}",
                retry_seconds=self.config.app.retry_seconds,
            )
            self.logger.warning("origin HTTP failure id=%s status=%s", origin.id, exc.code)
            return
        except Exception as exc:
            if not self._origin_poll_configuration_is_current(
                active_store,
                origin,
            ):
                self.logger.info(
                    "discarded stale origin poll failure after configuration change "
                    "origin=%s provider=%s",
                    origin.id,
                    origin.provider,
                )
                return
            active_store.record_origin_poll_failure(
                origin.id,
                error_code="unexpected_error",
                error=self._safe_error(exc),
                retry_seconds=self.config.app.retry_seconds,
            )
            self.logger.exception("origin poll failed id=%s provider=%s", origin.id, origin.provider)
            return

        if not self._origin_poll_configuration_is_current(active_store, origin):
            self.logger.info(
                "discarded stale origin poll result after configuration change "
                "origin=%s provider=%s",
                origin.id,
                origin.provider,
            )
            return

        origin_seeded = active_store.origin_has_items(
            origin.id,
            content_kind=self._origin_seed_content_kind(origin),
        )
        new_count = 0
        ignored_seed_count = 0
        filtered_count = 0
        matching_index = 0
        for candidate in result.items:
            if not self._candidate_matches_filter(origin, candidate, source_filter):
                filtered_count += 1
                active_store.upsert_discovered(
                    origin.id,
                    candidate,
                    disposition="ignored",
                    decision_code="source_filter",
                    decision_reason=f"source filter ignored: {format_source_filter(source_filter_pattern)}",
                    max_failures=self.config.app.max_attempts,
                )
                continue

            disposition = "eligible"
            decision_code = None
            decision_reason = None
            stream_id = str(candidate.metadata.get("stream_id") or "")
            if (
                candidate.provider == "twitch"
                and candidate.content_kind == "vod"
                and stream_id
                and active_store.has_ready_twitch_live_recording(stream_id)
            ):
                disposition = "ignored"
                decision_code = "live_recording_exists"
                decision_reason = "matching Twitch live stream was already archived"
            elif not origin_seeded and origin.bootstrap == "latest" and matching_index > 0:
                disposition = "ignored"
                decision_code = "initial_seed"
                decision_reason = "initial feed seed ignored; kept latest entry only"
            elif origin.bootstrap == "all":
                decision_code = "bootstrap_all"
                decision_reason = "origin bootstrap explicitly permits backfill"
            job_payload = None
            if candidate.metadata.get("recording_mode") == "live":
                job_payload = {
                    "download_lane": "live",
                    "recording_mode": "live",
                }
            _, created = active_store.upsert_discovered(
                origin.id,
                candidate,
                disposition=disposition,
                decision_code=decision_code,
                decision_reason=decision_reason,
                max_failures=self.config.app.max_attempts,
                job_payload=job_payload,
            )
            if created:
                if disposition == "eligible":
                    new_count += 1
                else:
                    ignored_seed_count += 1
            matching_index += 1

        active_store.record_origin_poll_success(
            origin.id,
            cursor=result.cursor,
            etag=result.etag,
            last_modified=result.last_modified,
        )
        self.logger.info(
            "origin id=%s provider=%s items=%d new=%d ignored_seed=%d filtered=%d",
            origin.id,
            origin.provider,
            len(result.items),
            new_count,
            ignored_seed_count,
            filtered_count,
        )

    def process_pending(self) -> None:
        self.initialize()
        owner = f"cli:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._process_available(
            self.store,
            self.downloader,
            self.telegram,
            owner=owner,
            limit=self.config.app.max_items_per_poll,
            job_types=("download", "telegram_delivery"),
            download_lane="standard",
        )

    def _worker_loop(self, index: int, download_lane: str = "standard") -> None:
        owner = (
            f"worker:{download_lane}:{socket.gethostname()}:{os.getpid()}:"
            f"{index}:{uuid.uuid4().hex[:8]}"
        )
        while not self._stop_event.is_set():
            store: Store | None = None
            try:
                store = Store(self.config.db_path)
                store.initialize()
                downloader = Downloader(self.config, self.logger)
                telegram = TelegramUploader(self.config.telegram)
                while not self._stop_event.is_set():
                    job_types = (
                        ("download", "telegram_delivery")
                        if download_lane == "standard"
                        else ("download",)
                    )
                    processed = self._process_available(
                        store,
                        downloader,
                        telegram,
                        owner=owner,
                        limit=1,
                        job_types=job_types,
                        download_lane=download_lane,
                    )
                    if processed == 0:
                        self._stop_event.wait(self.config.app.worker_poll_interval_seconds)
            except Exception:
                self.logger.exception(
                    "worker loop crashed lane=%s index=%s; reconnecting",
                    download_lane,
                    index,
                )
                self._stop_event.wait(min(10, self.config.app.worker_poll_interval_seconds))
            finally:
                if store is not None:
                    store.close()

    def _control_loop(self) -> None:
        if not self.config.telegram.bot_token:
            self.logger.warning("control enabled but telegram.bot_token is empty")
            return
        while not self._stop_event.is_set():
            store: Store | None = None
            try:
                store = Store(self.config.db_path)
                store.initialize()
                bot = ControlBot(self.config, store, self.logger)
                while not self._stop_event.is_set():
                    bot.process_once()
                    store.get_panel_snapshot(
                        self._source_filter_pattern(store),
                        max_age_seconds=30,
                    )
            except Exception as exc:
                self.logger.warning(
                    "control bot long-poll failed; reconnecting: %s",
                    self._safe_error(exc),
                )
                self._stop_event.wait(1)
            finally:
                if store is not None:
                    store.close()

    def _process_available(
        self,
        store: Store,
        downloader: Downloader,
        telegram: TelegramUploader,
        *,
        owner: str,
        limit: int,
        job_types: tuple[str, ...] = ("download", "telegram_delivery"),
        download_lane: str | None = None,
    ) -> int:
        processed = 0
        for _ in range(limit):
            job = store.claim_next_job(
                job_types,
                owner=owner,
                lease_seconds=self.config.app.job_lease_seconds,
                download_lane=download_lane,
            )
            if job is None:
                break
            processed += 1
            heartbeat = _LeaseHeartbeat(
                self.config.db_path,
                job,
                self.config.app.job_lease_seconds,
                self.logger,
            )
            heartbeat_ready = heartbeat.start()
            try:
                if not heartbeat_ready:
                    store.defer_job(
                        job,
                        reason_code="lease_heartbeat_unavailable",
                        error="job deferred because its lease heartbeat could not start",
                        retry_seconds=5,
                    )
                    continue
                if job.job_type == "download":
                    self._process_download_job(
                        store,
                        downloader,
                        job,
                        lease_lost_event=heartbeat.lost_event,
                    )
                elif job.job_type == "telegram_delivery":
                    self._process_delivery_job(store, downloader, telegram, job)
                else:
                    store.block_job(job, reason_code="unknown_job_type", error=f"unsupported job type: {job.job_type}")
            except RuntimeError as exc:
                # A lost lease must not be allowed to overwrite a newer worker.
                if "lease" in str(exc).lower():
                    self.logger.warning("job lease lost id=%s: %s", job.id, exc)
                else:
                    self._fail_unexpected(store, job, exc)
            except Exception as exc:
                self._fail_unexpected(store, job, exc)
            finally:
                heartbeat.stop()
        return processed

    def _process_download_job(
        self,
        store: Store,
        downloader: Downloader,
        job: ClaimedJob,
        *,
        lease_lost_event: threading.Event,
    ) -> None:
        media = store.get_media(job.media_id)
        if media is None:
            store.block_job(job, reason_code="media_missing", error="media item no longer exists")
            return
        try:
            metadata = json.loads(str(media["metadata_json"] or "{}"))
            if not isinstance(metadata, dict):
                raise ValueError("media metadata must be an object")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            store.block_job(
                job,
                reason_code="invalid_media_metadata",
                error=self._safe_error(exc),
            )
            return
        live_recording = (
            str(media["provider"]) == "twitch"
            and metadata.get("recording_mode") == "live"
        )
        stream_id = str(metadata.get("stream_id") or "")
        twitch_vod = (
            str(media["provider"]) == "twitch"
            and str(media["content_kind"]) == "vod"
            and bool(stream_id)
        )
        if live_recording and not self._has_enabled_live_origin(store, job.media_id):
            if self._finalize_live_segments(store, downloader, job, media):
                return
            store.cancel_job(
                job,
                reason_code="live_origin_disabled",
                error="live recording origin is disabled, deleted, or switched to VOD mode",
            )
            return
        linked_live_state = None
        if twitch_vod:
            linked_live_state = store.twitch_live_recording_state(stream_id)
            if linked_live_state == "ready":
                store.cancel_job(
                    job,
                    reason_code="live_recording_exists",
                    error="matching Twitch live stream was already archived",
                )
                return
            if linked_live_state == "pending":
                store.defer_job(
                    job,
                    reason_code="live_recording_pending",
                    error="matching Twitch live recording is still in progress",
                    retry_seconds=self.config.twitch.live_retry_seconds,
                )
                return

        source_filter_pattern, source_filter = self._compiled_source_filter(store)
        origin_rows = store.media_origins(job.media_id)
        if source_filter is not None and not any(
            text_matches_source_filter(source_filter, row["id"], row["name"], media["title"])
            for row in origin_rows
        ):
            store.cancel_job(
                job,
                reason_code="source_filter",
                error=f"source filter ignored: {format_source_filter(source_filter_pattern)}",
            )
            return

        artifact = store.get_artifact(job.media_id, "master")
        force_redownload = False
        if artifact is not None:
            artifact_path = Path(str(artifact["path"]))
            reusable_artifact = artifact["state"] in {"ready", "staged"} or (
                twitch_vod
                and linked_live_state is None
                and artifact["state"] == "suppressed"
            )
            if reusable_artifact and artifact_path.exists():
                store.complete_download(
                    job,
                    path=artifact_path,
                    size_bytes=artifact_path.stat().st_size,
                    delivery_targets=self._delivery_targets(),
                    delivery_max_failures=self.config.app.max_attempts,
                    live_retry_seconds=self.config.twitch.live_retry_seconds,
                )
                return
            force_redownload = True

        if not live_recording and not self._download_delay_elapsed(str(media["first_seen_at"])):
            store.defer_job(job, reason_code="download_delay", error="waiting for download delay", retry_seconds=60)
            return

        url = str(media["canonical_url"])
        if str(media["provider"]) == "rss":
            try:
                validate_public_media_url(
                    url,
                    allowed_hosts=tuple(str(item) for item in metadata.get("allowed_media_hosts", [])),
                    allow_private=bool(metadata.get("allow_private_media", False)),
                )
            except SourceError as exc:
                if exc.code == "unsafe_media_url":
                    store.block_job(job, reason_code=exc.code, error=self._safe_error(exc))
                else:
                    store.fail_job(
                        job,
                        reason_code=exc.code,
                        error=self._safe_error(exc),
                        retry_seconds=self.config.app.retry_seconds,
                    )
                return
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                store.block_job(job, reason_code="invalid_media_metadata", error=self._safe_error(exc))
                return
        try:
            probe = downloader.probe(
                url,
                provider=str(media["provider"]),
                live=live_recording,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as exc:
            store.fail_job(
                job,
                reason_code="probe_failed",
                error=self._safe_error(exc),
                retry_seconds=(
                    self.config.twitch.live_retry_seconds
                    if live_recording
                    else self.config.app.retry_seconds
                ),
            )
            return
        if probe.title:
            store.update_media_title(job.media_id, probe.title)
        expected_stream_id = stream_id
        if (
            live_recording
            and probe.external_id is not None
            and probe.external_id != expected_stream_id
        ):
            if self._finalize_live_segments(store, downloader, job, media):
                return
            store.cancel_job(
                job,
                reason_code="stream_replaced",
                error=(
                    f"expected Twitch stream {expected_stream_id}, "
                    f"but channel is now streaming {probe.external_id}"
                ),
            )
            return
        if live_recording and probe.live_status != "is_live":
            if self._finalize_live_segments(store, downloader, job, media):
                return
            store.fail_job(
                job,
                reason_code="live_window_missed",
                error=f"live_status={probe.live_status or 'unknown'}; stream is no longer recordable",
                retry_seconds=self.config.twitch.live_retry_seconds,
            )
            return
        if not live_recording and probe.live_status in WAIT_LIVE_STATUSES:
            store.defer_job(
                job,
                reason_code="not_ready",
                error=f"live_status={probe.live_status}; waiting for VOD readiness",
                retry_seconds=self.config.app.live_retry_seconds,
            )
            return

        try:
            result = downloader.download(
                str(media["external_id"]),
                url,
                provider=str(media["provider"]),
                ignore_archive=force_redownload
                or job.reason_code in {
                    "artifact_missing",
                    "live_interrupted",
                    "service_stopping",
                },
                live=live_recording,
                cancel_events=(self._stop_event, lease_lost_event),
            )
        except DownloadCancelled as exc:
            self._record_live_segment(
                store,
                job.media_id,
                exc.partial_result,
                reason="service_stopping",
            )
            if lease_lost_event.is_set():
                raise RuntimeError("job lease lost during live recording") from None
            store.defer_job(
                job,
                reason_code="service_stopping",
                error=(
                    "live recording stopped with the service; retry starts from "
                    "the channel's current live position"
                ),
                retry_seconds=0,
            )
            return
        except LiveDownloadError as exc:
            self._record_live_segment(
                store,
                job.media_id,
                exc.partial_result,
                reason="download_interrupted",
            )
            recovery_probe = self._probe_twitch_live(downloader, url)
            if (
                recovery_probe is not None
                and recovery_probe.live_status == "is_live"
                and recovery_probe.external_id not in {None, expected_stream_id}
            ):
                if self._finalize_live_segments(store, downloader, job, media):
                    return
                store.cancel_job(
                    job,
                    reason_code="stream_replaced",
                    error=(
                        f"expected Twitch stream {expected_stream_id}, "
                        f"but channel is now streaming {recovery_probe.external_id}"
                    ),
                )
                return
            if (
                exc.retryable
                and (
                    recovery_probe is None
                    or (
                        recovery_probe.live_status == "is_live"
                        and recovery_probe.external_id in {None, expected_stream_id}
                    )
                )
            ):
                store.defer_job(
                    job,
                    reason_code="live_interrupted",
                    error=(
                        f"{self._safe_error(exc.cause)}; "
                        + (
                            "the same Twitch stream is still live, so recording "
                            "will reconnect"
                            if recovery_probe is not None
                            else (
                                "Twitch status could not be confirmed, so "
                                "recording will recheck without consuming its "
                                "failure budget"
                            )
                        )
                    ),
                    retry_seconds=self.config.twitch.live_retry_seconds,
                )
                return
            if (
                recovery_probe is not None
                and recovery_probe.live_status != "is_live"
                and self._finalize_live_segments(store, downloader, job, media)
            ):
                return
            store.fail_job(
                job,
                reason_code="download_failed",
                error=self._safe_error(exc.cause),
                retry_seconds=self.config.twitch.live_retry_seconds,
            )
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, RuntimeError) as exc:
            store.fail_job(
                job,
                reason_code="download_failed",
                error=self._safe_error(exc),
                retry_seconds=(
                    self.config.twitch.live_retry_seconds
                    if live_recording
                    else self.config.app.retry_seconds
                ),
            )
            return
        if live_recording:
            self._record_live_segment(
                store,
                job.media_id,
                result,
                reason="stream_finished",
            )
            completion_probe = self._probe_twitch_live(downloader, url)
            if (
                completion_probe is None
                or (
                    completion_probe.live_status == "is_live"
                    and completion_probe.external_id in {None, expected_stream_id}
                )
            ):
                store.defer_job(
                    job,
                    reason_code="live_interrupted",
                    error=(
                        "live downloader exited cleanly but the same Twitch "
                        "stream is still online; recording will reconnect"
                        if completion_probe is not None
                        else (
                            "live downloader exited cleanly but Twitch status "
                            "could not be confirmed; recording will recheck"
                        )
                    ),
                    retry_seconds=self.config.twitch.live_retry_seconds,
                )
                return
            if self._finalize_live_segments(store, downloader, job, media):
                return
            store.fail_job(
                job,
                reason_code="live_segment_missing",
                error="live download finished without a usable recording segment",
                retry_seconds=self.config.twitch.live_retry_seconds,
            )
            return
        if twitch_vod and store.has_ready_twitch_live_recording(stream_id):
            store.cancel_job(
                job,
                reason_code="live_recording_exists",
                error="matching Twitch live stream finished while its VOD was downloading",
            )
            return
        store.complete_download(
            job,
            path=result.file_path,
            size_bytes=result.file_size,
            delivery_targets=self._delivery_targets(),
            delivery_max_failures=self.config.app.max_attempts,
            live_retry_seconds=self.config.twitch.live_retry_seconds,
        )

    def _delivery_targets(self) -> tuple[str, ...]:
        return (self.telegram_destination_key,) if self.config.telegram.enabled else ()

    def _process_delivery_job(
        self,
        store: Store,
        downloader: Downloader,
        telegram: TelegramUploader,
        job: ClaimedJob,
    ) -> None:
        if not self.config.telegram.enabled:
            store.defer_job(
                job,
                reason_code="telegram_disabled",
                error="Telegram delivery is disabled",
                retry_seconds=self.config.app.retry_seconds,
            )
            return
        if job.target_key != self.telegram_destination_key:
            store.cancel_job(
                job,
                reason_code="destination_changed",
                error="delivery target no longer matches the configured Telegram destination",
            )
            return
        media = store.get_media(job.media_id)
        artifact = store.get_artifact(job.media_id, "master")
        if media is None:
            store.block_job(job, reason_code="media_missing", error="media item no longer exists")
            return
        if artifact is None or artifact["state"] != "ready" or not Path(str(artifact["path"])).exists():
            try:
                metadata = json.loads(str(media["metadata_json"] or "{}"))
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            if isinstance(metadata, dict) and metadata.get("recording_mode") == "live":
                store.block_job(
                    job,
                    reason_code="live_artifact_missing",
                    error="live recording artifact is missing and cannot be recreated after the stream",
                )
                return
            store.requeue_download(
                job.media_id,
                max_failures=self.config.app.max_attempts,
                reason="delivery artifact is missing",
            )
            store.defer_job(
                job,
                reason_code="artifact_missing",
                error="master artifact is missing; download requeued",
                retry_seconds=self.config.app.retry_seconds,
            )
            return

        master_path = Path(str(artifact["path"]))
        upload_path = master_path
        upload_artifact_id = int(artifact["id"])
        try:
            if self.config.telegram.media_type == "audio":
                prepared = downloader.shrink_audio_for_upload(
                    master_path,
                    self.config.telegram.max_upload_bytes,
                    force_audio=True,
                )
                upload_path = prepared.file_path
                if upload_path != master_path:
                    upload_artifact_id = store.record_artifact(
                        job.media_id,
                        role="telegram_upload",
                        path=upload_path,
                        size_bytes=prepared.file_size,
                        metadata={"derived_from": int(artifact["id"])},
                    )
            thumbnail_path = downloader.prepare_thumbnail_for_upload(
                str(media["external_id"]),
                provider=str(media["provider"]),
            )
            if thumbnail_path is not None:
                store.record_artifact(
                    job.media_id,
                    role="thumbnail",
                    path=thumbnail_path,
                    size_bytes=thumbnail_path.stat().st_size,
                )
            store.mark_delivery_sending(job)
            message_id = telegram.upload(
                upload_path,
                title=str(media["title"]),
                url=str(media["canonical_url"]),
                feed_name=store.primary_origin_name(job.media_id),
                video_id=str(media["external_id"]),
                thumbnail_path=thumbnail_path,
            )
        except TelegramUploadError as exc:
            message = self._safe_error(exc)
            if exc.uncertain:
                store.mark_job_uncertain(job, error=message)
            elif "exceeds telegram.max_upload_bytes" in message:
                store.block_job(job, reason_code="upload_too_large", error=message)
            else:
                store.fail_job(
                    job,
                    reason_code="telegram_failed",
                    error=message,
                    retry_seconds=self.config.app.retry_seconds,
                )
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            store.fail_job(
                job,
                reason_code="transcode_failed",
                error=self._safe_error(exc),
                retry_seconds=self.config.app.retry_seconds,
            )
            return
        try:
            store.complete_delivery(
                job,
                artifact_id=upload_artifact_id,
                destination_key=job.target_key,
                remote_id=str(message_id),
            )
        except Exception as exc:
            # The remote side effect already succeeded. Never turn a local
            # persistence failure into an automatic resend.
            self.logger.exception("could not persist successful Telegram delivery job=%s", job.id)
            try:
                store.mark_job_uncertain(job, error=self._safe_error(exc))
            except Exception:
                # If the database/lease is still unavailable, stale recovery
                # sees phase=sending and moves the job to uncertain.
                self.logger.exception("could not mark delivery uncertain job=%s", job.id)

    def _fail_unexpected(self, store: Store, job: ClaimedJob, exc: Exception) -> None:
        self.logger.exception("job failed unexpectedly id=%s type=%s", job.id, job.job_type)
        try:
            store.fail_job(
                job,
                reason_code="unexpected_error",
                error=self._safe_error(exc),
                retry_seconds=self.config.app.retry_seconds,
            )
        except RuntimeError:
            self.logger.warning("could not fail job id=%s because its lease was lost", job.id)

    def _new_source_registry(self) -> SourceRegistry:
        return SourceRegistry(
            self.config.twitch,
            youtube_fetcher=lambda url: fetch_feed(url),
            rss_fetcher=lambda url: fetch_feed(url),
        )

    def _all_origins(self, store: Store | None = None) -> list[Origin]:
        return (store or self.store).list_origins()

    def _is_live_recording_origin(self, origin: Origin) -> bool:
        try:
            return twitch_recording_mode(origin, self.config.twitch) == "live"
        except SourceError as exc:
            self.logger.warning("invalid Twitch recording mode origin=%s: %s", origin.id, exc)
            return False

    def _origin_seed_content_kind(self, origin: Origin) -> str | None:
        if origin.provider != "twitch":
            return None
        if origin.kind == "vods":
            return (
                "live_stream"
                if self._is_live_recording_origin(origin)
                else "vod"
            )
        return {
            "highlights": "highlight",
            "uploads": "upload",
        }.get(origin.kind)

    def _origin_poll_configuration_is_current(
        self,
        store: Store,
        polled_origin: Origin,
    ) -> bool:
        if polled_origin.provider != "twitch" or polled_origin.kind != "vods":
            return True
        current = next(
            (
                origin
                for origin in store.list_origins()
                if origin.id == polled_origin.id
            ),
            None,
        )
        if current is None or not current.enabled:
            return False
        try:
            return twitch_recording_mode(
                current,
                self.config.twitch,
            ) == twitch_recording_mode(
                polled_origin,
                self.config.twitch,
            )
        except SourceError:
            return False

    def _has_enabled_live_origin(self, store: Store, media_id: int) -> bool:
        media_origin_ids = {
            str(row["id"])
            for row in store.media_origins(media_id)
            if row["disposition"] == "eligible"
        }
        return any(
            origin.id in media_origin_ids
            and origin.enabled
            and self._is_live_recording_origin(origin)
            for origin in store.list_origins()
        )

    def _probe_twitch_live(
        self,
        downloader: Downloader,
        url: str,
    ) -> ProbeResult | None:
        try:
            return downloader.probe(url, provider="twitch", live=True)
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            FileNotFoundError,
            json.JSONDecodeError,
            RuntimeError,
        ):
            return None

    def _record_live_segment(
        self,
        store: Store,
        media_id: int,
        result: DownloadResult | None,
        *,
        reason: str,
    ) -> None:
        if result is None or not result.file_path.is_file() or result.file_size <= 0:
            return
        metadata: dict[str, object] = {"reason": reason}
        if result.attempt_order is not None:
            metadata["attempt_order"] = result.attempt_order
        store.record_live_segment(
            media_id,
            path=result.file_path,
            size_bytes=result.file_size,
            metadata=metadata,
        )

    def _finalize_live_segments(
        self,
        store: Store,
        downloader: Downloader,
        job: ClaimedJob,
        media,
    ) -> bool:
        paths = store.live_segment_paths(job.media_id)
        if not paths:
            return False
        try:
            result = downloader.merge_live_segments(
                str(media["external_id"]),
                paths,
                provider=str(media["provider"]),
            )
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            FileNotFoundError,
            RuntimeError,
        ) as exc:
            store.fail_job(
                job,
                reason_code="live_segment_merge_failed",
                error=self._safe_error(exc),
                retry_seconds=self.config.twitch.live_retry_seconds,
            )
            return True
        store.complete_download(
            job,
            path=result.file_path,
            size_bytes=result.file_size,
            delivery_targets=self._delivery_targets(),
            delivery_max_failures=self.config.app.max_attempts,
            live_retry_seconds=self.config.twitch.live_retry_seconds,
        )
        return True

    def _compiled_source_filter(self, store: Store | None = None):
        pattern = self._source_filter_pattern(store)
        try:
            return pattern, compile_source_filter(pattern)
        except ValueError as exc:
            self.logger.warning("%s; falling back to %s", exc, format_source_filter(DEFAULT_SOURCE_FILTER_PATTERN))
            return DEFAULT_SOURCE_FILTER_PATTERN, compile_source_filter(DEFAULT_SOURCE_FILTER_PATTERN)

    def _candidate_matches_filter(self, origin: Origin, candidate: MediaCandidate, source_filter) -> bool:
        return text_matches_source_filter(source_filter, origin.id, origin.name, candidate.title)

    def _download_delay_elapsed(self, first_seen_at: str) -> bool:
        first_seen = datetime.fromisoformat(first_seen_at)
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
        if self.config.control.enabled and not self.config.control.allowed_user_ids:
            self.logger.warning("control bot has no allowed_user_ids; chat-only authorization permits every member of an allowed chat")

    def _source_filter_pattern(self, store: Store | None = None) -> str | None:
        pattern = (store or self.store).get_bot_state(SOURCE_FILTER_STATE_KEY)
        if pattern is None:
            return DEFAULT_SOURCE_FILTER_PATTERN
        return pattern or None

    def _safe_error(self, exc: BaseException) -> str:
        if isinstance(exc, subprocess.TimeoutExpired):
            return f"external command timed out after {exc.timeout} seconds"
        if isinstance(exc, subprocess.CalledProcessError):
            detail = (exc.stderr or exc.stdout or "").strip()
            text = f"external command exited with code {exc.returncode}"
            if detail:
                text += f": {detail[:1000]}"
        else:
            text = str(exc)
        for secret in (
            self.config.telegram.bot_token,
            self.config.twitch.access_token,
            self.config.twitch.client_secret,
        ):
            if secret:
                text = text.replace(secret, "<redacted>")
        return text[:2000]


class _LeaseHeartbeat:
    def __init__(
        self,
        db_path: Path,
        job: ClaimedJob,
        lease_seconds: int,
        logger: logging.Logger,
    ):
        self.db_path = db_path
        self.job = job
        self.lease_seconds = lease_seconds
        self.logger = logger
        self._stop = threading.Event()
        self.lost_event = threading.Event()
        self.ready_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"lease-heartbeat-{job.id}", daemon=True)

    def start(self) -> bool:
        self._thread.start()
        startup_timeout = max(1.0, min(5.0, self.lease_seconds * 0.2))
        if not self.ready_event.wait(timeout=startup_timeout):
            self.logger.warning("lease heartbeat did not become ready job id=%s", self.job.id)
            self._stop.set()
            self._thread.join(timeout=2)
            return False
        return not self.lost_event.is_set()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        interval = max(5, min(30, self.lease_seconds // 3))
        last_renewed = time.monotonic()
        next_wait = 0
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=1)
            conn.execute("PRAGMA busy_timeout=1000")
            while not self._stop.wait(next_wait):
                try:
                    cursor = conn.execute(
                        """
                        UPDATE jobs SET lease_until=?, updated_at=?
                        WHERE id=? AND state='running' AND lease_token=?
                        """,
                        (
                            future_iso(self.lease_seconds),
                            now_iso(),
                            self.job.id,
                            self.job.lease_token,
                        ),
                    )
                    conn.commit()
                    if cursor.rowcount != 1:
                        self.logger.warning("lease heartbeat lost job id=%s", self.job.id)
                        self.lost_event.set()
                        return
                    last_renewed = time.monotonic()
                    self.ready_event.set()
                    next_wait = interval
                except sqlite3.Error as exc:
                    if conn.in_transaction:
                        conn.rollback()
                    elapsed = time.monotonic() - last_renewed
                    self.logger.warning(
                        "lease heartbeat retrying job id=%s elapsed=%.1fs: %s",
                        self.job.id,
                        elapsed,
                        exc,
                    )
                    if elapsed >= max(5, self.lease_seconds * 0.8):
                        self.logger.warning("lease heartbeat gave up job id=%s", self.job.id)
                        self.lost_event.set()
                        return
                    next_wait = 1
        except Exception as exc:
            self.logger.warning(
                "lease heartbeat crashed job id=%s: %s",
                self.job.id,
                exc,
            )
            self.lost_event.set()
        finally:
            self.ready_event.set()
            if conn is not None:
                conn.close()
