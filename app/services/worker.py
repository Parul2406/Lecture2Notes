"""In-process background workers.

Transcription: (link download ->) ffmpeg audio extraction -> Whisper -> transcript in DB. A single
thread processes lectures one at a time, since Whisper already uses all available cores/GPU.

Notes: transcript -> local LLM (Ollama) -> structured study notes in DB. Runs on its own thread, so a
new upload's transcription doesn't wait for notes (and vice versa).

Jobs run on the server, independent of any browser tab. Teachers can stop them (see jobs.py).
Swap this for Celery/RQ/Arq when you need multiple machines or durable queues.
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import Lecture, LectureStatus, MediaType, Notes, NotesStatus, Transcript
from app.services import download, jobs, media, notes, transcription
from app.services.jobs import NOTES, REDOWNLOAD, TRANSCRIPTION

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="transcriber")
# One at a time: the local model already uses the whole GPU/CPU.
_notes_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notes")
# Re-downloads are network-bound and don't touch the lecture's stages, so they get their own thread.
_media_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="redownload")
PROGRESS_COMMIT_INTERVAL = 2.0  # seconds
PENDING_TITLE = "Importing from link…"  # replaced by the video's own title once downloaded
STOPPED_MESSAGE = "Stopped by the teacher."


def enqueue(lecture_id: int) -> None:
    jobs.clear(TRANSCRIPTION, lecture_id)
    _executor.submit(_run_safely, lecture_id)


def resume_pending() -> None:
    """Re-queue lectures that were mid-processing when the server stopped."""
    with SessionLocal() as db:
        ids = db.scalars(
            select(Lecture.id)
            .where(Lecture.status.in_([s for s in LectureStatus if s.is_processing]))
            .order_by(Lecture.created_at)
        ).all()
        note_ids = db.scalars(
            select(Notes.lecture_id).where(Notes.status.in_([s for s in NotesStatus if s.is_processing]))
        ).all()
        redownload_ids = db.scalars(select(Lecture.id).where(Lecture.redownload_progress.is_not(None))).all()
        _cleanup_orphan_media(db)
    for lecture_id in ids:
        log.info("Resuming processing of lecture %s", lecture_id)
        enqueue(lecture_id)
    for lecture_id in note_ids:
        if lecture_id not in ids:  # those get notes queued after transcription
            log.info("Resuming notes generation for lecture %s", lecture_id)
            _notes_executor.submit(_run_notes_safely, lecture_id)
    for lecture_id in redownload_ids:
        log.info("Resuming re-download for lecture %s", lecture_id)
        _media_executor.submit(_run_redownload_safely, lecture_id)


def _cleanup_orphan_media(db) -> None:
    """Remove downloaded videos nothing uses any more, e.g. an old copy that a browser was still streaming
    when a re-download replaced it, or files of deleted lectures.

    Rules: a lecture's current video is never touched; partial downloads (.part/.ytdl/.fNNN pieces) of
    existing lectures are kept so stopped downloads can resume; complete files are removed only when the
    lecture already has a different current video.
    """
    lectures = {l.id: l for l in db.scalars(select(Lecture)).all()}
    for f in settings.uploads_dir.glob("lecture_*"):
        m = re.match(r"lecture_(\d+)_", f.name)
        if not m:
            continue
        lecture = lectures.get(int(m.group(1)))
        current = lecture and lecture.media_path and Path(lecture.media_path).resolve() == f.resolve()
        partial = bool(re.search(r"\.(part|ytdl|temp)$|\.f\d+\.", f.name))
        if current or (lecture and (partial or not lecture.media_path)):
            continue
        try:
            f.unlink()
            log.info("Removed unused media file %s", f.name)
        except OSError:
            pass


def shutdown() -> None:
    _executor.shutdown(wait=False, cancel_futures=True)
    _notes_executor.shutdown(wait=False, cancel_futures=True)
    _media_executor.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------- stopping

def stop_lecture(lecture_id: int) -> bool:
    """Stop downloading/extracting/transcribing. Returns False if there was nothing to stop.

    A queued job is marked cancelled at once; a running one is stopped at its next check
    (usually within a second or two) and then marks itself cancelled.
    """
    with SessionLocal() as db:
        lecture = db.get(Lecture, lecture_id)
        if lecture is None or not lecture.status.is_processing:
            return False
        jobs.request_stop(TRANSCRIPTION, lecture_id)
        if not jobs.is_running(TRANSCRIPTION, lecture_id):
            _mark_lecture_cancelled(lecture)
            db.commit()
    return True


def stop_notes(lecture_id: int) -> bool:
    with SessionLocal() as db:
        lecture = db.get(Lecture, lecture_id)
        if lecture is None or lecture.notes is None or not lecture.notes.status.is_processing:
            return False
        jobs.request_stop(NOTES, lecture_id)
        if not jobs.is_running(NOTES, lecture_id):
            _mark_notes_cancelled(lecture.notes)
            db.commit()
    return True


def stopping(lecture_id: int) -> dict:
    """Which jobs of this lecture have a stop request that hasn't taken effect yet."""
    return {
        "transcription": jobs.stop_requested(TRANSCRIPTION, lecture_id),
        "notes": jobs.stop_requested(NOTES, lecture_id),
        "redownload": jobs.stop_requested(REDOWNLOAD, lecture_id),
    }


# ---------------------------------------------------------------- re-download (replace the video safely)

def enqueue_redownload(lecture_id: int) -> bool:
    """Fetch the linked video again into a separate file. The lecture keeps its current video, stages,
    transcript and notes; the new file replaces the old one only once it's complete."""
    with SessionLocal() as db:
        lecture = db.get(Lecture, lecture_id)
        if lecture is None or not lecture.source_url or lecture.redownload_progress is not None:
            return False
        lecture.redownload_progress, lecture.redownload_error = 0.0, None
        db.commit()
    jobs.clear(REDOWNLOAD, lecture_id)
    _media_executor.submit(_run_redownload_safely, lecture_id)
    return True


def stop_redownload(lecture_id: int) -> bool:
    with SessionLocal() as db:
        lecture = db.get(Lecture, lecture_id)
        if lecture is None or lecture.redownload_progress is None:
            return False
        jobs.request_stop(REDOWNLOAD, lecture_id)
        if not jobs.is_running(REDOWNLOAD, lecture_id):
            lecture.redownload_progress, lecture.redownload_error = None, None
            db.commit()
    return True


def _run_redownload_safely(lecture_id: int) -> None:
    jobs.mark_running(REDOWNLOAD, lecture_id, True)
    try:
        redownload_media(lecture_id)
    except Exception:  # noqa: BLE001
        log.exception("Unexpected error while re-downloading lecture %s", lecture_id)
    finally:
        jobs.mark_running(REDOWNLOAD, lecture_id, False)
        jobs.clear(REDOWNLOAD, lecture_id)


def _remove_file(path: Path) -> None:
    for _ in range(5):  # Windows refuses to delete a file a browser is still streaming; retry briefly
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(1)
    log.info("Old video %s is still in use; it will be removed on the next start", path.name)


def redownload_media(lecture_id: int) -> None:
    with SessionLocal() as db:
        lecture = db.get(Lecture, lecture_id)
        if lecture is None or lecture.redownload_progress is None:
            return  # deleted, or stopped while queued
        last_commit = 0.0

        def on_progress(p: float) -> None:
            nonlocal last_commit
            lecture.redownload_progress = p
            if time.monotonic() - last_commit >= PROGRESS_COMMIT_INTERVAL:
                db.commit()
                last_commit = time.monotonic()

        try:
            result = download.download(
                lecture.source_url, settings.uploads_dir, f"lecture_{lecture.id}_dl",
                on_progress=on_progress, check_stop=jobs.checker(REDOWNLOAD, lecture_id),
            )
        except jobs.Cancelled:
            log.info("Re-download of lecture %s stopped; the current video is unchanged", lecture_id)
            lecture.redownload_progress, lecture.redownload_error = None, None
            db.commit()
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("Re-download of lecture %s failed: %s", lecture_id, exc)
            lecture.redownload_progress = None
            lecture.redownload_error = (str(exc) or exc.__class__.__name__)[:2000]
            db.commit()
            return

        # Complete: move the new file into place, then swap it in and only now remove the old one.
        new_path = settings.uploads_dir / f"lecture_{lecture.id}_v{int(time.time())}{result['path'].suffix}"
        result["path"].replace(new_path)
        old_path = Path(lecture.media_path) if lecture.media_path else None
        lecture.media_path = str(new_path)
        lecture.media_type = result["media_type"]
        lecture.original_filename = new_path.name
        if result["duration"] and not lecture.duration:
            lecture.duration = float(result["duration"])
        lecture.redownload_progress, lecture.redownload_error = None, None
        if not lecture.thumbnail_custom:
            auto_thumbnail(lecture)
        db.commit()
        log.info("Lecture %s: new copy of the video is in place", lecture_id)
        if old_path and old_path.resolve() != new_path.resolve():
            _remove_file(old_path)


def _mark_lecture_cancelled(lecture: Lecture) -> None:
    lecture.status = LectureStatus.CANCELLED
    lecture.error = STOPPED_MESSAGE
    if lecture.title == PENDING_TITLE:
        lecture.title = "Link import (stopped)"


def _mark_notes_cancelled(row: Notes) -> None:
    # Keep the previous notes if this was a regeneration.
    row.status = NotesStatus.COMPLETED if row.content else NotesStatus.CANCELLED
    row.error = None if row.content else STOPPED_MESSAGE
    row.progress = 0.0


# ---------------------------------------------------------------- notes

def enqueue_notes(lecture_id: int) -> None:
    """Mark the lecture's notes as pending and queue generation (creates the Notes row if needed)."""
    jobs.clear(NOTES, lecture_id)
    with SessionLocal() as db:
        lecture = db.get(Lecture, lecture_id)
        if lecture is None:
            return
        lecture.notes = lecture.notes or Notes(lecture_id=lecture.id)
        lecture.notes.status = NotesStatus.PENDING
        lecture.notes.progress = 0.0
        lecture.notes.error = None
        db.commit()
    _notes_executor.submit(_run_notes_safely, lecture_id)


def _run_notes_safely(lecture_id: int) -> None:
    jobs.mark_running(NOTES, lecture_id, True)
    try:
        generate_notes(lecture_id)
    except Exception:  # noqa: BLE001
        log.exception("Unexpected error while generating notes for lecture %s", lecture_id)
    finally:
        jobs.mark_running(NOTES, lecture_id, False)
        jobs.clear(NOTES, lecture_id)


def generate_notes(lecture_id: int) -> None:
    with SessionLocal() as db:
        lecture = db.get(Lecture, lecture_id)
        if lecture is None or lecture.notes is None or not lecture.notes.status.is_processing:
            return  # deleted, or stopped while queued
        row = lecture.notes
        if lecture.transcript is None:
            row.status, row.error = NotesStatus.FAILED, "The lecture has no transcript yet."
            db.commit()
            return
        if jobs.stop_requested(NOTES, lecture_id):
            _mark_notes_cancelled(row)
            db.commit()
            return

        row.status, row.error, row.progress = NotesStatus.GENERATING, None, 0.0
        row.model = settings.notes_model
        db.commit()

        def on_progress(p: float) -> None:
            row.progress = p
            db.commit()

        try:
            started = time.monotonic()
            content = notes.generate_notes(
                lecture.title, lecture.description, lecture.transcript.segments,
                language=lecture.transcript.language, on_progress=on_progress,
                check_stop=jobs.checker(NOTES, lecture_id),
            )
            log.info("Lecture %s notes ready in %.0fs", lecture_id, time.monotonic() - started)
            row.content, row.status, row.edited = content, NotesStatus.COMPLETED, False
        except jobs.Cancelled:
            log.info("Notes generation for lecture %s stopped by the teacher", lecture_id)
            _mark_notes_cancelled(row)
        except Exception as exc:  # noqa: BLE001
            log.warning("Notes generation failed for lecture %s: %s", lecture_id, exc)
            row.status = NotesStatus.FAILED
            row.error = (str(exc) if isinstance(exc, notes.NotesError) else f"Unexpected error: {exc}")[:2000]
        db.commit()


# ---------------------------------------------------------------- thumbnails

def auto_thumbnail(lecture: Lecture) -> None:
    """Grab a frame (10% into the video, at most 60s in) unless the teacher picked a thumbnail."""
    if lecture.thumbnail_custom or lecture.media_type != MediaType.VIDEO or not lecture.media_path:
        return
    at = min((lecture.duration or 50) * 0.1, 60)
    dest = settings.thumbnails_dir / f"lecture_{lecture.id}.jpg"
    try:
        media.extract_frame(Path(lecture.media_path), dest, at)
        lecture.thumbnail_path = str(dest)
    except Exception as exc:  # noqa: BLE001 - a missing thumbnail must never fail processing
        log.warning("Could not create a thumbnail for lecture %s: %s", lecture.id, exc)


# ---------------------------------------------------------------- transcription

def _run_safely(lecture_id: int) -> None:
    jobs.mark_running(TRANSCRIPTION, lecture_id, True)
    try:
        process_lecture(lecture_id)
    except Exception:  # noqa: BLE001 - never let the worker thread die silently
        log.exception("Unexpected error while processing lecture %s", lecture_id)
    finally:
        jobs.mark_running(TRANSCRIPTION, lecture_id, False)
        jobs.clear(TRANSCRIPTION, lecture_id)


def process_lecture(lecture_id: int) -> None:
    check_stop = jobs.checker(TRANSCRIPTION, lecture_id)
    with SessionLocal() as db:
        lecture = db.get(Lecture, lecture_id)
        if lecture is None or not lecture.status.is_processing:
            return  # deleted, or stopped while queued

        last_commit = 0.0

        def set_status(status: LectureStatus, progress: float = 0.0) -> None:
            check_stop()
            lecture.status = status
            lecture.progress = progress
            lecture.error = None
            db.commit()

        def on_progress(p: float) -> None:
            nonlocal last_commit
            check_stop()
            lecture.progress = p
            if time.monotonic() - last_commit >= PROGRESS_COMMIT_INTERVAL:
                db.commit()
                last_commit = time.monotonic()

        audio_path = settings.audio_dir / f"lecture_{lecture.id}.wav"
        try:
            # 0. Link import: download the video/audio first
            if lecture.source_url and not lecture.media_path:
                set_status(LectureStatus.DOWNLOADING)
                result = download.download(
                    lecture.source_url, settings.uploads_dir, f"lecture_{lecture.id}_src",
                    on_progress=on_progress, check_stop=check_stop,
                )
                lecture.media_path = str(result["path"])
                lecture.media_type = result["media_type"]
                lecture.original_filename = result["path"].name
                if lecture.title == PENDING_TITLE:
                    lecture.title = result["title"]
                if result["duration"]:
                    lecture.duration = float(result["duration"])
                db.commit()
                log.info("Lecture %s downloaded from %s", lecture.id, lecture.source_url)

            if not lecture.thumbnail_path:
                auto_thumbnail(lecture)
                db.commit()

            # 1. FFmpeg: extract a Whisper-friendly WAV (skipped when continuing and it's already there)
            if lecture.audio_path and Path(lecture.audio_path).is_file():
                log.info("Lecture %s: audio already extracted, continuing with transcription", lecture.id)
            else:
                set_status(LectureStatus.EXTRACTING_AUDIO)
                media.extract_audio(Path(lecture.media_path), audio_path, check_stop=check_stop)
                lecture.audio_path = str(audio_path)

            # 2. Whisper: timestamped transcript
            set_status(LectureStatus.TRANSCRIBING)
            started = time.monotonic()
            # In a child process, so Stop takes effect immediately even mid-segment.
            result = transcription.transcribe_killable(audio_path, on_progress=on_progress, check_stop=check_stop)
            check_stop()
            log.info(
                "Lecture %s transcribed: %d segments, %.0fs audio in %.0fs",
                lecture.id, len(result["segments"]), result["duration"], time.monotonic() - started,
            )

            # 3. Store
            transcript = lecture.transcript or Transcript(lecture_id=lecture.id)
            transcript.model = result["model"]
            transcript.language = result["language"]
            transcript.language_probability = result["language_probability"]
            transcript.duration = result["duration"]
            transcript.text = result["text"]
            transcript.segments = result["segments"]
            transcript.edited = False
            lecture.transcript = transcript
            lecture.duration = result["duration"]
            set_status(LectureStatus.COMPLETED, 1.0)
            if settings.notes_enabled and settings.notes_auto and result["segments"]:
                enqueue_notes(lecture.id)

        except jobs.Cancelled:
            log.info("Processing of lecture %s stopped by the teacher", lecture_id)
            db.rollback()
            lecture = db.get(Lecture, lecture_id)
            if lecture is not None:
                _mark_lecture_cancelled(lecture)
                db.commit()
                # Keep finished work for "Continue"; only remove an audio file that wasn't completed.
                if not lecture.audio_path:
                    audio_path.unlink(missing_ok=True)

        except Exception as exc:  # noqa: BLE001
            log.exception("Processing failed for lecture %s", lecture_id)
            db.rollback()
            lecture = db.get(Lecture, lecture_id)
            if lecture is not None:
                lecture.status = LectureStatus.FAILED
                lecture.error = str(exc)[:2000] or exc.__class__.__name__
                if lecture.title == PENDING_TITLE:
                    lecture.title = "Link import failed"
                db.commit()
