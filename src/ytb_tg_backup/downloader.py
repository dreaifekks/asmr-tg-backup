from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import shutil
import subprocess

from .config import Config


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
}
THUMBNAIL_EXTENSIONS = {".jpg", ".jpeg", ".webp", ".png"}
TELEGRAM_THUMBNAIL_MAX_BYTES = 200_000


@dataclass(frozen=True)
class ProbeResult:
    live_status: str | None
    title: str | None


@dataclass(frozen=True)
class DownloadResult:
    file_path: Path
    file_size: int


class Downloader:
    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger

    def check_tools(self) -> list[str]:
        missing = []
        if shutil.which(self.config.download.yt_dlp) is None:
            missing.append(self.config.download.yt_dlp)
        if self.config.download.extract_audio and not self.config.download.ffmpeg:
            missing.append("ffmpeg")
        elif self.config.download.ffmpeg and shutil.which(self.config.download.ffmpeg) is None:
            missing.append(self.config.download.ffmpeg)
        return missing

    def probe(self, url: str) -> ProbeResult:
        cmd = [
            self.config.download.yt_dlp,
            "--no-warnings",
            "--skip-download",
            "--dump-single-json",
            "--no-playlist",
            url,
        ]
        completed = subprocess.run(cmd, check=True, text=True, capture_output=True, timeout=180)
        data = json.loads(completed.stdout)
        return ProbeResult(live_status=data.get("live_status"), title=data.get("title"))

    def download(self, video_id: str, url: str) -> DownloadResult:
        self.config.download_dir.mkdir(parents=True, exist_ok=True)
        self.config.archive_file.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            self.config.download.yt_dlp,
            "--no-playlist",
            "--paths",
            f"home:{self.config.download_dir}",
            "--output",
            self.config.download.output_template,
            "--download-archive",
            str(self.config.archive_file),
            "--format",
            self.config.download.format,
            "--print",
            "after_move:filepath",
        ]
        if self.config.download.ffmpeg:
            cmd.extend(["--ffmpeg-location", self.config.download.ffmpeg])
        if self.config.download.extract_audio:
            cmd.append("--extract-audio")
            if self.config.download.audio_format:
                cmd.extend(["--audio-format", self.config.download.audio_format])
            if self.config.download.audio_quality:
                cmd.extend(["--audio-quality", self.config.download.audio_quality])
        elif self.config.download.ffmpeg and self.config.download.merge_output_format:
            cmd.extend(["--merge-output-format", self.config.download.merge_output_format])
        if self.config.download.restrict_filenames:
            cmd.append("--restrict-filenames")
        if self.config.download.write_info_json:
            cmd.append("--write-info-json")
        if self.config.download.write_thumbnail:
            cmd.append("--write-thumbnail")
        cmd.extend(self.config.download.extra_args)
        cmd.append(url)

        self.logger.info("downloading video_id=%s", video_id)
        completed = subprocess.run(cmd, check=True, text=True, capture_output=True)
        printed_paths = [Path(line.strip()) for line in completed.stdout.splitlines() if line.strip()]
        candidates = [path for path in printed_paths if _looks_like_media(path)]
        if not candidates:
            candidates = self._find_downloaded_file(video_id)
        if not candidates:
            raise RuntimeError("yt-dlp finished but no downloaded video file was found")
        file_path = max(candidates, key=lambda item: item.stat().st_size if item.exists() else -1)
        return DownloadResult(file_path=file_path, file_size=file_path.stat().st_size)

    def shrink_audio_for_upload(self, file_path: Path, max_bytes: int) -> DownloadResult:
        if not self.config.download.extract_audio or file_path.stat().st_size <= max_bytes:
            return DownloadResult(file_path=file_path, file_size=file_path.stat().st_size)
        ffmpeg = self.config.download.ffmpeg
        if not ffmpeg:
            return DownloadResult(file_path=file_path, file_size=file_path.stat().st_size)

        duration = self._duration_seconds(file_path)
        bitrate = _target_audio_bitrate_kbps(max_bytes=max_bytes, duration_seconds=duration)
        for candidate_bitrate in _bitrate_candidates(bitrate):
            output = file_path.with_name(f"{file_path.stem}.tg{candidate_bitrate}k.m4a")
            cmd = [
                ffmpeg,
                "-y",
                "-i",
                str(file_path),
                "-vn",
                "-c:a",
                "aac",
                "-b:a",
                f"{candidate_bitrate}k",
                "-movflags",
                "+faststart",
                str(output),
            ]
            self.logger.info("shrinking audio for upload path=%s bitrate=%sk", file_path, candidate_bitrate)
            subprocess.run(cmd, check=True, text=True, capture_output=True)
            size = output.stat().st_size
            if size <= max_bytes or candidate_bitrate == 24:
                return DownloadResult(file_path=output, file_size=size)
        return DownloadResult(file_path=file_path, file_size=file_path.stat().st_size)

    def prepare_thumbnail_for_upload(self, video_id: str) -> Path | None:
        source = self._find_thumbnail_file(video_id)
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
                subprocess.run(cmd, check=True, text=True, capture_output=True)
            except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                self.logger.warning("thumbnail conversion failed path=%s error=%s", source, exc)
                return None
            if output.exists() and output.stat().st_size < TELEGRAM_THUMBNAIL_MAX_BYTES:
                return output
        self.logger.warning("thumbnail too large after conversion path=%s", output)
        return None

    def _find_downloaded_file(self, video_id: str) -> list[Path]:
        return [
            path
            for path in self.config.download_dir.rglob(f"*{video_id}*")
            if _looks_like_media(path)
        ]

    def _find_thumbnail_file(self, video_id: str) -> Path | None:
        candidates = [
            path
            for path in self.config.download_dir.rglob(f"*{video_id}*")
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
        )
        return float(completed.stdout.strip())


def _looks_like_media(path: Path) -> bool:
    return path.exists() and path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS


def _looks_like_thumbnail(path: Path) -> bool:
    return (
        path.exists()
        and path.is_file()
        and path.suffix.lower() in THUMBNAIL_EXTENSIONS
        and ".tgthumb" not in path.name
    )


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
    return max(24, min(64, kbps))


def _bitrate_candidates(start_kbps: int) -> list[int]:
    candidates = [start_kbps, start_kbps - 8, start_kbps - 16, 32, 24]
    clean: list[int] = []
    for value in candidates:
        value = max(24, min(64, value))
        if value not in clean:
            clean.append(value)
    return clean
