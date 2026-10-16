import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Enum, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    TEACHER = "teacher"
    STUDENT = "student"


class LectureStatus(str, enum.Enum):
    UPLOADED = "uploaded"
    EXTRACTING_AUDIO = "extracting_audio"
    TRANSCRIBING = "transcribing"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def is_processing(self) -> bool:
        return self in (LectureStatus.UPLOADED, LectureStatus.EXTRACTING_AUDIO, LectureStatus.TRANSCRIBING)


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
    media_path: Mapped[str] = mapped_column(String(500))
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
