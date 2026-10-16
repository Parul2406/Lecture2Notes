import logging
import os
import threading
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np

from app.config import settings

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000  # must match the -ar used in media.extract_audio

_model = None
_model_lock = threading.Lock()


def get_model():
    """Load the Whisper model once, on first use (the first load downloads the weights)."""
    global _model
    with _model_lock:
        if _model is None:
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")  # noisy on Windows
            from faster_whisper import WhisperModel
            from faster_whisper.audio import pad_or_trim

            def load(device: str):
                log.info(
                    "Loading Whisper model '%s' (device=%s, compute_type=%s)",
                    settings.whisper_model, device, settings.whisper_compute_type,
                )
                model = WhisperModel(
                    settings.whisper_model,
                    device=device,
                    compute_type=settings.whisper_compute_type,
                    download_root=str(settings.data_dir / "models"),
                )
                # Missing CUDA libraries (cuBLAS/cuDNN) only surface on the first encode,
                # so run a tiny one now instead of failing mid-lecture.
                features = model.feature_extractor(np.zeros(SAMPLE_RATE, dtype=np.float32))
                model.encode(pad_or_trim(features))
                return model

            try:
                _model = load(settings.whisper_device)
            except RuntimeError as exc:
                if settings.whisper_device == "cpu":
                    raise
                log.warning("Whisper could not run on '%s' (%s); falling back to CPU", settings.whisper_device, exc)
                _model = load("cpu")
        return _model


def load_wav(path: Path) -> np.ndarray:
    """Read the 16 kHz mono s16le WAV produced by ffmpeg into float32 samples in [-1, 1].

    Decoding it ourselves avoids faster-whisper's PyAV-based decoder entirely.
    """
    with wave.open(str(path), "rb") as wf:
        if wf.getframerate() != SAMPLE_RATE or wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise ValueError(f"Expected 16 kHz mono 16-bit WAV, got {wf.getparams()}")
        frames = wf.readframes(wf.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(audio_path: Path, on_progress: Callable[[float], None] | None = None) -> dict:
    """Run Whisper locally and return a JSON-serialisable transcript with timestamped segments."""
    model = get_model()
    segments_iter, info = model.transcribe(
        load_wav(audio_path),
        language=settings.whisper_language or None,
        beam_size=settings.whisper_beam_size,
        word_timestamps=settings.whisper_word_timestamps,
        vad_filter=True,  # skip long silences, reduces hallucinations
    )

    segments: list[dict] = []
    # segments_iter is lazy: transcription actually happens while we iterate.
    for seg in segments_iter:
        item = {
            "id": seg.id,
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": seg.text.strip(),
            "avg_logprob": round(seg.avg_logprob, 4),
            "no_speech_prob": round(seg.no_speech_prob, 4),
        }
        if seg.words:
            item["words"] = [
                {
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                    "word": w.word.strip(),
                    "probability": round(w.probability, 4),
                }
                for w in seg.words
            ]
        segments.append(item)
        if on_progress and info.duration:
            on_progress(min(seg.end / info.duration, 1.0))

    return {
        "model": settings.whisper_model,
        "language": info.language,
        "language_probability": round(info.language_probability, 4),
        "duration": round(info.duration, 3),
        "text": " ".join(s["text"] for s in segments),
        "segments": segments,
    }
