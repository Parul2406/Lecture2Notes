import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    TEACHER = "teacher"
    STUDENT = "student"


class LectureStatus(str, enum.Enum):
    DOWNLOADING = "downloading"  # imported from a link, file not fetched yet
    UPLOADED = "uploaded"
    EXTRACTING_AUDIO = "extracting_audio"
    TRANSCRIBING = "transcribing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"  # stopped by the teacher

    @property
    def is_processing(self) -> bool:
        return self in (
            LectureStatus.DOWNLOADING, LectureStatus.UPLOADED, LectureStatus.EXTRACTING_AUDIO, LectureStatus.TRANSCRIBING
        )


class NotesStatus(str, enum.Enum):
    PENDING = "pending"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"  # stopped by the teacher before any notes existed

    @property
    def is_processing(self) -> bool:
        return self in (NotesStatus.PENDING, NotesStatus.GENERATING)


class MediaType(str, enum.Enum):
    VIDEO = "video"
    AUDIO = "audio"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False, length=20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lectures: Mapped[list["Lecture"]] = relationship(back_populates="teacher", cascade="all, delete-orphan")

    @property
    def is_teacher(self) -> bool:
        return self.role == Role.TEACHER


class Lecture(Base):
    __tablename__ = "lectures"

    id: Mapped[int] = mapped_column(primary_key=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")

    media_type: Mapped[MediaType] = mapped_column(Enum(MediaType, native_enum=False, length=10))
    original_filename: Mapped[str] = mapped_column(String(255))
    media_path: Mapped[str] = mapped_column(String(500))  # "" until a link import has downloaded
    source_url: Mapped[str | None] = mapped_column(String(2000))  # set when imported from a link
    # Re-download of a linked video runs beside everything else; the current file is replaced only when it completes.
    redownload_progress: Mapped[float | None] = mapped_column(Float)  # None = not running, else 0..1
    redownload_error: Mapped[str | None] = mapped_column(Text)
    thumbnail_path: Mapped[str | None] = mapped_column(String(500))  # JPEG; auto from the video or uploaded
    thumbnail_custom: Mapped[bool | None] = mapped_column(default=False)  # True = teacher chose it; don't overwrite
    # Random token for the public share links (/s/<token>, /s/<token>/quiz); None = not shared
    share_token: Mapped[str | None] = mapped_column(String(64), index=True)
    audio_path: Mapped[str | None] = mapped_column(String(500))
    duration: Mapped[float | None] = mapped_column(Float)

    status: Mapped[LectureStatus] = mapped_column(
        Enum(LectureStatus, native_enum=False, length=20), default=LectureStatus.UPLOADED, index=True
    )
    progress: Mapped[float] = mapped_column(Float, default=0.0)  # 0..1 during transcription
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    teacher: Mapped[User] = relationship(back_populates="lectures")
    transcript: Mapped["Transcript | None"] = relationship(
        back_populates="lecture", uselist=False, cascade="all, delete-orphan"
    )
    notes: Mapped["Notes | None"] = relationship(
        back_populates="lecture", uselist=False, cascade="all, delete-orphan"
    )
    attempts: Mapped[list["QuizAttempt"]] = relationship(
        back_populates="lecture", cascade="all, delete-orphan", order_by="QuizAttempt.created_at.desc()"
    )


class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(primary_key=True)
    lecture_id: Mapped[int] = mapped_column(ForeignKey("lectures.id", ondelete="CASCADE"), unique=True)
    model: Mapped[str] = mapped_column(String(100))
    language: Mapped[str | None] = mapped_column(String(10))
    language_probability: Mapped[float | None] = mapped_column(Float)
    duration: Mapped[float | None] = mapped_column(Float)
    text: Mapped[str] = mapped_column(Text, default="")
    # [{"id", "start", "end", "text", "avg_logprob", "no_speech_prob", "words": [{"start", "end", "word", "probability"}]}]
    segments: Mapped[list[dict]] = mapped_column(JSON, default=list)
    edited: Mapped[bool | None] = mapped_column(default=False)  # corrected by the teacher
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lecture: Mapped[Lecture] = relationship(back_populates="transcript")

    def as_dict(self) -> dict:
        return {
            "lecture_id": self.lecture_id,
            "model": self.model,
            "language": self.language,
            "language_probability": self.language_probability,
            "duration": self.duration,
            "text": self.text,
            "segments": self.segments,
            "created_at": self.created_at.isoformat(),
        }


class Notes(Base):
    """Study notes generated by the local LLM from a lecture's transcript."""

    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    lecture_id: Mapped[int] = mapped_column(ForeignKey("lectures.id", ondelete="CASCADE"), unique=True)
    status: Mapped[NotesStatus] = mapped_column(
        Enum(NotesStatus, native_enum=False, length=20), default=NotesStatus.PENDING, index=True
    )
    progress: Mapped[float] = mapped_column(Float, default=0.0)  # 0..1 while generating
    error: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(100))
    # {"summary", "sections": [{"title", "start", "summary", "key_points": [{"text", "start"}]}],
    #  "definitions": [{"term", "definition", "start"}],
    #  "mcqs": [{"question", "options", "answer_index", "explanation", "start"}]}
    content: Mapped[dict | None] = mapped_column(JSON)
    edited: Mapped[bool | None] = mapped_column(default=False)  # changed by the teacher after generation
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    lecture: Mapped[Lecture] = relationship(back_populates="notes")

    def as_dict(self) -> dict:
        return {
            "lecture_id": self.lecture_id,
            "status": self.status.value,
            "progress": round(self.progress or 0.0, 4),
            "error": self.error,
            "model": self.model,
            "content": self.content,
            "updated_at": self.updated_at.isoformat(),
        }


class QuizAttempt(Base):
    """One submission of a lecture's quiz, graded on the server."""

    __tablename__ = "quiz_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # used in the result URL
    lecture_id: Mapped[int] = mapped_column(ForeignKey("lectures.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))  # None = via share link
    name: Mapped[str] = mapped_column(String(120))
    score: Mapped[int] = mapped_column(Integer)
    total: Mapped[int] = mapped_column(Integer)
    # [{"chosen": int | None, "correct": int}] in question order
    answers: Mapped[list[dict]] = mapped_column(JSON, default=list)
    quiz_version: Mapped[str] = mapped_column(String(40))  # fingerprint of the questions; changes on regenerate
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    lecture: Mapped[Lecture] = relationship(back_populates="attempts")

    @property
    def percent(self) -> int:
        return round(100 * self.score / self.total) if self.total else 0
