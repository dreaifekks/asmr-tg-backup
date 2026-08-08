from __future__ import annotations

from dataclasses import dataclass
import glob
import json
import logging
import os
from pathlib import Path
import signal
import shutil
import subprocess
import tempfile
import threading
import time

from .config import Config, DownloadProfile
from .proxy import build_proxy_env


WAIT_LIVE_STATUSES = {"is_live", "is_upcoming", "post_live"}
MEDIA_EXTENSIONS = {
    ".m4a",
    ".mp3",
    ".opus",
    ".ogg",
    ".aac",
    ".flac",
    ".wav",
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".avi",
    ".m4v",
    ".ts",
}
AUDIO_EXTENSIONS = {".m4a", ".mp3", ".opus", ".ogg", ".aac", ".flac", ".wav"}
THUMBNAIL_EXTENSIONS = {".jpg", ".jpeg", ".webp", ".png"}
TELEGRAM_THUMBNAIL_MAX_BYTES = 200_000


@dataclass(frozen=True)
class ProbeResult:
    live_status: str | None
    title: str | None
    external_id: str | None = None


@dataclass(frozen=True)
class DownloadResult:
    file_path: Path
    file_size: int
    attempt_order: int | None = None


class DownloadCancelled(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        partial_result: DownloadResult | None = None,
    ):
        super().__init__(message)
        self.partial_result = partial_result


class LiveDownloadError(RuntimeError):
    def __init__(
        self,
        cause: BaseException,
        *,
        partial_result: DownloadResult | None = None,
    ):
        super().__init__(str(cause))
        self.cause = cause
        self.partial_result = partial_result
        self.retryable = _is_retryable_live_failure(cause)


class Downloader:
    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self._proxy_env = build_proxy_env(
            config.proxy.url if config.proxy.downloads else ""
        )

    def check_tools(self) -> list[str]:
        missing = []
        if shutil.which(self.config.download.yt_dlp) is None:
            missing.append(self.config.download.yt_dlp)
        profile_needs_ffmpeg = any(
            profile.extract_audio is True or (
                profile.extract_audio is False and bool(profile.merge_output_format)
            )
            for profile in self.config.download.provider_profiles.values()
        )
        if (self.config.download.extract_audio or profile_needs_ffmpeg) and not self.config.download.ffmpeg:
            missing.append("ffmpeg")
        elif self.config.download.ffmpeg and shutil.which(self.config.download.ffmpeg) is None:
            missing.append(self.config.download.ffmpeg)
        return missing

    def probe(
        self,
        url: str,
        *,
        provider: str = "youtube",
        live: bool = False,
    ) -> ProbeResult:
        cmd = [
            self.config.download.yt_dlp,
            "--no-warnings",
            "--skip-download",
        ]
        if provider == "youtube":
            # Upcoming/live YouTube pages can expose useful metadata before
            # formats are available. Keep that metadata so the service can
            # defer without consuming the job's failure budget.
            cmd.append("--ignore-no-formats-error")
        cmd.extend(
            [
                "--dump-single-json",
                "--no-playlist",
            ]
        )
        cmd.extend(self._extra_args(provider))
        cmd.append(url)
        try:
            completed = subprocess.run(
                cmd,
                check=True,
                text=True,
                capture_output=True,
                timeout=self.config.download.probe_timeout_seconds,
                env=self._proxy_env,
            )
        except subprocess.CalledProcessError as exc:
            detail = f"{exc.stderr or ''}\n{exc.stdout or ''}".lower()
            if live and provider == "twitch" and _is_twitch_offline_error(detail):
                return ProbeResult(live_status="not_live", title=None)
            raise
        data = json.loads(completed.stdout)
        return ProbeResult(
            live_status=data.get("live_status"),
            title=data.get("title"),
            external_id=str(data["id"]) if data.get("id") is not None else None,
        )

    def download(
        self,
        video_id: str,
        url: str,
        *,
        provider: str = "youtube",
        ignore_archive: bool = False,
        live: bool = False,
        cancel_events: tuple[threading.Event, ...] = (),
    ) -> DownloadResult:
        self.config.download_dir.mkdir(parents=True, exist_ok=True)
        archive_file = self.archive_file_for_provider(provider)
        archive_file.parent.mkdir(parents=True, exist_ok=True)
        provider_dir = self.config.download_dir / _safe_provider_name(provider)
        provider_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        profile = self.config.download.provider_profiles.get(provider)
        download_format = _profile_value(profile, "format", self.config.download.format)
        merge_output_format = _profile_value(
            profile,
            "merge_output_format",
            self.config.download.merge_output_format,
        )
        extract_audio = bool(_profile_value(profile, "extract_audio", self.config.download.extract_audio))
        audio_format = _profile_value(profile, "audio_format", self.config.download.audio_format)
        audio_quality = _profile_value(profile, "audio_quality", self.config.download.audio_quality)

        live_attempt_token = f"{time.time_ns():x}" if live else None
        output_template = (
            _live_output_template(
                self.config.download.output_template,
                live_attempt_token,
            )
            if live_attempt_token
            else self.config.download.output_template
        )
        cmd = [
            self.config.download.yt_dlp,
            "--no-playlist",
            "--paths",
            f"home:{provider_dir}",
            "--output",
            output_template,
            "--format",
            str(download_format),
            "--print",
            "after_move:filepath",
        ]
        if ignore_archive or live:
            cmd.append("--no-download-archive")
        else:
            cmd.extend(["--download-archive", str(archive_file)])
        if self.config.download.ffmpeg:
            cmd.extend(["--ffmpeg-location", self.config.download.ffmpeg])
        if extract_audio:
            cmd.append("--extract-audio")
            if audio_format:
                cmd.extend(["--audio-format", str(audio_format)])
            if audio_quality:
                cmd.extend(["--audio-quality", str(audio_quality)])
        elif self.config.download.ffmpeg and merge_output_format:
            cmd.extend(["--merge-output-format", str(merge_output_format)])
        if self.config.download.restrict_filenames:
            cmd.append("--restrict-filenames")
        if self.config.download.write_info_json:
            cmd.append("--write-info-json")
        if self.config.download.write_thumbnail:
            cmd.append("--write-thumbnail")
        cmd.extend(self._extra_args(provider))
        if live:
            # Twitch's experimental --live-from-start path depends on an
            # associated VOD, which may already be subscriber-only. Recording
            # from the current HLS position is the reliable archival path.
            cmd.extend(
                [
                    "--no-live-from-start",
                    "--retries",
                    "infinite",
                    "--fragment-retries",
                    "infinite",
                    "--hls-use-mpegts",
                    "--no-part",
                    "--downloader-args",
                    (
                        "ffmpeg_i:-reconnect 1 -reconnect_streamed 1 "
                        "-reconnect_on_network_error 1 "
                        "-reconnect_on_http_error 5xx "
                        "-reconnect_delay_max 10 "
                        "-reconnect_delay_total_max 300"
                    ),
                    "--no-progress",
                    "--no-match-filters",
                    "--match-filters",
                    f"id = {video_id}",
                ]
            )
        cmd.append(url)

        self.logger.info("downloading video_id=%s", video_id)
        if live:
            try:
                completed = self._run_live_download(cmd, cancel_events=cancel_events)
            except DownloadCancelled as exc:
                raise DownloadCancelled(
                    str(exc),
                    partial_result=self._find_live_attempt_result(
                        video_id,
                        str(live_attempt_token),
                        provider=provider,
                    ),
                ) from None
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                raise LiveDownloadError(
                    exc,
                    partial_result=self._find_live_attempt_result(
                        video_id,
                        str(live_attempt_token),
                        provider=provider,
                    ),
                ) from exc
        else:
            completed = subprocess.run(
                cmd,
                check=True,
                text=True,
                capture_output=True,
                timeout=self.config.download.download_timeout_seconds,
                env=self._proxy_env,
            )
        printed_paths = [Path(line.strip()) for line in completed.stdout.splitlines() if line.strip()]
        candidates = [path for path in printed_paths if _looks_like_media(path)]
        if not candidates:
            candidates = (
                self._find_live_attempt_files(
                    video_id,
                    str(live_attempt_token),
                    provider=provider,
                )
                if live
                else self._find_downloaded_file(video_id, provider=provider)
            )
        if not candidates:
            raise RuntimeError("yt-dlp finished but no downloaded video file was found")
        file_path = max(candidates, key=lambda item: item.stat().st_size if item.exists() else -1)
        return DownloadResult(
            file_path=file_path,
            file_size=file_path.stat().st_size,
            attempt_order=(
                int(str(live_attempt_token), 16)
                if live_attempt_token is not None
                else None
            ),
        )

    def merge_live_segments(
        self,
        video_id: str,
        paths: list[Path],
        *,
        provider: str = "twitch",
    ) -> DownloadResult:
        segments = list(
            dict.fromkeys(
                path.resolve()
                for path in paths
                if path.is_file() and path.stat().st_size > 0
            )
        )
        if not segments:
            raise RuntimeError("no live recording segments are available")
        ffmpeg = self.config.download.ffmpeg
        if not ffmpeg:
            raise RuntimeError("ffmpeg is required to merge live recording segments")
        profile = self.config.download.provider_profiles.get(provider)
        audio_only = bool(
            _profile_value(
                profile,
                "extract_audio",
                self.config.download.extract_audio,
            )
        )
        output_suffix = ".m4a" if audio_only else ".mp4"
        output = segments[0].with_name(
            f"{_safe_provider_name(provider)}_{video_id}.live-merged"
            f"{output_suffix}"
        )
        with tempfile.TemporaryDirectory(
            prefix=".live-merge-",
            dir=output.parent,
        ) as temp_dir:
            temp_path = Path(temp_dir)
            normalized: list[Path] = []
            for index, segment in enumerate(segments):
                normalized_path = temp_path / (
                    f"{index:04d}.mka" if audio_only else f"{index:04d}.mkv"
                )
                normalize_cmd = [
                    ffmpeg,
                    "-nostdin",
                    "-y",
                    "-fflags",
                    "+genpts",
                    "-err_detect",
                    "ignore_err",
                    "-i",
                    str(segment),
                    "-map_metadata",
                    "-1",
                ]
                if audio_only:
                    # An interrupted yt-dlp post-processing attempt may still
                    # be MPEG-TS/AAC (even when its name ends in .mp4), while a
                    # clean attempt is normally M4A. Re-encoding audio is cheap
                    # and gives the concat step one consistent stream shape.
                    normalize_cmd.extend(
                        [
                            "-map",
                            "0:a:0",
                            "-vn",
                            "-c:a",
                            "aac",
                            "-b:a",
                            "128k",
                        ]
                    )
                else:
                    # Matroska accepts the normal Twitch HLS codecs and avoids
                    # relying on a possibly misleading interrupted .mp4 suffix.
                    normalize_cmd.extend(
                        [
                            "-map",
                            "0:v:0?",
                            "-map",
                            "0:a:0?",
                            "-c",
                            "copy",
                            "-avoid_negative_ts",
                            "make_zero",
                        ]
                    )
                normalize_cmd.append(str(normalized_path))
                subprocess.run(
                    normalize_cmd,
                    check=True,
                    text=True,
                    capture_output=True,
                    timeout=self.config.download.ffmpeg_timeout_seconds,
                )
                normalized.append(normalized_path)

            concat_path = temp_path / "segments.ffconcat"
            concat_path.write_text(
                "".join(
                    f"file '{_escape_ffconcat_path(segment)}'\n"
                    for segment in normalized
                ),
                encoding="utf-8",
            )
            concat_path.chmod(0o600)
            joined_path = temp_path / ("joined.mka" if audio_only else "joined.mkv")
            concat_cmd = [
                ffmpeg,
                "-nostdin",
                "-y",
                "-fflags",
                "+genpts",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_path),
                "-map",
                "0",
                "-c",
                "copy",
                str(joined_path),
            ]
            subprocess.run(
                concat_cmd,
                check=True,
                text=True,
                capture_output=True,
                timeout=self.config.download.ffmpeg_timeout_seconds,
            )
            finalize_cmd = [
                ffmpeg,
                "-nostdin",
                "-y",
                "-i",
                str(joined_path),
            ]
            if audio_only:
                finalize_cmd.extend(["-map", "0:a:0"])
            else:
                finalize_cmd.extend(
                    ["-map", "0:v:0", "-map", "0:a:0?"]
                )
            finalize_cmd.extend(
                [
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(temp_path / f"final{output_suffix}"),
                ]
            )
            subprocess.run(
                finalize_cmd,
                check=True,
                text=True,
                capture_output=True,
                timeout=self.config.download.ffmpeg_timeout_seconds,
            )
            os.replace(temp_path / f"final{output_suffix}", output)
        return DownloadResult(file_path=output, file_size=output.stat().st_size)

    def shrink_audio_for_upload(
        self,
        file_path: Path,
        max_bytes: int,
        *,
        force_audio: bool = False,
    ) -> DownloadResult:
        needs_audio_derivative = force_audio and file_path.suffix.lower() not in AUDIO_EXTENSIONS
        if not needs_audio_derivative and file_path.stat().st_size <= max_bytes:
            return DownloadResult(file_path=file_path, file_size=file_path.stat().st_size)
        ffmpeg = self.config.download.ffmpeg
        if not ffmpeg:
            return DownloadResult(file_path=file_path, file_size=file_path.stat().st_size)

        if needs_audio_derivative:
            copied = self._copy_audio_for_upload(file_path, max_bytes)
            if copied is not None:
                return copied

        duration = self._duration_seconds(file_path)
        bitrate = _target_audio_bitrate_kbps(max_bytes=max_bytes, duration_seconds=duration)
        with tempfile.TemporaryDirectory(
            prefix=".tg-audio-encode-",
            dir=file_path.parent,
        ) as temp_dir:
            for candidate_bitrate in _bitrate_candidates(bitrate):
                output = file_path.with_name(f"{file_path.stem}.tg{candidate_bitrate}k.m4a")
                temporary_output = Path(temp_dir) / f"audio-{candidate_bitrate}k.m4a"
                cmd = [
                    ffmpeg,
                    "-nostdin",
                    "-y",
                    "-i",
                    str(file_path),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-c:a",
                    "aac",
                    "-b:a",
                    f"{candidate_bitrate}k",
                    "-movflags",
                    "+faststart",
                    str(temporary_output),
                ]
                self.logger.info("shrinking audio for upload path=%s bitrate=%sk", file_path, candidate_bitrate)
                subprocess.run(
                    cmd,
                    check=True,
                    text=True,
                    capture_output=True,
                    timeout=self.config.download.ffmpeg_timeout_seconds,
                )
                size = temporary_output.stat().st_size
                if size <= max_bytes or candidate_bitrate == 24:
                    os.replace(temporary_output, output)
                    return DownloadResult(file_path=output, file_size=size)
        return DownloadResult(file_path=file_path, file_size=file_path.stat().st_size)

    def _copy_audio_for_upload(
        self,
        file_path: Path,
        max_bytes: int,
    ) -> DownloadResult | None:
        """Extract an M4A-compatible audio stream without another lossy encode."""
        output = file_path.with_name(f"{file_path.stem}.tgaudio.m4a")
        with tempfile.TemporaryDirectory(
            prefix=".tg-audio-copy-",
            dir=output.parent,
        ) as temp_dir:
            temporary_output = Path(temp_dir) / "audio.m4a"
            cmd = [
                self.config.download.ffmpeg,
                "-nostdin",
                "-y",
                "-i",
                str(file_path),
                "-map",
                "0:a:0",
                "-vn",
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
                str(temporary_output),
            ]
            self.logger.info("copying audio for upload path=%s", file_path)
            try:
                subprocess.run(
                    cmd,
                    check=True,
                    text=True,
                    capture_output=True,
                    timeout=self.config.download.ffmpeg_timeout_seconds,
                )
            except subprocess.CalledProcessError as exc:
                self.logger.info(
                    "audio stream copy unavailable; falling back to AAC encoding path=%s error=%s",
                    file_path,
                    exc,
                )
                return None
            size = temporary_output.stat().st_size
            if size > max_bytes:
                self.logger.info(
                    "copied audio exceeds upload limit; falling back to AAC encoding path=%s size=%s",
                    file_path,
                    size,
                )
                return None
            os.replace(temporary_output, output)
        return DownloadResult(file_path=output, file_size=output.stat().st_size)

    def prepare_thumbnail_for_upload(self, video_id: str, *, provider: str | None = None) -> Path | None:
        source = self._find_thumbnail_file(video_id, provider=provider)
        if source is None:
            return None
        ffmpeg = self.config.download.ffmpeg
        if not ffmpeg:
            if source.suffix.lower() in {".jpg", ".jpeg"} and source.stat().st_size < TELEGRAM_THUMBNAIL_MAX_BYTES:
                return source
            return None

        output = source.with_name(f"{source.stem}.tgthumb.jpg")
        for quality in (4, 7, 10, 13, 16, 20, 24, 28, 31):
            cmd = [
                ffmpeg,
                "-y",
                "-i",
                str(source),
                "-vf",
                "scale='min(320,iw)':'min(320,ih)':force_original_aspect_ratio=decrease",
                "-frames:v",
                "1",
                "-q:v",
                str(quality),
                str(output),
            ]
            try:
                subprocess.run(
                    cmd,
                    check=True,
                    text=True,
                    capture_output=True,
                    timeout=self.config.download.ffmpeg_timeout_seconds,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
                self.logger.warning("thumbnail conversion failed path=%s error=%s", source, exc)
                return None
            if output.exists() and output.stat().st_size < TELEGRAM_THUMBNAIL_MAX_BYTES:
                return output
        self.logger.warning("thumbnail too large after conversion path=%s", output)
        return None

    def _find_downloaded_file(self, video_id: str, *, provider: str | None = None) -> list[Path]:
        search_root = self.config.download_dir / _safe_provider_name(provider) if provider else self.config.download_dir
        if not search_root.exists():
            search_root = self.config.download_dir
        return [
            path
            for path in search_root.rglob(f"*{glob.escape(video_id)}*")
            if _looks_like_media(path)
        ]

    def _find_live_attempt_files(
        self,
        video_id: str,
        attempt_token: str,
        *,
        provider: str,
    ) -> list[Path]:
        return [
            path
            for path in self._find_downloaded_file(video_id, provider=provider)
            if f"live-{attempt_token}" in path.name
        ]

    def _find_live_attempt_result(
        self,
        video_id: str,
        attempt_token: str,
        *,
        provider: str,
    ) -> DownloadResult | None:
        candidates = self._find_live_attempt_files(
            video_id,
            attempt_token,
            provider=provider,
        )
        if not candidates:
            return None
        path = max(candidates, key=lambda item: item.stat().st_size)
        return DownloadResult(
            file_path=path,
            file_size=path.stat().st_size,
            attempt_order=int(attempt_token, 16),
        )

    def _find_thumbnail_file(self, video_id: str, *, provider: str | None = None) -> Path | None:
        search_root = self.config.download_dir / _safe_provider_name(provider) if provider else self.config.download_dir
        if not search_root.exists():
            search_root = self.config.download_dir
        candidates = [
            path
            for path in search_root.rglob(f"*{glob.escape(video_id)}*")
            if _looks_like_thumbnail(path)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.stat().st_size if item.exists() else -1)

    def _duration_seconds(self, file_path: Path) -> float:
        ffprobe = _ffprobe_for(self.config.download.ffmpeg)
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(file_path),
            ],
            check=True,
            text=True,
            capture_output=True,
            timeout=self.config.download.ffmpeg_timeout_seconds,
        )
        return float(completed.stdout.strip())

    def _extra_args(self, provider: str) -> list[str]:
        profile = self.config.download.provider_profiles.get(provider)
        provider_args = profile.extra_args if profile and profile.extra_args else []
        return [*self.config.download.extra_args, *provider_args]

    def archive_file_for_provider(self, provider: str) -> Path:
        base = self.config.archive_file
        safe_provider = _safe_provider_name(provider)
        if safe_provider == "youtube":
            return base
        suffix = base.suffix
        name = f"{base.stem}.{safe_provider}{suffix}" if suffix else f"{base.name}.{safe_provider}"
        return base.with_name(name)

    def _run_live_download(
        self,
        cmd: list[str],
        *,
        cancel_events: tuple[threading.Event, ...],
    ) -> subprocess.CompletedProcess[str]:
        timeout_seconds = self.config.twitch.live_download_timeout_seconds
        deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else None
        process = subprocess.Popen(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=self._proxy_env,
        )
        while True:
            try:
                stdout, stderr = process.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                if any(event.is_set() for event in cancel_events):
                    stdout, stderr = self._terminate_process_group(process)
                    raise DownloadCancelled("live recording cancelled") from None
                if deadline is not None and time.monotonic() >= deadline:
                    stdout, stderr = self._terminate_process_group(process)
                    raise subprocess.TimeoutExpired(
                        cmd=cmd,
                        timeout=timeout_seconds,
                        output=stdout,
                        stderr=stderr,
                    ) from None
        completed = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
        if process.returncode:
            raise subprocess.CalledProcessError(
                process.returncode,
                cmd,
                output=stdout,
                stderr=stderr,
            )
        return completed

    @staticmethod
    def _terminate_process_group(
        process: subprocess.Popen[str],
    ) -> tuple[str, str]:
        if process.poll() is not None:
            return process.communicate()
        try:
            os.killpg(process.pid, signal.SIGINT)
        except (ProcessLookupError, PermissionError):
            process.send_signal(signal.SIGINT)
        try:
            return process.communicate(timeout=12)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            process.terminate()
        try:
            return process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        return process.communicate()


def _looks_like_media(path: Path) -> bool:
    return path.exists() and path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS


def _looks_like_thumbnail(path: Path) -> bool:
    return (
        path.exists()
        and path.is_file()
        and path.suffix.lower() in THUMBNAIL_EXTENSIONS
        and ".tgthumb" not in path.name
    )


def _escape_ffconcat_path(path: Path) -> str:
    return str(path).replace("'", "'\\''")


def _ffprobe_for(ffmpeg: str) -> str:
    ffmpeg_path = Path(ffmpeg)
    if ffmpeg_path.name == "ffmpeg":
        return str(ffmpeg_path.with_name("ffprobe"))
    return "ffprobe"


def _target_audio_bitrate_kbps(*, max_bytes: int, duration_seconds: float) -> int:
    if duration_seconds <= 0:
        return 40
    # Leave container overhead and Telegram multipart headroom.
    kbps = int((max_bytes * 8 * 0.90) / duration_seconds / 1000)
    return max(24, min(256, kbps))


def _bitrate_candidates(start_kbps: int) -> list[int]:
    start_kbps = max(24, min(256, start_kbps))
    candidates = {
        start_kbps,
        max(24, start_kbps - 8),
        max(24, start_kbps - 16),
        192,
        128,
        96,
        64,
        48,
        40,
        32,
        24,
    }
    return sorted(
        (value for value in candidates if value <= start_kbps),
        reverse=True,
    )


def _safe_provider_name(provider: str | None) -> str:
    value = provider or "unknown"
    cleaned = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in value)
    return cleaned or "unknown"


def _live_output_template(template: str, attempt_token: str) -> str:
    marker = f"live-{attempt_token}."
    head, separator, tail = template.rpartition("%(ext)s")
    if separator:
        return f"{head}{marker}{separator}{tail}"
    return f"{template}.{marker}%(ext)s"


def _is_twitch_offline_error(detail: str) -> bool:
    return any(
        marker in detail
        for marker in (
            "is offline",
            "not currently live",
            "is not live",
            "channel is offline",
        )
    )


def _is_retryable_live_failure(exc: BaseException) -> bool:
    if isinstance(exc, subprocess.TimeoutExpired):
        return True
    if not isinstance(exc, subprocess.CalledProcessError):
        return False
    detail = f"{exc.stderr or ''}\n{exc.stdout or ''}".lower()
    return any(
        marker in detail
        for marker in (
            "connection reset",
            "connection refused",
            "connection timed out",
            "network is unreachable",
            "temporary failure",
            "server returned 5",
            "http error 5",
            "input/output error",
            "i/o error",
            "error in the pull function",
            "end of file",
        )
    )


def _profile_value(profile: DownloadProfile | None, name: str, fallback: object) -> object:
    if profile is None:
        return fallback
    value = getattr(profile, name)
    return fallback if value is None else value
