"""Stop requests for background jobs.

A teacher pressing "Stop" adds the lecture to a set here; the running job checks it between steps
(and inside long steps: download progress, ffmpeg polling, every Whisper segment, every chunk the
local model streams) and raises Cancelled, which the worker turns into status "cancelled".
"""

import threading

TRANSCRIPTION = "transcription"
NOTES = "notes"

_lock = threading.Lock()
_requests: set[tuple[str, int]] = set()
_running: set[tuple[str, int]] = set()


def mark_running(kind: str, lecture_id: int, running: bool) -> None:
    with _lock:
        (_running.add if running else _running.discard)((kind, lecture_id))


def is_running(kind: str, lecture_id: int) -> bool:
    with _lock:
        return (kind, lecture_id) in _running


class Cancelled(Exception):
    pass


def request_stop(kind: str, lecture_id: int) -> None:
    with _lock:
        _requests.add((kind, lecture_id))


def stop_requested(kind: str, lecture_id: int) -> bool:
    with _lock:
        return (kind, lecture_id) in _requests


def clear(kind: str, lecture_id: int) -> None:
    with _lock:
        _requests.discard((kind, lecture_id))


def check(kind: str, lecture_id: int) -> None:
    if stop_requested(kind, lecture_id):
        raise Cancelled()


def checker(kind: str, lecture_id: int):
    """A no-argument callable that raises Cancelled once a stop was requested."""
    return lambda: check(kind, lecture_id)
