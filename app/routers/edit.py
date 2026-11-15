"""Teacher editing: lecture details, thumbnails, transcript corrections and the notes/quiz editor."""

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import LectureStatus, MediaType, Notes, NotesStatus, User
from app.routers.lectures import get_owned_lecture, get_viewable_lecture
from app.security import require_teacher, require_user
from app.services import media, worker
from app.templating import flash, templates

router = APIRouter()

MAX_THUMBNAIL_BYTES = 8 * 1024 * 1024


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


def _thumb_url(lecture) -> str | None:
    if not lecture.thumbnail_path or not Path(lecture.thumbnail_path).is_file():
        return None
    return f"/lectures/{lecture.id}/thumbnail?v={int(Path(lecture.thumbnail_path).stat().st_mtime)}"


# ---------------------------------------------------------------- details

@router.post("/lectures/{lecture_id}/details")
def update_details(
    lecture_id: int, request: Request, title: str = Form(...), description: str = Form(""),
    teacher: User = Depends(require_teacher), db: Session = Depends(get_db),
):
    lecture = get_owned_lecture(lecture_id, teacher, db)
    title = title.strip()
    if not title:
        flash(request, "The title can't be empty.", "error")
    else:
        lecture.title = title[:200]
        lecture.description = description.strip()
        db.commit()
        flash(request, "Lecture details saved.", "success")
    return _redirect(f"/lectures/{lecture.id}")


@router.post("/lectures/{lecture_id}/redownload")
def redownload(lecture_id: int, request: Request, teacher: User = Depends(require_teacher), db: Session = Depends(get_db)):
    """Fetch the linked video again (e.g. to replace a copy the browser can't play). Transcript and notes are kept."""
    lecture = get_owned_lecture(lecture_id, teacher, db)
    if not lecture.source_url or lecture.status.is_processing:
        flash(request, "This lecture can't be re-downloaded right now.", "error")
        return _redirect(f"/lectures/{lecture.id}")
    if lecture.media_path:
        Path(lecture.media_path).unlink(missing_ok=True)
    for partial in settings.uploads_dir.glob(f"lecture_{lecture.id}_src.*"):
        partial.unlink(missing_ok=True)
    lecture.media_path = ""
    if not lecture.thumbnail_custom:
        lecture.thumbnail_path = None
    lecture.status, lecture.progress, lecture.error = LectureStatus.DOWNLOADING, 0.0, None
    db.commit()
    worker.enqueue(lecture.id)
    flash(request, "Downloading the video again.", "success")
    return _redirect(f"/lectures/{lecture.id}")


# ---------------------------------------------------------------- thumbnails

@router.get("/lectures/{lecture_id}/thumbnail")
def get_thumbnail(lecture_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    lecture = get_viewable_lecture(lecture_id, user, db)
    if not lecture.thumbnail_path or not Path(lecture.thumbnail_path).is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No thumbnail")
    return FileResponse(lecture.thumbnail_path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


def _thumb_dest(lecture) -> Path:
    return settings.thumbnails_dir / f"lecture_{lecture.id}.jpg"


@router.post("/api/lectures/{lecture_id}/thumbnail")
async def upload_thumbnail(
    lecture_id: int, file: UploadFile = File(...),
    teacher: User = Depends(require_teacher), db: Session = Depends(get_db),
):
    lecture = get_owned_lecture(lecture_id, teacher, db)
    data = await file.read(MAX_THUMBNAIL_BYTES + 1)
    if len(data) > MAX_THUMBNAIL_BYTES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The image is larger than 8 MB.")
    try:
        media.save_thumbnail_image(data, _thumb_dest(lecture))
    except media.MediaError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    lecture.thumbnail_path, lecture.thumbnail_custom = str(_thumb_dest(lecture)), True
    db.commit()
    return {"url": _thumb_url(lecture)}


class FrameIn(BaseModel):
    at: float = Field(ge=0)


@router.post("/api/lectures/{lecture_id}/thumbnail/frame")
def thumbnail_from_frame(lecture_id: int, body: FrameIn, teacher: User = Depends(require_teacher), db: Session = Depends(get_db)):
    lecture = get_owned_lecture(lecture_id, teacher, db)
    if lecture.media_type != MediaType.VIDEO or not lecture.media_path or not Path(lecture.media_path).is_file():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This lecture has no video to take a frame from.")
    try:
        media.extract_frame(Path(lecture.media_path), _thumb_dest(lecture), body.at)
    except media.MediaError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    lecture.thumbnail_path, lecture.thumbnail_custom = str(_thumb_dest(lecture)), True
    db.commit()
    return {"url": _thumb_url(lecture)}


@router.post("/api/lectures/{lecture_id}/thumbnail/auto")
def thumbnail_auto(lecture_id: int, teacher: User = Depends(require_teacher), db: Session = Depends(get_db)):
    lecture = get_owned_lecture(lecture_id, teacher, db)
    lecture.thumbnail_custom = False
    worker.auto_thumbnail(lecture)
    db.commit()
    return {"url": _thumb_url(lecture)}


# ---------------------------------------------------------------- transcript corrections

class SegmentEdit(BaseModel):
    index: int = Field(ge=0)
    text: str = Field(max_length=5000)


class TranscriptEdit(BaseModel):
    segments: list[SegmentEdit]


@router.put("/api/lectures/{lecture_id}/transcript")
def edit_transcript(lecture_id: int, body: TranscriptEdit, teacher: User = Depends(require_teacher), db: Session = Depends(get_db)):
    lecture = get_owned_lecture(lecture_id, teacher, db)
    t = lecture.transcript
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No transcript yet")
    segments = [dict(s) for s in t.segments]
    changed = 0
    for e in body.segments:
        if e.index >= len(segments):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"No line {e.index}")
        text = " ".join(e.text.split())
        if text != segments[e.index]["text"]:
            segments[e.index]["text"] = text
            segments[e.index].pop("words", None)  # word timings no longer match edited text
            changed += 1
    if changed:
        t.segments = segments  # reassign so SQLAlchemy saves the JSON
        t.text = " ".join(s["text"] for s in segments if s["text"])
        t.edited = True
        db.commit()
    return {"changed": changed}


# ---------------------------------------------------------------- notes / quiz editor

class KeyPointIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    start: float = 0


class SectionIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    start: float = 0
    summary: str = Field(default="", max_length=5000)
    key_points: list[KeyPointIn] = []


class DefinitionIn(BaseModel):
    term: str = Field(min_length=1, max_length=300)
    definition: str = Field(min_length=1, max_length=3000)
    start: float = 0


class MCQIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    options: list[str] = Field(min_length=2, max_length=6)
    answer_index: int = Field(ge=0)
    explanation: str = Field(default="", max_length=3000)
    start: float = 0

    @field_validator("options")
    @classmethod
    def no_blank_options(cls, v):
        v = [o.strip() for o in v]
        if any(not o for o in v):
            raise ValueError("Answer options can't be empty.")
        return v

    @model_validator(mode="after")
    def answer_in_range(self):
        if self.answer_index >= len(self.options):
            raise ValueError("Pick which option is correct.")
        return self


class NotesIn(BaseModel):
    summary: str = Field(default="", max_length=10000)
    sections: list[SectionIn] = []
    definitions: list[DefinitionIn] = []
    mcqs: list[MCQIn] = []


@router.get("/lectures/{lecture_id}/notes/edit")
def notes_editor(lecture_id: int, request: Request, teacher: User = Depends(require_teacher), db: Session = Depends(get_db)):
    lecture = get_owned_lecture(lecture_id, teacher, db)
    if lecture.transcript is None:
        flash(request, "Notes can be edited once the transcript is ready.", "error")
        return _redirect(f"/lectures/{lecture.id}")
    if lecture.notes and lecture.notes.status.is_processing:
        flash(request, "Wait for the notes to finish generating (or stop it) before editing.", "error")
        return _redirect(f"/lectures/{lecture.id}")
    content = (lecture.notes.content if lecture.notes else None) or {"summary": "", "sections": [], "definitions": [], "mcqs": []}
    return templates.TemplateResponse(request, "notes_edit.html", {
        "user": teacher, "lecture": lecture, "content": content,
        "attempts": len(lecture.attempts),
    })


@router.put("/api/lectures/{lecture_id}/notes")
def save_notes(lecture_id: int, body: NotesIn, teacher: User = Depends(require_teacher), db: Session = Depends(get_db)):
    lecture = get_owned_lecture(lecture_id, teacher, db)
    if lecture.transcript is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "No transcript yet")
    if lecture.notes and lecture.notes.status.is_processing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Notes are being generated right now")
    content = body.model_dump()
    content["sections"].sort(key=lambda s: s["start"])
    row = lecture.notes or Notes(lecture_id=lecture.id, model="written by the teacher")
    row.content, row.status, row.error, row.edited, row.progress = content, NotesStatus.COMPLETED, None, True, 1.0
    lecture.notes = row
    db.commit()
    return {"ok": True}
