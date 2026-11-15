import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import get_db
from app.models import Lecture, LectureStatus, MediaType, User
from app.security import get_current_user, require_teacher, require_user
from app.services import download, export, media, pipeline, worker
from app.templating import flash, templates

router = APIRouter()


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


def get_viewable_lecture(lecture_id: int, user: User, db: Session) -> Lecture:
    """Teachers see their own lectures; students see every lecture whose video is available
    (even while it's still being transcribed - notes and transcript appear when ready)."""
    lecture = db.get(Lecture, lecture_id)
    if lecture is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lecture not found")
    if user.is_teacher:
        if lecture.teacher_id != user.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Lecture not found")
    elif not lecture.media_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lecture not found")
    return lecture


def get_owned_lecture(lecture_id: int, teacher: User, db: Session) -> Lecture:
    lecture = db.get(Lecture, lecture_id)
    if lecture is None or lecture.teacher_id != teacher.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lecture not found")
    return lecture


@router.get("/")
def home(request: Request, user: User | None = Depends(get_current_user)):
    if user:
        return _redirect("/dashboard")
    return templates.TemplateResponse(request, "index.html", {"user": None})


@router.get("/dashboard")
def dashboard(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    query = select(Lecture).options(selectinload(Lecture.teacher), selectinload(Lecture.notes)).order_by(Lecture.created_at.desc())
    if user.is_teacher:
        query = query.where(Lecture.teacher_id == user.id)
    else:
        query = query.where(Lecture.media_path != "")  # watchable, even if still being transcribed
    lectures = db.scalars(query).all()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "lectures": lectures,
            "accept": ",".join(sorted(media.VIDEO_EXTENSIONS | media.AUDIO_EXTENSIONS)),
        },
    )


@router.post("/lectures")
def upload_lecture(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    file: UploadFile = File(...),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    title = title.strip()
    if not title:
        flash(request, "Title is required.", "error")
        return _redirect("/dashboard")

    original_name = Path(file.filename or "").name
    media_type = media.detect_media_type(original_name)
    if media_type is None:
        flash(request, "Unsupported file type. Upload a video or audio file.", "error")
        return _redirect("/dashboard")

    dest = settings.uploads_dir / f"{uuid.uuid4().hex}{Path(original_name).suffix.lower()}"
    try:
        size = media.save_upload(file.file, dest, settings.max_upload_mb * 1024 * 1024)
    except media.UploadTooLarge as exc:
        flash(request, str(exc), "error")
        return _redirect("/dashboard")
    if size == 0:
        dest.unlink(missing_ok=True)
        flash(request, "The uploaded file is empty.", "error")
        return _redirect("/dashboard")

    lecture = Lecture(
        teacher_id=teacher.id,
        title=title[:200],
        description=description.strip(),
        media_type=media_type,
        original_filename=original_name[:255],
        media_path=str(dest),
        status=LectureStatus.UPLOADED,
    )
    db.add(lecture)
    db.commit()
    worker.enqueue(lecture.id)

    flash(request, f"“{lecture.title}” uploaded. Transcription has started.", "success")
    return _redirect(f"/lectures/{lecture.id}")


@router.post("/lectures/import")
def import_lecture(
    request: Request,
    url: str = Form(...),
    title: str = Form(""),
    description: str = Form(""),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    try:
        url = download.validate_url(url)
    except download.DownloadError as exc:
        flash(request, str(exc), "error")
        return _redirect("/dashboard")

    title = title.strip()
    lecture = Lecture(
        teacher_id=teacher.id,
        title=title[:200] or worker.PENDING_TITLE,
        description=description.strip(),
        media_type=MediaType.VIDEO,  # corrected after download
        original_filename="",
        media_path="",
        source_url=url[:2000],
        status=LectureStatus.DOWNLOADING,
    )
    db.add(lecture)
    db.commit()
    worker.enqueue(lecture.id)
    flash(request, "Importing the video. Transcription starts as soon as the download finishes.", "success")
    return _redirect(f"/lectures/{lecture.id}")


@router.get("/lectures/{lecture_id}")
def lecture_detail(
    lecture_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    lecture = get_viewable_lecture(lecture_id, user, db)
    return templates.TemplateResponse(
        request,
        "lectures/detail.html",
        {
            "user": user, "lecture": lecture, "notes_enabled": settings.notes_enabled, "shared": False,
            "media_url": f"/lectures/{lecture.id}/media", "quiz_url": f"/lectures/{lecture.id}/quiz", "ready": True,
            "thumb_url": f"/lectures/{lecture.id}/thumbnail", "stages": pipeline.stages(lecture),
            "continue_label": pipeline.continue_label(lecture),
        },
    )


@router.get("/lectures/{lecture_id}/media")
def lecture_media(lecture_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    lecture = get_viewable_lecture(lecture_id, user, db)
    path = Path(lecture.media_path) if lecture.media_path else None
    if path is None or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Media file missing")
    # FileResponse supports HTTP Range requests, so the player can seek.
    return FileResponse(path, filename=lecture.original_filename, content_disposition_type="inline")


@router.post("/lectures/{lecture_id}/retry")
def retry_lecture(
    lecture_id: int, request: Request, teacher: User = Depends(require_teacher), db: Session = Depends(get_db)
):
    lecture = get_owned_lecture(lecture_id, teacher, db)
    if lecture.status not in (LectureStatus.FAILED, LectureStatus.CANCELLED):
        flash(request, "Only failed or stopped lectures can be restarted.", "error")
    else:
        # Continue from the stage that didn't finish (finished downloads/audio are reused).
        lecture.status = LectureStatus.DOWNLOADING if pipeline.resume_stage(lecture) == "downloading" else LectureStatus.UPLOADED
        lecture.progress = 0.0
        lecture.error = None
        db.commit()
        worker.enqueue(lecture.id)
        flash(request, f"Continuing from: {pipeline.LABELS[pipeline.resume_stage(lecture)]}.", "success")
    return _redirect(f"/lectures/{lecture.id}")


@router.post("/lectures/{lecture_id}/notes")
def generate_lecture_notes(
    lecture_id: int, request: Request, teacher: User = Depends(require_teacher), db: Session = Depends(get_db)
):
    lecture = get_owned_lecture(lecture_id, teacher, db)
    if not settings.notes_enabled:
        flash(request, "Notes are turned off (NOTES_ENABLED=false in .env).", "error")
    elif lecture.status != LectureStatus.COMPLETED or lecture.transcript is None:
        flash(request, "Notes can be generated once the transcript is ready.", "error")
    elif lecture.notes and lecture.notes.status.is_processing:
        flash(request, "Notes are already being generated.", "info")
    else:
        worker.enqueue_notes(lecture.id)
        flash(request, "Generating notes with the local model. This takes a few minutes for a long lecture.", "success")
    return _redirect(f"/lectures/{lecture.id}#notes")


EXPORT_FORMATS = {
    "md": ("text/markdown; charset=utf-8", export.to_markdown),
    "pdf": ("application/pdf", export.to_pdf),
    "srt": ("application/x-subrip; charset=utf-8", export.to_srt),
}


@router.get("/lectures/{lecture_id}/export.{fmt}")
def export_lecture(lecture_id: int, fmt: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    lecture = get_viewable_lecture(lecture_id, user, db)
    if fmt not in EXPORT_FORMATS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown export format")
    if lecture.transcript is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transcript not ready")
    media_type, render = EXPORT_FORMATS[fmt]
    body = render(lecture)
    filename = f"{export.safe_filename(lecture.title)}.{fmt}"
    return Response(
        body if isinstance(body, bytes) else body.encode("utf-8"),
        media_type=media_type,
        headers={"Content-Disposition": export.content_disposition(filename)},
    )


@router.post("/lectures/{lecture_id}/delete")
def delete_lecture(
    lecture_id: int, request: Request, teacher: User = Depends(require_teacher), db: Session = Depends(get_db)
):
    lecture = get_owned_lecture(lecture_id, teacher, db)
    if lecture.status.is_processing:
        flash(request, "Wait for processing to finish before deleting this lecture.", "error")
        return _redirect(f"/lectures/{lecture.id}")

    files = [lecture.media_path, lecture.audio_path, lecture.thumbnail_path]
    title = lecture.title
    db.delete(lecture)
    db.commit()
    for f in files:
        if f:
            Path(f).unlink(missing_ok=True)
    flash(request, f"Deleted “{title}”.", "info")
    return _redirect("/dashboard")
