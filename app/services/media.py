import logging
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO

from app.config import settings
from app.models import MediaType

log = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".mpeg", ".mpg"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".flac", ".opus", ".wma"}
CHUNK_SIZE = 1024 * 1024


class MediaError(Exception):
    pass


class UploadTooLarge(MediaError):
    pass


def detect_media_type(filename: str) -> MediaType | None:
    ext = Path(filename).suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return MediaType.VIDEO
    if ext in AUDIO_EXTENSIONS:
        return MediaType.AUDIO
    return None


def save_upload(src: BinaryIO, dest: Path, max_bytes: int) -> int:
    """Stream an upload to disk in chunks, aborting (and cleaning up) if it exceeds max_bytes."""
    written = 0
    try:
        with dest.open("wb") as out:
            while chunk := src.read(CHUNK_SIZE):
                written += len(chunk)
                if written > max_bytes:
                    raise UploadTooLarge(f"File is larger than {max_bytes // (1024 * 1024)} MB")
                out.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return written


@lru_cache(maxsize=1)
def ffmpeg_binary() -> str:
    if settings.ffmpeg_binary:
        return settings.ffmpeg_binary
    if found := shutil.which("ffmpeg"):
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        raise MediaError("ffmpeg not found: install it, add it to PATH, or set FFMPEG_BINARY") from exc


def extract_audio(src: Path, dest: Path, timeout: int = 60 * 60) -> Path:
    """Extract the audio track as 16 kHz mono 16-bit PCM WAV, the format Whisper works on natively."""
    cmd = [
        ffmpeg_binary(),
        "-hide_banner", "-nostdin", "-y",
        "-i", str(src),
        "-vn",                 # drop video
        "-ac", "1",            # mono
        "-ar", "16000",        # 16 kHz
        "-c:a", "pcm_s16le",
        str(dest),
    ]
    log.info("Extracting audio: %s -> %s", src.name, dest.name)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        dest.unlink(missing_ok=True)
        raise MediaError("ffmpeg timed out while extracting audio") from exc

    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        tail = "\n".join(proc.stderr.strip().splitlines()[-8:])
        raise MediaError(f"ffmpeg failed to extract audio (no audio track?):\n{tail}")
    return dest
