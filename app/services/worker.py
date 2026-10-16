"""In-process background worker: upload -> ffmpeg audio extraction -> Whisper -> transcript in DB.

A single worker thread processes lectures one at a time, since Whisper already uses all available
cores/GPU. Swap this for Celery/RQ/Arq when you need multiple machines or durable queues.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import Lecture, LectureStatus, Transcript
from app.services import media, transcription

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="transcriber")
PROGRESS_COMMIT_INTERVAL = 3.0  # seconds


def enqueue(lecture_id: int) -> None:
    _executor.submit(_run_safely, lecture_id)


def resume_pending() -> None:
    """Re-queue lectures that were mid-processing when the server stopped."""
    with SessionLocal() as db:
        ids = db.scalars(
            select(Lecture.id)
            .where(Lecture.status.in_([s for s in LectureStatus if s.is_processing]))
            .order_by(Lecture.created_at)
        ).all()
    for lecture_id in ids:
        log.info("Resuming processing of lecture %s", lecture_id)
        enqueue(lecture_id)


def shutdown() -> None:
    _executor.shutdown(wait=False, cancel_futures=True)


def _run_safely(lecture_id: int) -> None:
    try:
        process_lecture(lecture_id)
    except Exception:  # noqa: BLE001 - never let the worker thread die silently
        log.exception("Unexpected error while processing lecture %s", lecture_id)


def process_lecture(lecture_id: int) -> None:
    with SessionLocal() as db:
        lecture = db.get(Lecture, lecture_id)
        if lecture is None:  # deleted while queued
            return

        def set_status(status: LectureStatus, progress: float = 0.0) -> None:
            lecture.status = status
            lecture.progress = progress
            lecture.error = None
            db.commit()

        try:
            # 1. FFmpeg: extract a Whisper-friendly WAV from the uploaded video/audio
            set_status(LectureStatus.EXTRACTING_AUDIO)
            audio_path = settings.audio_dir / f"lecture_{lecture.id}.wav"
            media.extract_audio(Path(lecture.media_path), audio_path)
            lecture.audio_path = str(audio_path)

            # 2. Whisper: timestamped transcript
            set_status(LectureStatus.TRANSCRIBING)
            last_commit = 0.0

            def on_progress(p: float) -> None:
                nonlocal last_commit
                lecture.progress = p
                if time.monotonic() - last_commit >= PROGRESS_COMMIT_INTERVAL:
                    db.commit()
                    last_commit = time.monotonic()

            started = time.monotonic()
            result = transcription.transcribe(audio_path, on_progress=on_progress)
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
            lecture.transcript = transcript
            lecture.duration = result["duration"]
            set_status(LectureStatus.COMPLETED, 1.0)

        except Exception as exc:  # noqa: BLE001
            log.exception("Processing failed for lecture %s", lecture_id)
            db.rollback()
            lecture = db.get(Lecture, lecture_id)
            if lecture is not None:
                lecture.status = LectureStatus.FAILED
                lecture.error = str(exc)[:2000] or exc.__class__.__name__
                db.commit()
