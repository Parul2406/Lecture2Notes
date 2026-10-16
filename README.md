# Lecture to Notes (v1)

Teachers upload lecture recordings (video or audio). The server extracts the audio with **FFmpeg** and transcribes
it locally with **Whisper** ([faster-whisper](https://github.com/SYSTRAN/faster-whisper)). The timestamped transcript
is stored in the database as JSON. Students can browse lectures, play them, and click any line of the transcript to
jump to that moment.

**Stack:** FastAPI · Jinja2 templates · SQLAlchemy (SQLite by default) · session-cookie auth (bcrypt) · FFmpeg · faster-whisper

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (source .venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
copy .env.example .env          # optional, every setting has a default
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000, sign up as a teacher, and upload a lecture.

- **FFmpeg:** the system `ffmpeg` is used if it is on PATH. Otherwise the binary bundled with `imageio-ffmpeg` is
  used, so it works with no extra install. Set `FFMPEG_BINARY` to force a specific binary.
- **Whisper model:** downloaded to `data/models/` the first time a lecture is transcribed. `WHISPER_MODEL=small` is
  the default; use `tiny`/`base` for speed or `medium`/`large-v3` for accuracy.
- **GPU:** with `WHISPER_DEVICE=auto`, CUDA is used if it works. If not, the app logs a warning and falls back to CPU.
  To enable an NVIDIA GPU, run `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` (CUDA 12 / cuDNN 9) and
  set `WHISPER_COMPUTE_TYPE=float16`.

## How it works

```
upload (teacher) ──► data/uploads/<uuid>.<ext>
                     │  status: uploaded
                     ▼
background worker ─► ffmpeg -vn -ac 1 -ar 16000 pcm_s16le ──► data/audio/lecture_<id>.wav
                     │  status: extracting_audio
                     ▼
                     faster-whisper (VAD filter, word timestamps)
                     │  status: transcribing (progress 0..1)
                     ▼
                     transcripts table (segments JSON)      status: completed | failed
```

- The worker is a single in-process thread (`app/services/worker.py`), so lectures are transcribed one at a time.
  Lectures that were still processing when the server stopped are re-queued on startup.
- The lecture page polls `/api/lectures/{id}/status` and reloads when processing finishes.
- Failed lectures show the error, and the teacher can **Retry** them.

### Transcript format (`transcripts.segments`)

```json
{
  "lecture_id": 1, "model": "small", "language": "en", "language_probability": 0.99,
  "duration": 12.864, "text": "Welcome to today's lecture on Python. ...",
  "segments": [
    {
      "id": 1, "start": 0.0, "end": 2.2, "text": "Welcome to today's lecture on Python.",
      "avg_logprob": -0.19, "no_speech_prob": 0.017,
      "words": [{"start": 0.0, "end": 0.52, "word": "Welcome", "probability": 0.89}]
    }
  ]
}
```

`words` is present when `WHISPER_WORD_TIMESTAMPS=true` (the default).

## Roles and access

| | Teacher | Student |
|---|---|---|
| Upload / retry / delete lectures | own lectures | – |
| See lectures | own lectures (any status) | all **completed** lectures |
| Transcript page and JSON | ✓ | ✓ |

## Routes

| Method | Path | |
|---|---|---|
| GET/POST | `/signup`, `/login` | auth pages |
| POST | `/logout` | |
| GET | `/dashboard` | teacher: upload and list own lectures; student: list lectures |
| POST | `/lectures` | upload (teacher) |
| GET | `/lectures/{id}` | player and transcript |
| GET | `/lectures/{id}/media` | original file (supports Range, so the player can seek) |
| POST | `/lectures/{id}/retry`, `/lectures/{id}/delete` | teacher |
| GET | `/api/lectures/{id}/status` | `{status, progress, error}` |
| GET | `/api/lectures/{id}/transcript` | full transcript JSON |

## Project layout

```
app/
  main.py            FastAPI app, session middleware, startup (create tables, resume queue)
  config.py          settings from env / .env
  database.py        engine, session, Base
  models.py          User, Lecture, Transcript
  security.py        bcrypt hashing, session login, require_user / require_teacher
  templating.py      Jinja setup, flash messages, timestamp filter
  routers/           auth.py, lectures.py, api.py
  services/          media.py (ffmpeg), transcription.py (whisper), worker.py (background jobs)
  templates/, static/
data/                created at runtime: app.db, uploads/, audio/, models/
```

## Known v1 limitations / next steps

- Tables are created with `create_all`. Add Alembic before changing the schema.
- In-process worker: one machine, one job at a time. Move to Celery/RQ/Arq with Redis for scale.
- No CSRF tokens (SameSite=Lax cookies cover the common cases), email verification, or password reset yet.
- No courses or enrollment: every student sees every completed lecture.
- Uploaded files are stored on local disk. Use S3-compatible storage for production.
