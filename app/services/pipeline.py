"""Where a lecture is in its pipeline, and where "Continue" picks up after a stop or failure.

Stages: [Downloading] -> Extracting audio -> Transcribing -> Writing notes.
Finished work is kept (downloaded file, extracted audio), so continuing skips straight to the stage
that didn't finish instead of starting over.
"""

from pathlib import Path

from app.models import Lecture, LectureStatus, NotesStatus

LABELS = {
    "downloading": "Downloading",
    "extracting_audio": "Extracting audio",
    "transcribing": "Transcribing",
    "notes": "Writing notes",
}


def _exists(path: str | None) -> bool:
    return bool(path) and Path(path).is_file()


def resume_stage(lecture: Lecture) -> str:
    """First transcription stage whose result is missing."""
    if lecture.source_url and not _exists(lecture.media_path):
        return "downloading"
    if not _exists(lecture.audio_path):
        return "extracting_audio"
    return "transcribing"


def stages(lecture: Lecture) -> list[dict]:
    """[{key, label, state}] with state one of done | active | queued | stopped | failed | pending."""
    keys = (["downloading"] if lecture.source_url else []) + ["extracting_audio", "transcribing", "notes"]
    status = lecture.status
    transcribed = status == LectureStatus.COMPLETED and lecture.transcript is not None
    done = {
        "downloading": _exists(lecture.media_path),
        "extracting_audio": transcribed or _exists(lecture.audio_path),
        "transcribing": transcribed,
        "notes": bool(lecture.notes and lecture.notes.content and lecture.notes.status == NotesStatus.COMPLETED),
    }
    current = {
        LectureStatus.DOWNLOADING: ("downloading", "active"),
        LectureStatus.UPLOADED: ("extracting_audio", "queued"),
        LectureStatus.EXTRACTING_AUDIO: ("extracting_audio", "active"),
        LectureStatus.TRANSCRIBING: ("transcribing", "active"),
        LectureStatus.CANCELLED: (resume_stage(lecture), "stopped"),
        LectureStatus.FAILED: (resume_stage(lecture), "failed"),
    }.get(status)
    notes = lecture.notes
    if transcribed and notes is not None:
        current = {
            NotesStatus.PENDING: ("notes", "queued"),
            NotesStatus.GENERATING: ("notes", "active"),
            NotesStatus.CANCELLED: ("notes", "stopped"),
            NotesStatus.FAILED: ("notes", "failed"),
        }.get(notes.status, current if current and current[0] != "notes" else None)

    out = []
    for key in keys:
        if current and key == current[0]:
            state = current[1]
        elif done[key]:
            state = "done"
        else:
            state = "pending"
        out.append({"key": key, "label": LABELS[key], "state": state})
    return out


def continue_label(lecture: Lecture) -> str | None:
    """Text for the Continue button after a stop/failure of transcription ("Continue – Transcribing")."""
    if lecture.status not in (LectureStatus.CANCELLED, LectureStatus.FAILED):
        return None
    verb = "Continue" if lecture.status == LectureStatus.CANCELLED else "Retry"
    return f"{verb} – {LABELS[resume_stage(lecture)]}"
