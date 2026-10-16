from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.routers.lectures import get_viewable_lecture
from app.security import require_user

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/lectures/{lecture_id}/status")
def lecture_status(lecture_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    lecture = get_viewable_lecture(lecture_id, user, db)
    return {
        "id": lecture.id,
        "status": lecture.status.value,
        "progress": round(lecture.progress, 4),
        "error": lecture.error,
    }


@router.get("/lectures/{lecture_id}/transcript")
def lecture_transcript(lecture_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    lecture = get_viewable_lecture(lecture_id, user, db)
    if lecture.transcript is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transcript not ready")
    return {"title": lecture.title, **lecture.transcript.as_dict()}
