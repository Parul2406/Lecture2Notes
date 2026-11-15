import secrets
from functools import cached_property
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    app_name: str = "Lecture to Notes"
    data_dir: Path = BASE_DIR / "data"
    database_url: str = f"sqlite:///{(BASE_DIR / 'data' / 'app.db').as_posix()}"
    secret_key: str = ""
    secure_cookies: bool = False
    max_upload_mb: int = 2048  # also the size limit for videos imported from a link
    max_download_hours: float = 4  # longest video that can be imported from a link
    # Address students use to reach this server, for share links (e.g. http://192.168.1.20:8000).
    # Empty = the address the teacher's browser is using.
    public_base_url: str = ""

    ffmpeg_binary: str = ""

    whisper_model: str = "small"
    whisper_device: str = "auto"
    whisper_compute_type: str = "int8"
    whisper_language: str = ""
    whisper_beam_size: int = 5
    whisper_word_timestamps: bool = True

    # Notes generation with a local LLM served by Ollama (runs on this machine, no cloud APIs)
    notes_enabled: bool = True
    notes_auto: bool = True  # generate notes automatically after each transcription
    ollama_url: str = "http://127.0.0.1:11434"
    notes_model: str = "gemma4"  # any local Ollama model, e.g. gemma4, llama3.1:8b, qwen2.5:7b
    notes_chunk_words: int = 1500  # transcript words per model call
    notes_num_ctx: int = 8192  # model context window (tokens)
    notes_timeout: float = 900  # seconds per model call

    # PDF export: path to a Unicode .ttf font; empty = auto-detect a system font
    pdf_font: str = ""

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def thumbnails_dir(self) -> Path:
        return self.data_dir / "thumbnails"

    @cached_property
    def session_secret(self) -> str:
        """Use SECRET_KEY if set, otherwise a key persisted in the data dir so sessions survive restarts."""
        if self.secret_key:
            return self.secret_key
        key_file = self.data_dir / ".secret_key"
        if key_file.exists():
            return key_file.read_text().strip()
        key = secrets.token_urlsafe(48)
        key_file.write_text(key)
        return key

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.uploads_dir, self.audio_dir, self.thumbnails_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
