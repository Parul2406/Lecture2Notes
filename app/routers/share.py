"""Share links (public lecture + quiz pages), graded quizzes and live results."""

import ipaddress
import secrets
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Lecture, LectureStatus, Notes, NotesStatus, QuizAttempt, User
from app.routers.lectures import get_owned_lecture, get_viewable_lecture
from app.security import get_current_user, require_teacher, require_user
from app.services import pipeline, quiz, worker
from app.templating import flash, templates

router = APIRouter()


# ---------------------------------------------------------------- helpers

def _lan_ip() -> str | None:
    """This computer's address on the local network (no packets are sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        return None if ipaddress.ip_address(ip).is_loopback else ip
    except OSError:
        return None


def base_url(request: Request) -> str:
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def share_info(request: Request, lecture: Lecture) -> dict:
    base = base_url(request)
    host = request.url.hostname or ""
    local_only = not settings.public_base_url and host in ("127.0.0.1", "localhost", "::1")
    lan = _lan_ip() if local_only else None
    return {
        "enabled": lecture.share_token is not None,
        "lecture_url": f"{base}/s/{lecture.share_token}" if lecture.share_token else None,
        "quiz_url": f"{base}/s/{lecture.share_token}/quiz" if lecture.share_token else None,
        "has_quiz": bool(quiz.questions(lecture)),
        "local_only": local_only,
        "lan_hint": f"http://{lan}:{request.url.port or 80}" if lan else None,
    }


def _shared_lecture(token: str, db: Session) -> Lecture:
    lecture = db.scalar(select(Lecture).where(Lecture.share_token == token)) if token else None
    if lecture is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This link is not valid or sharing was turned off.")
    return lecture


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------- teacher: share settings

@router.post("/api/lectures/{lecture_id}/share")
def update_share(
    lecture_id: int, request: Request, action: str = Body(..., embed=True),
    teacher: User = Depends(require_teacher), db: Session = Depends(get_db),
):
    lecture = get_owned_lecture(lecture_id, teacher, db)
    if action in ("enable", "reset"):
        if action == "reset" or not lecture.share_token:
            lecture.share_token = secrets.token_urlsafe(12)
    elif action == "disable":
        lecture.share_token = None
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown action")
    db.commit()
    return share_info(request, lecture)


@router.get("/api/lectures/{lecture_id}/share")
def get_share(lecture_id: int, request: Request, teacher: User = Depends(require_teacher), db: Session = Depends(get_db)):
    return share_info(request, get_owned_lecture(lecture_id, teacher, db))


@router.get("/api/lectures/{lecture_id}/results")
def lecture_results(lecture_id: int, teacher: User = Depends(require_teacher), db: Session = Depends(get_db)):
    return quiz.results(get_owned_lecture(lecture_id, teacher, db))


@router.post("/lectures/{lecture_id}/results/clear")
def clear_results(lecture_id: int, request: Request, teacher: User = Depends(require_teacher), db: Session = Depends(get_db)):
    lecture = get_owned_lecture(lecture_id, teacher, db)
    lecture.attempts.clear()
    db.commit()
    flash(request, "Quiz results cleared.", "info")
    return _redirect(f"/lectures/{lecture.id}#results")


# ---------------------------------------------------------------- teacher: stop jobs, background jobs feed

@router.post("/api/lectures/{lecture_id}/stop")
def stop_job(
    lecture_id: int, what: str = Body("processing", embed=True),
    teacher: User = Depends(require_teacher), db: Session = Depends(get_db),
):
    get_owned_lecture(lecture_id, teacher, db)
    stopped = {"notes": worker.stop_notes, "redownload": worker.stop_redownload}.get(what, worker.stop_lecture)(lecture_id)
    if not stopped:
        raise HTTPException(status.HTTP_409_CONFLICT, "Nothing is running for this lecture.")
    return {"ok": True}


@router.get("/api/jobs")
def my_jobs(teacher: User = Depends(require_teacher), db: Session = Depends(get_db)):
    """Running and recently finished jobs of this teacher, for the live tasks panel on every page."""
    lectures = db.scalars(select(Lecture).where(Lecture.teacher_id == teacher.id)).all()
    recent_cutoff = datetime.now(timezone.utc) - timedelta(seconds=90)

    def recent(dt):
        return dt is not None and (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)) >= recent_cutoff

    active, finished = [], []
    for l in lectures:
        stopping = worker.stopping(l.id)
        if l.status.is_processing:
            active.append({"key": f"t{l.id}", "lecture_id": l.id, "title": l.title, "kind": "processing",
                           "stage": l.status.value, "progress": round(l.progress or 0, 4), "stopping": stopping["transcription"]})
        elif recent(l.updated_at) and l.status in (LectureStatus.COMPLETED, LectureStatus.FAILED, LectureStatus.CANCELLED):
            finished.append({"key": f"t{l.id}", "lecture_id": l.id, "title": l.title, "result": l.status.value})
        if l.redownload_progress is not None:
            active.append({"key": f"r{l.id}", "lecture_id": l.id, "title": l.title, "kind": "redownload",
                           "stage": "redownloading", "progress": round(l.redownload_progress, 4), "stopping": stopping["redownload"]})
        n: Notes | None = l.notes
        if n and n.status.is_processing:
            active.append({"key": f"n{l.id}", "lecture_id": l.id, "title": l.title, "kind": "notes",
                           "stage": "writing_notes" if n.status == NotesStatus.GENERATING else "notes_queued",
                           "progress": round(n.progress or 0, 4), "stopping": stopping["notes"]})
        elif n and recent(n.updated_at):
            finished.append({"key": f"n{l.id}", "lecture_id": l.id, "title": l.title, "result": f"notes_{n.status.value}"})
    return {"active": active, "finished": finished}


# ---------------------------------------------------------------- public shared lecture

@router.get("/s/{token}")
def shared_lecture(token: str, request: Request, user: User | None = Depends(get_current_user), db: Session = Depends(get_db)):
    lecture = _shared_lecture(token, db)
    return templates.TemplateResponse(request, "lectures/detail.html", {
        "user": user, "lecture": lecture, "shared": True, "notes_enabled": settings.notes_enabled,
        "media_url": f"/s/{token}/media", "quiz_url": f"/s/{token}/quiz", "thumb_url": f"/s/{token}/thumbnail",
        "ready": bool(lecture.media_path), "stages": pipeline.stages(lecture), "continue_label": None,
    })


@router.get("/s/{token}/media")
def shared_media(token: str, db: Session = Depends(get_db)):
    lecture = _shared_lecture(token, db)
    path = Path(lecture.media_path) if lecture.media_path else None
    if path is None or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Media not available")
    return FileResponse(path, filename=lecture.original_filename, content_disposition_type="inline")


@router.get("/s/{token}/thumbnail")
def shared_thumbnail(token: str, db: Session = Depends(get_db)):
    lecture = _shared_lecture(token, db)
    if not lecture.thumbnail_path or not Path(lecture.thumbnail_path).is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No thumbnail")
    return FileResponse(lecture.thumbnail_path, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- quiz (shared link or logged in)

def _quiz_page(request: Request, lecture: Lecture, user: User | None, action: str, back_url: str, **extra):
    return templates.TemplateResponse(request, "quiz.html", {
        "user": user, "lecture": lecture, "questions": quiz.public_questions(lecture),
        "action": action, "back_url": back_url, **extra,
    })


def _submit(request: Request, lecture: Lecture, user: User | None, form, result_base: str, db: Session):
    if not quiz.questions(lecture):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This lecture has no quiz yet.")
    name = (user.name if user else (form.get("name") or "")).strip()
    if not name:
        flash(request, "Please enter your name before submitting.", "error")
        return None
    attempt = quiz.grade(lecture, dict(form), name, user)
    db.add(attempt)
    db.commit()
    return _redirect(f"{result_base}/{attempt.public_id}")


def _result_page(request: Request, lecture: Lecture, user: User | None, public_id: str, retake_url: str, back_url: str, db: Session):
    attempt = db.scalar(select(QuizAttempt).where(QuizAttempt.public_id == public_id, QuizAttempt.lecture_id == lecture.id))
    if attempt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Result not found")
    return templates.TemplateResponse(request, "quiz.html", {
        "user": user, "lecture": lecture, "attempt": attempt, "review": quiz.review(lecture, attempt),
        "retake_url": retake_url, "back_url": back_url,
    })


@router.get("/s/{token}/quiz")
def shared_quiz(token: str, request: Request, user: User | None = Depends(get_current_user), db: Session = Depends(get_db)):
    lecture = _shared_lecture(token, db)
    return _quiz_page(request, lecture, user, f"/s/{token}/quiz", f"/s/{token}")


@router.post("/s/{token}/quiz")
async def shared_quiz_submit(token: str, request: Request, user: User | None = Depends(get_current_user), db: Session = Depends(get_db)):
    lecture = _shared_lecture(token, db)
    form = await request.form()
    return _submit(request, lecture, user, form, f"/s/{token}/quiz/r", db) or _redirect(f"/s/{token}/quiz")


@router.get("/s/{token}/quiz/r/{public_id}")
def shared_quiz_result(token: str, public_id: str, request: Request, user: User | None = Depends(get_current_user), db: Session = Depends(get_db)):
    lecture = _shared_lecture(token, db)
    return _result_page(request, lecture, user, public_id, f"/s/{token}/quiz", f"/s/{token}", db)


@router.get("/lectures/{lecture_id}/quiz")
def lecture_quiz(lecture_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    lecture = get_viewable_lecture(lecture_id, user, db)
    return _quiz_page(request, lecture, user, f"/lectures/{lecture.id}/quiz", f"/lectures/{lecture.id}")


@router.post("/lectures/{lecture_id}/quiz")
async def lecture_quiz_submit(lecture_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    lecture = get_viewable_lecture(lecture_id, user, db)
    form = await request.form()
    return _submit(request, lecture, user, form, f"/lectures/{lecture.id}/quiz/r", db) or _redirect(f"/lectures/{lecture.id}/quiz")


@router.get("/lectures/{lecture_id}/quiz/r/{public_id}")
def lecture_quiz_result(lecture_id: int, public_id: str, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    lecture = get_viewable_lecture(lecture_id, user, db)
    return _result_page(request, lecture, user, public_id, f"/lectures/{lecture.id}/quiz", f"/lectures/{lecture.id}", db)
