"""Import a lecture from a link: YouTube (and the other sites yt-dlp supports) or a direct video/audio URL.

The file is downloaded once into data/uploads/, after which the lecture goes through the normal
pipeline (FFmpeg -> Whisper -> notes) like an uploaded file.
"""

import ipaddress
import logging
import re
import shutil
import socket
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

from app.config import settings
from app.services import jobs, media

log = logging.getLogger(__name__)

# Prefer H.264 + AAC at up to 720p: every browser plays it (AV1, which YouTube often serves, needs an extra
# codec on Windows/Edge). Then VP9, then anything that isn't AV1, and only as a last resort whatever exists.
FORMAT = (
    "bv*[height<=720][vcodec^=avc1]+ba[ext=m4a]/b[height<=720][vcodec^=avc1]"
    "/bv*[height<=720][vcodec^=vp9]+ba/bv*[height<=720][vcodec!^=av01]+ba/b[height<=720][vcodec!^=av01]"
    "/bv*[height<=720]+ba/b"
)


class DownloadError(Exception):
    pass


def validate_url(url: str) -> str:
    """Accept only public http(s) URLs, so the server can't be pointed at itself or the local network."""
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise DownloadError("Enter a full link starting with http:// or https://.")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise DownloadError(f"Couldn't find the website '{parsed.hostname}'. Check the link.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise DownloadError("Links to this computer or the local network aren't allowed.")
    return url


def _js_runtimes() -> dict:
    # YouTube needs a JavaScript runtime to unlock its formats; use whichever is installed.
    return {name: {} for name in ("deno", "node", "bun") if shutil.which(name)}


def _clean_error(exc: Exception) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))
    msg = re.sub(r"^ERROR:\s*", "", msg).strip()
    if "Unsupported URL" in msg:
        return "This link doesn't point to a video or audio file that can be imported."
    if "Private video" in msg or "Sign in" in msg:
        return "This video is private or needs a login, so it can't be imported."
    if "larger than max-filesize" in msg or "File is larger" in msg:
        return f"The video is larger than the {settings.max_upload_mb} MB limit."
    return msg[:500] or "The download failed."


def download(
    url: str,
    dest_dir: Path,
    stem: str,
    on_progress: Callable[[float], None] | None = None,
    check_stop: Callable[[], None] | None = None,
) -> dict:
    """Download the media at `url` to dest_dir/stem.<ext>. Returns {path, title, media_type, duration}."""
    url = validate_url(url)
    max_seconds = int(settings.max_download_hours * 3600)

    def too_long(info, *, incomplete):
        duration = info.get("duration")
        if duration and duration > max_seconds:
            return f"The video is longer than {settings.max_download_hours:g} hours."
        return None

    def hook(d):
        if check_stop:
            try:
                check_stop()
            except jobs.Cancelled:
                raise yt_dlp.utils.DownloadCancelled("Stopped by the teacher") from None
        if on_progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                on_progress(min(d.get("downloaded_bytes", 0) / total, 1.0))

    opts = {
        "outtmpl": str(dest_dir / f"{stem}.%(ext)s"),
        "format": FORMAT,
        "merge_output_format": "mp4",
        "ffmpeg_location": media.ffmpeg_binary(),
        "noplaylist": True,
        "max_filesize": settings.max_upload_mb * 1024 * 1024,
        "match_filter": too_long,
        "progress_hooks": [hook],
        "js_runtimes": _js_runtimes(),
        "socket_timeout": 30,
        "retries": 3,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "color": {"stderr": "never", "stdout": "never"},
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadCancelled as exc:
        raise jobs.Cancelled() from exc  # partial files are kept, so continuing resumes the download
    except yt_dlp.utils.DownloadError as exc:
        if check_stop:
            check_stop()  # a stop during a retry surfaces as a DownloadError
        raise DownloadError(_clean_error(exc)) from exc

    if info is None:  # rejected by match_filter
        raise DownloadError(f"The video is longer than {settings.max_download_hours:g} hours.")
    if info.get("_type") == "playlist":
        raise DownloadError("That's a playlist. Paste the link of a single video.")

    downloads = info.get("requested_downloads") or []
    path = Path(downloads[0]["filepath"]) if downloads else None
    if path is None or not path.exists():
        matches = sorted(dest_dir.glob(f"{stem}.*"))
        if not matches:
            raise DownloadError("The download finished but no file was saved.")
        path = matches[0]

    media_type = media.detect_media_type(path.name)
    if media_type is None:
        path.unlink(missing_ok=True)
        raise DownloadError(f"The link gave a '{path.suffix}' file, which isn't a supported video or audio format.")

    title = (info.get("title") or "").strip()
    if not title or title == info.get("id") or title.startswith("videoplayback"):
        title = Path(urlparse(url).path).stem.replace("_", " ").replace("-", " ").strip() or "Imported lecture"
    return {"path": path, "title": title[:200], "media_type": media_type, "duration": info.get("duration")}
