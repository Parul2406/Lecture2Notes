# Lecture2Notes

Teachers upload lecture recordings (video or audio) or import them from a link (YouTube, Vimeo, a direct
`.mp4`/`.mp3` URL, ...). The server extracts the audio with **FFmpeg** and transcribes
it locally with **Whisper** ([faster-whisper](https://github.com/SYSTRAN/faster-whisper)). The timestamped transcript
is stored in the database as JSON. A **local LLM** (via [Ollama](https://ollama.com)) then turns the transcript into study notes: a summary, sections with key
points, key definitions and practice multiple-choice questions, each linked to the moment in the lecture it came from.
Students can play lectures (even while they're still being transcribed), click any transcript line or note to jump
to that moment, take graded quizzes from a share link and see their marks straight away, and download notes and
transcript as PDF, Markdown or SRT subtitles. Teachers can share links, watch quiz results live, stop and continue
processing, and edit lecture details, thumbnails, transcripts, notes and quizzes.

**Everything runs on this machine.** Speech recognition and notes generation both use local models; no lecture
audio or text is sent to any cloud API.

**Stack:** FastAPI · Jinja2 templates · SQLAlchemy (SQLite by default) · session-cookie auth (bcrypt) · FFmpeg · faster-whisper · Ollama (local LLM) · yt-dlp · fpdf2

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (source .venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
copy .env.example .env          # optional, every setting has a default
ollama pull gemma4               # once, for notes (needs Ollama installed)
python db/seed.py                # optional: demo accounts (safe to re-run)
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 and log in with a demo account, or sign up, then add a lecture.

**Demo accounts** (from `db/seed.py`, password `test1234`, or `SEED_PASSWORD`):

| Role | Email |
|---|---|
| Teacher | `parul@parul.com` |
| Student | `parul@student.com` |

`python db/seed.py --reset-passwords` sets them back to the demo password.

- **Share links for students on other devices:** the server above only accepts connections from this computer.
  Start it with `--host 0.0.0.0` and open the app through the computer's network address (the Share dialog shows
  it, e.g. `http://192.168.1.20:8000`), or set `PUBLIC_BASE_URL`, then copy the share links.

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
  runtime; Node.js or Deno is picked up automatically if installed. Videos are saved at up to 720p, preferring
  H.264 so every browser can play them (YouTube's AV1 needs an extra codec in Edge). If imports stop working,
  update it: `pip install -U "yt-dlp[default]"`.
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

- Two in-process worker threads (`app/services/worker.py`): one for download/FFmpeg/Whisper, one for notes. Jobs run
  on the server, so they keep going whatever page the teacher has open. Interrupted jobs are re-queued on startup.
- A **tasks panel** on every teacher page shows running jobs with live progress and a **Stop** button, and pops up a
  message when a job finishes.
- **Stop / Continue:** any stage can be stopped (`app/services/jobs.py`). The lecture page shows a stage tracker and
  a **Continue – <stage>** button (or **Retry – <stage>** after a failure) that resumes from the unfinished stage;
  finished downloads and extracted audio are reused (`app/services/pipeline.py`).
- A **thumbnail** is taken from the video automatically; teachers can upload one or pick the current video frame.

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

## Sharing and quizzes

- **Share** on a lecture creates two public links: `/s/<token>` (video, notes, transcript) and `/s/<token>/quiz`.
  No account is needed. **New links** replaces the token; **Stop sharing** removes it.
- The quiz page only contains questions and options; the server grades submissions (`app/services/quiz.py`), so the
  answers can't be found in the page. Students see their score, the correct answers and explanations right away.
  Logged-in students are recorded under their account; others type their name.
- Teachers get a **Results** tab that updates every few seconds: submissions, average, per-student scores and the
  share of students who got each question right. Editing or regenerating the quiz keeps old results but marks them
  as taken on a previous version.

## Editing

Teachers can change, at any time: the lecture title, description and thumbnail (**Edit**); any transcript line
(**Edit** on the transcript panel; timestamps stay); and the summary, sections, key points, definitions and quiz in
the notes editor (`/lectures/{id}/notes/edit`), including writing notes by hand without AI. Link-imported videos that
won't play can be fetched again with **Edit → Re-download**: the new copy downloads in the background (with
progress and Stop), and the current video, stages, transcript and notes stay until it's complete.

Account settings (name, email, password, theme) are at `/settings`.

## Roles and access

| | Teacher | Student |
|---|---|---|
| Add, edit, stop/continue, share, delete lectures | own lectures | – |
| See lectures | own lectures (any status) | every lecture whose video is available |
| Transcript, notes, export PDF / Markdown / SRT | ✓ | ✓ |
| Generate, regenerate or edit notes and quiz | own lectures | – |
| Take quizzes | preview with answers | ✓ (also via share link, no account) |
| Quiz results | live, own lectures | own score |

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
| POST | `/lectures/{id}/retry` | continue/retry from the unfinished stage (teacher) |
| POST | `/lectures/{id}/details`, `/redownload`, `/delete` | edit details, fetch the video again, delete (teacher) |
| GET | `/lectures/{id}/thumbnail`, `/lectures/{id}/notes/edit` | thumbnail image; notes editor (teacher) |
| GET/POST | `/lectures/{id}/quiz`, `/lectures/{id}/quiz/r/{result}` | take the quiz (logged in); result page |
| GET/POST | `/s/{token}`, `/s/{token}/media`, `/s/{token}/quiz`, … | shared lecture and quiz (anyone with the link) |
| GET/POST | `/settings`, `/settings/profile`, `/settings/password` | account settings |
| POST | `/lectures/{id}/notes` | generate or regenerate notes (teacher) |
| GET | `/lectures/{id}/export.pdf`, `.md`, `.srt` | download notes + transcript |
| GET | `/api/lectures/{id}/status` | `{status, progress, error}` |
| GET | `/api/lectures/{id}/transcript` | full transcript JSON |
| GET | `/api/lectures/{id}/notes` | `{status, progress, error, model, content}` |
| GET/POST | `/api/lectures/{id}/share` | share links: `{"action": "enable" \| "reset" \| "disable"}` (teacher) |
| GET | `/api/lectures/{id}/results` | live quiz results (teacher) |
| POST | `/api/lectures/{id}/stop` | stop a job: `{"what": "processing" \| "notes"}` (teacher) |
| POST/PUT | `/api/lectures/{id}/thumbnail[/frame\|/auto]`, `/transcript`, `/notes` | thumbnails and editing (teacher) |
| GET | `/api/jobs` | running and recently finished jobs (teacher) |

## Project layout

```
app/
  main.py            FastAPI app, session middleware, startup (create tables, add new columns, resume jobs)
  config.py          settings from env / .env
  database.py        engine, session, Base, add_missing_columns()
  models.py          User, Lecture, Transcript, Notes, QuizAttempt
  security.py        bcrypt hashing, session login, require_user / require_teacher
  templating.py      Jinja setup, flash messages, timestamp filter
  routers/           auth.py (accounts, settings), lectures.py, api.py, share.py (share links, quizzes,
                     results, stop, jobs feed), edit.py (details, thumbnails, transcript and notes editing)
  services/          media.py (ffmpeg, frames), download.py (yt-dlp), transcription.py (whisper),
                     notes.py (local LLM), quiz.py (grading, results), export.py (PDF/Markdown/SRT),
                     pipeline.py (stages, continue), jobs.py (stop requests), worker.py (background jobs)
  templates/, static/ (style.css, fonts/)
data/                created at runtime: app.db, uploads/, audio/, thumbnails/, models/
db/                  seed.py (demo accounts), schema.sql, schema.postgres.sql (reference schemas)
docs/                project-overview.html (full project documentation)
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
