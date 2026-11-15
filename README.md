# Lecture to Notes

Teachers upload lecture recordings (video or audio) or import them from a link (YouTube, Vimeo, a direct
`.mp4`/`.mp3` URL, ...). The server extracts the audio with **FFmpeg** and transcribes
it locally with **Whisper** ([faster-whisper](https://github.com/SYSTRAN/faster-whisper)). The timestamped transcript
is stored in the database as JSON. A **local LLM** (via [Ollama](https://ollama.com)) then turns the transcript into study notes: a summary, sections with key
points, key definitions and practice multiple-choice questions, each linked to the moment in the lecture it came from.
Students can play lectures, click any transcript line or note to jump to that moment, and download notes and
transcript as PDF, Markdown or SRT subtitles.

**Everything runs on this machine.** Speech recognition and notes generation both use local models; no lecture
audio or text is sent to any cloud API.

**Stack:** FastAPI · Jinja2 templates · SQLAlchemy (SQLite by default) · session-cookie auth (bcrypt) · FFmpeg · faster-whisper · Ollama (local LLM) · fpdf2

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
- **Notes:** install [Ollama](https://ollama.com/download), then download the model once with `ollama pull gemma4`
  (about 9.6 GB). Ollama runs in the background and uses the GPU when one is available. If Ollama isn't running,
  everything else still works and the lecture page shows what to do. See [Notes](#notes) below.
- **Import from a link:** uses [yt-dlp](https://github.com/yt-dlp/yt-dlp). YouTube also needs a JavaScript
  runtime; Node.js or Deno is picked up automatically if installed. Videos are saved at up to 720p.
  `MAX_UPLOAD_MB` and `MAX_DOWNLOAD_HOURS` limit what can be imported, and links to `localhost` or the local
  network are refused.
- **PDF fonts:** PDF export uses a system font (Arial/Segoe UI on Windows, DejaVu on Linux) plus fallback fonts for
  Indic, Chinese, Japanese and Korean scripts (Nirmala UI, Microsoft YaHei, ... / Noto). Set `PDF_FONT` to a `.ttf`
  to override the main font.

## How it works

```
link import (teacher) ─► yt-dlp download ─► data/uploads/lecture_<id>_src.<ext>
                     │  status: downloading (progress 0..1)
                     ▼
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
                     │  (if NOTES_AUTO=true)
                     ▼
notes worker ──────► local LLM via Ollama, part by part ──► notes table (content JSON)
                     notes status: pending → generating → completed | failed
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

## Notes

`app/services/notes.py` generates notes with a local model served by Ollama on `localhost`. Nothing leaves the
computer, and Ollama *cloud* models are refused with an error.

- **Long lectures** are split into parts of about `NOTES_CHUNK_WORDS` words (1,500 by default). Each part becomes
  sections with key points, definitions and practice questions; a final call writes the overall summary from the
  section notes. This keeps each request small enough for a laptop GPU, and the page shows progress per part.
- **Timestamps:** each part is sent as numbered segments (`[12] text`). The model's output is constrained to a JSON
  schema in which every item names the segment it came from, and the app turns that into a timestamp.
- Notes are generated automatically after transcription (`NOTES_AUTO=true`). Teachers can also press
  **Generate notes** or **Regenerate notes**. Failures (Ollama not running, model not downloaded, bad output) are
  shown on the page with what to do, and can be retried.
- Notes run on their own thread, one lecture at a time, so they never block transcription of new uploads.
- **Model:** `NOTES_MODEL=gemma4` by default. Tested on an RTX 3050 6 GB laptop with a 5.7-minute lecture:

  | Model | Time | Notes |
  |---|---|---|
  | `gemma4` (8B, default) | ~70–90 s | accurate summaries, good questions |
  | `llama3.1:8b` | ~130 s | weaker: some definitions duplicated or wrong |

  A one-hour lecture takes roughly 10–15 minutes with `gemma4`. Any other local Ollama model works
  (`ollama pull <name>`, then set `NOTES_MODEL`).
- Other settings: `OLLAMA_URL` (default `http://127.0.0.1:11434`), `NOTES_NUM_CTX` (context window, 8192),
  `NOTES_TIMEOUT` (seconds per model call), `NOTES_ENABLED`.

```json
{
  "summary": "...",
  "sections": [{"title": "Lists", "start": 3.5, "summary": "...", "key_points": [{"text": "...", "start": 3.5}]}],
  "definitions": [{"term": "List", "definition": "...", "start": 3.5}],
  "mcqs": [{"question": "...", "options": ["...", "...", "...", "..."], "answer_index": 0, "explanation": "...", "start": 65.0}]
}
```

## Export

From the lecture page: **PDF** and **Markdown** (notes, practice questions with an answer key, and the full
timestamped transcript) or **SRT** subtitles (transcript only). Files are named after the lecture title.

## Roles and access

| | Teacher | Student |
|---|---|---|
| Upload / retry / delete lectures | own lectures | – |
| See lectures | own lectures (any status) | all **completed** lectures |
| Transcript page and JSON | ✓ | ✓ |
| Generate / regenerate notes | own lectures | – |
| Read notes, export PDF / Markdown / SRT | ✓ | ✓ |

## Routes

| Method | Path | |
|---|---|---|
| GET/POST | `/signup`, `/login` | auth pages |
| POST | `/logout` | |
| GET | `/dashboard` | teacher: upload and list own lectures; student: list lectures |
| POST | `/lectures` | upload (teacher) |
| POST | `/lectures/import` | import from a link: `url`, optional `title`, `description` (teacher) |
| GET | `/lectures/{id}` | player and transcript |
| GET | `/lectures/{id}/media` | original file (supports Range, so the player can seek) |
| POST | `/lectures/{id}/retry`, `/lectures/{id}/delete` | teacher |
| POST | `/lectures/{id}/notes` | generate or regenerate notes (teacher) |
| GET | `/lectures/{id}/export.pdf`, `.md`, `.srt` | download notes + transcript |
| GET | `/api/lectures/{id}/status` | `{status, progress, error}` |
| GET | `/api/lectures/{id}/transcript` | full transcript JSON |
| GET | `/api/lectures/{id}/notes` | `{status, progress, error, model, content}` |

## Project layout

```
app/
  main.py            FastAPI app, session middleware, startup (create tables, resume queue)
  config.py          settings from env / .env
  database.py        engine, session, Base
  models.py          User, Lecture, Transcript, Notes
  security.py        bcrypt hashing, session login, require_user / require_teacher
  templating.py      Jinja setup, flash messages, timestamp filter
  routers/           auth.py, lectures.py, api.py
  services/          media.py (ffmpeg), download.py (link import, yt-dlp), transcription.py (whisper), notes.py (local LLM),
                     export.py (PDF/Markdown/SRT), worker.py (background jobs)
  templates/, static/
data/                created at runtime: app.db, uploads/, audio/, models/
```

## Design

Light theme by default (a dark theme is one click away in the header). Headings use Source Serif 4, the interface
IBM Plex Sans and timestamps IBM Plex Mono; the font files are bundled in `app/static/fonts/` (SIL Open Font
License), so the app needs no internet for fonts. Icons are inline SVG.

## Known limitations / next steps

- New columns are added automatically on startup (`add_missing_columns` in `app/database.py`), but only nullable
  ones; use Alembic for anything more involved.

- In-process worker: one machine, one job at a time. Move to Celery/RQ/Arq with Redis for scale.
- No CSRF tokens (SameSite=Lax cookies cover the common cases), email verification, or password reset yet.
- No courses or enrollment: every student sees every completed lecture.
- Uploaded files are stored on local disk. Use S3-compatible storage for production.
