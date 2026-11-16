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
_dll_dirs_added = False


def _add_cuda_dll_dirs() -> None:
    """Let CTranslate2 find the CUDA libraries installed as pip packages (nvidia-cublas-cu12,
    nvidia-cudnn-cu12). On Windows their DLLs live in site-packages/nvidia/*/bin, which isn't on
    the DLL search path by default."""
    global _dll_dirs_added
    if _dll_dirs_added or os.name != "nt":
        return
    _dll_dirs_added = True
    try:
        import nvidia
    except ImportError:
        return  # CUDA packages not installed: Whisper will use the CPU
    for root in getattr(nvidia, "__path__", []):
        for bin_dir in Path(root).glob("*/bin"):
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


def _compute_type(device: str) -> str:
    """'auto' = float16 on the GPU (fast, accurate), int8 on the CPU (fast, low memory)."""
    if settings.whisper_compute_type != "auto":
        return settings.whisper_compute_type
    return "float16" if device == "cuda" else "int8"


def _cuda_devices() -> int:
    _add_cuda_dll_dirs()
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count()
    except Exception:  # noqa: BLE001 - no CUDA driver at all
        return 0


def _load(device: str):
    """Load the model on a concrete device ("cuda" or "cpu") and prove it works with a tiny encode."""
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")  # noisy on Windows
    from faster_whisper import WhisperModel
    from faster_whisper.audio import pad_or_trim

    compute_type = _compute_type(device)
    log.info("Loading Whisper model '%s' (device=%s, compute_type=%s)", settings.whisper_model, device, compute_type)
    model = WhisperModel(
        settings.whisper_model,
        device=device,
        compute_type=compute_type,
        download_root=str(settings.data_dir / "models"),
    )
    model.device_used = device
    # Missing CUDA libraries (cuBLAS/cuDNN) only surface on the first encode,
    # so run a tiny one now instead of failing mid-lecture.
    features = model.feature_extractor(np.zeros(SAMPLE_RATE, dtype=np.float32))
    model.encode(pad_or_trim(features))
    return model


def get_model():
    """Load the Whisper model once, on the fastest device that works: an NVIDIA GPU (float16) when one
    and the CUDA libraries are available, otherwise the CPU (int8). The first load downloads the weights."""
    global _model
    with _model_lock:
        if _model is None:
            device = settings.whisper_device
            if device == "auto":
                device = "cuda" if _cuda_devices() > 0 else "cpu"
            try:
                _model = _load(device)
            except (RuntimeError, OSError) as exc:
                if device == "cpu":
                    raise
                log.warning("Whisper could not run on the GPU (%s); using the CPU instead", exc)
                _model = _load("cpu")
        return _model


def _switch_to_cpu() -> None:
    global _model
    with _model_lock:
        _model = None
        _model = _load("cpu")


def _gpu_name() -> str | None:
    import subprocess

    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
        return out[0].strip() if out else None
    except (OSError, subprocess.SubprocessError):
        return None


def hardware_summary() -> dict:
    """What transcription will run on, without loading the model (for the log and the settings page)."""
    if settings.whisper_device == "cpu":
        return {"device": "cpu", "compute_type": _compute_type("cpu"), "detail": "CPU (set by WHISPER_DEVICE=cpu)"}
    gpus = _cuda_devices()
    if gpus:
        name = _gpu_name() or "NVIDIA GPU"
        return {"device": "cuda", "compute_type": _compute_type("cuda"), "detail": f"GPU: {name}"}
    try:
        import nvidia.cublas  # noqa: F401  - CUDA libraries are installed, so there's just no usable GPU
        reason = "no NVIDIA GPU found"
    except ImportError:
        reason = "NVIDIA CUDA libraries not installed" if _gpu_name() else "no NVIDIA GPU found"
    return {"device": "cpu", "compute_type": _compute_type("cpu"), "detail": f"CPU ({reason})"}


def load_wav(path: Path) -> np.ndarray:
    """Read the 16 kHz mono s16le WAV produced by ffmpeg into float32 samples in [-1, 1].

    Decoding it ourselves avoids faster-whisper's PyAV-based decoder entirely.
    """
    with wave.open(str(path), "rb") as wf:
        if wf.getframerate() != SAMPLE_RATE or wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise ValueError(f"Expected 16 kHz mono 16-bit WAV, got {wf.getparams()}")
        frames = wf.readframes(wf.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


class TranscriptionError(Exception):
    pass


def _child_main(audio_path: str, data_conn, ctrl_conn) -> None:
    """Runs in a separate process: transcribe and send progress/result back to the server."""
    # Exit as soon as the server goes away or asks us to stop (the control pipe closes or gets a message),
    # so a killed or restarted server never leaves Whisper running in the background.
    def watchdog():
        try:
            ctrl_conn.recv()
        except (EOFError, OSError):
            pass
        os._exit(0)

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        result = transcribe(Path(audio_path), on_progress=lambda p: data_conn.send(("progress", p)))
        data_conn.send(("result", result))
    except Exception as exc:  # noqa: BLE001 - report every failure to the parent
        data_conn.send(("error", f"{exc.__class__.__name__}: {exc}"))


def transcribe_killable(
    audio_path: Path,
    on_progress: Callable[[float], None] | None = None,
    check_stop: Callable[[], None] | None = None,
) -> dict:
    """Run transcribe() in a child process so a stop request ends it immediately.

    Whisper can spend minutes on one stretch of audio without yielding a segment, so checking for
    "stop" between segments isn't enough. Here the parent polls check_stop() every quarter second
    and terminates the child when it raises. Loading the model again per job costs a few seconds.
    """
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    data_parent, data_child = ctx.Pipe(duplex=False)
    ctrl_child, ctrl_parent = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child_main, args=(str(audio_path), data_child, ctrl_child),
                       name="whisper", daemon=True)
    proc.start()
    data_child.close()
    ctrl_child.close()
    try:
        while True:
            if check_stop:
                check_stop()  # raises -> finally kills the child
            if data_parent.poll(0.25):
                try:
                    kind, payload = data_parent.recv()
                except EOFError:
                    raise TranscriptionError(f"The transcription process exited unexpectedly (code {proc.exitcode}).")
                if kind == "progress":
                    if on_progress:
                        on_progress(payload)
                elif kind == "result":
                    return payload
                else:
                    raise TranscriptionError(payload)
            elif not proc.is_alive() and not data_parent.poll():
                raise TranscriptionError(f"The transcription process exited unexpectedly (code {proc.exitcode}).")
    finally:
        if proc.is_alive():
            try:
                ctrl_parent.send("stop")
            except OSError:
                pass
            proc.join(timeout=2)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=5)
        ctrl_parent.close()
        data_parent.close()


def _is_gpu_failure(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("cuda", "cublas", "cudnn", "out of memory", "gpu"))


def transcribe(audio_path: Path, on_progress: Callable[[float], None] | None = None) -> dict:
    """Run Whisper locally and return a JSON-serialisable transcript with timestamped segments.

    If the GPU fails mid-way (typically out of memory because another program, such as the local
    notes model, is using it), the lecture is transcribed again on the CPU instead of failing."""
    audio = load_wav(audio_path)
    model = get_model()
    try:
        return _transcribe(model, audio, on_progress)
    except RuntimeError as exc:
        if getattr(model, "device_used", "cpu") != "cuda" or not _is_gpu_failure(exc):
            raise
        log.warning("Transcription on the GPU failed (%s); switching to the CPU and starting this lecture again", exc)
        if on_progress:
            on_progress(0.0)
        _switch_to_cpu()
        return _transcribe(get_model(), audio, on_progress)


def _transcribe(model, audio, on_progress) -> dict:
    segments_iter, info = model.transcribe(
        audio,
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
        "device": getattr(model, "device_used", "cpu"),
        "language": info.language,
        "language_probability": round(info.language_probability, 4),
        "duration": round(info.duration, 3),
        "text": " ".join(s["text"] for s in segments),
        "segments": segments,
    }
