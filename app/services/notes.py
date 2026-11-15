"""Study-notes generation with a local LLM (Ollama), fully on this machine.

No lecture content leaves the computer: the app talks to the Ollama server on localhost, and cloud
models are refused.

Long lectures don't fit comfortably in a small model's context, so the transcript is split into
parts of about NOTES_CHUNK_WORDS words (on segment boundaries):

    part 1 ─► sections, key points, definitions, questions
    part 2 ─► ...                                            ─► merge ─► overall summary
    part n ─► ...

Each part is sent as numbered segments ("[12] text"). The model's output is constrained to a JSON
schema and points back at segment numbers, which we turn into timestamps so every note can jump
to the moment it came from.
"""

import json
import logging
import re
from collections.abc import Callable

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.config import settings

log = logging.getLogger(__name__)


# ---- Output schemas (the model's output is constrained to these) ----

class KeyPoint(BaseModel):
    text: str
    segment: int


class Section(BaseModel):
    title: str
    segment: int
    summary: str
    key_points: list[KeyPoint]


class Definition(BaseModel):
    term: str
    definition: str
    segment: int


class MCQ(BaseModel):
    question: str
    options: list[str]
    answer_index: int
    explanation: str
    segment: int


class PartNotes(BaseModel):
    sections: list[Section]
    definitions: list[Definition]
    mcqs: list[MCQ]


class Summary(BaseModel):
    summary: str


PART_PROMPT = """You are making study notes for students from part {part} of {parts} of a lecture transcript.

Lecture title: {title}
{description}{previous}
Rules:
- Use only what the lecturer says in this part. Don't add outside material.
- The transcript comes from speech recognition: fix obviously misheard words from context.
- Write in {language}. Use plain text only: no Markdown, no LaTeX, no emoji.
- Each transcript line starts with its segment number in square brackets. Set every `segment` field
  to the number of the line where that idea is first said.

Produce:
- sections: the topics in this part, in order (usually 1-3). Each has a short title, a 2-3 sentence
  summary, and 2-5 key points.
- definitions: important terms the lecturer defines or explains in this part (can be empty).
- mcqs: {mcqs} multiple-choice questions that test understanding of this part. Each has exactly 4
  options, answer_index is the 0-based index of the correct option, and the explanation says why.

<transcript>
{transcript}
</transcript>"""

SUMMARY_PROMPT = """Below are the section notes of a lecture titled "{title}".
Write one paragraph (3-6 sentences) in {language} that gives students the big picture of the whole
lecture. Plain text only: no Markdown, no LaTeX.

{sections}"""


class NotesError(Exception):
    pass


LANGUAGE_NAMES = {
    "en": "English", "hi": "Hindi", "bn": "Bengali", "ta": "Tamil", "te": "Telugu", "mr": "Marathi",
    "gu": "Gujarati", "kn": "Kannada", "ml": "Malayalam", "pa": "Punjabi", "ur": "Urdu", "es": "Spanish",
    "fr": "French", "de": "German", "pt": "Portuguese", "it": "Italian", "ru": "Russian", "zh": "Chinese",
    "ja": "Japanese", "ko": "Korean", "ar": "Arabic",
}


# ---- Ollama client ----

def _ollama(path: str, payload: dict | None = None, timeout: float = 30) -> dict:
    url = settings.ollama_url.rstrip("/") + path
    try:
        if payload is None:
            r = httpx.get(url, timeout=timeout)
        else:
            r = httpx.post(url, json=payload, timeout=timeout)
    except httpx.ConnectError as exc:
        raise NotesError(
            f"Can't reach Ollama at {settings.ollama_url}. Start the Ollama app (or run `ollama serve`) and try again."
        ) from exc
    except httpx.TimeoutException as exc:
        raise NotesError("The local model took too long to respond. Try a smaller NOTES_MODEL.") from exc
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200:
        raise NotesError(f"Ollama error ({r.status_code}): {data.get('error') or r.text[:300]}")
    return data


def check_model() -> None:
    """Make sure the configured model is downloaded and runs locally (not an Ollama cloud model)."""
    models = _ollama("/api/tags").get("models", [])
    wanted = settings.notes_model if ":" in settings.notes_model else settings.notes_model + ":latest"
    match = next((m for m in models if m.get("name") in (settings.notes_model, wanted)), None)
    if match is None:
        raise NotesError(
            f"The model '{settings.notes_model}' isn't downloaded. Run `ollama pull {settings.notes_model}` "
            "in a terminal, then try again."
        )
    if match.get("remote_host") or match.get("remote_model"):
        raise NotesError(
            f"'{settings.notes_model}' is an Ollama cloud model, which would send the lecture to the internet. "
            "Set NOTES_MODEL to a local model (for example gemma4 or llama3.1:8b)."
        )


def _chat_stream(payload: dict, check_stop: Callable[[], None] | None) -> tuple[str, str | None]:
    """Streamed /api/chat call. Returns (content, done_reason). Streaming lets a stop request
    interrupt the model mid-answer instead of waiting for the whole part to finish."""
    url = settings.ollama_url.rstrip("/") + "/api/chat"
    content, done_reason = [], None
    try:
        with httpx.stream("POST", url, json={**payload, "stream": True},
                          timeout=httpx.Timeout(settings.notes_timeout, connect=10)) as r:
            if r.status_code != 200:
                r.read()
                try:
                    msg = r.json().get("error")
                except ValueError:
                    msg = r.text[:300]
                raise NotesError(f"Ollama error ({r.status_code}): {msg}")
            for line in r.iter_lines():
                if check_stop:
                    check_stop()
                if not line:
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise NotesError(f"Ollama error: {chunk['error']}")
                content.append(chunk.get("message", {}).get("content", ""))
                if chunk.get("done"):
                    done_reason = chunk.get("done_reason")
    except httpx.ConnectError as exc:
        raise NotesError(
            f"Can't reach Ollama at {settings.ollama_url}. Start the Ollama app (or run `ollama serve`) and try again."
        ) from exc
    except httpx.TimeoutException as exc:
        raise NotesError("The local model took too long to respond. Try a smaller NOTES_MODEL.") from exc
    return "".join(content), done_reason


def _generate(prompt: str, schema: type[BaseModel], check_stop: Callable[[], None] | None = None) -> BaseModel:
    """One schema-constrained chat call. Retries once if the output doesn't validate."""
    last_error = None
    for attempt in range(2):
        text, done_reason = _chat_stream(
            {
                "model": settings.notes_model,
                "messages": [{"role": "user", "content": prompt}],
                "format": schema.model_json_schema(),
                "think": False,
                "keep_alive": "10m",
                "options": {"num_ctx": settings.notes_num_ctx, "temperature": 0.2 if attempt == 0 else 0.5},
            },
            check_stop,
        )
        if done_reason == "length":
            last_error = "the model's answer was cut off (increase NOTES_NUM_CTX or lower NOTES_CHUNK_WORDS)"
            continue
        try:
            return schema.model_validate_json(text)
        except (ValidationError, json.JSONDecodeError) as exc:
            last_error = f"invalid output: {exc}"
            log.warning("Notes model returned invalid output (attempt %d): %s", attempt + 1, exc)
    raise NotesError(f"The local model failed to produce notes: {last_error}")


# ---- Pipeline ----

def _split_parts(segments: list[dict], max_words: int) -> list[list[int]]:
    """Group segment indexes into parts of roughly max_words words."""
    parts, current, words = [], [], 0
    for i, seg in enumerate(segments):
        n = len(seg["text"].split())
        if current and words + n > max_words:
            parts.append(current)
            current, words = [], 0
        current.append(i)
        words += n
    if current:
        parts.append(current)
    # Fold a tiny last part into the previous one so it doesn't become its own thin section.
    if len(parts) > 1 and sum(len(segments[i]["text"].split()) for i in parts[-1]) < max_words // 4:
        parts[-2].extend(parts.pop())
    return parts


_LATEX = {r"\rightarrow": "→", r"\to": "→", r"\leftarrow": "←", r"\times": "×", r"\le": "≤", r"\ge": "≥"}


def _clean(text: str) -> str:
    """Strip stray Markdown/LaTeX that small models sometimes emit despite instructions."""
    text = re.sub(r"\$\s*(\\[a-zA-Z]+)\s*\$", lambda m: _LATEX.get(m.group(1), ""), text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def generate_notes(
    title: str,
    description: str,
    segments: list[dict],
    language: str | None = None,
    on_progress: Callable[[float], None] | None = None,
    check_stop: Callable[[], None] | None = None,
) -> dict:
    """Return the notes content dict (with timestamps) for a transcript."""
    if not segments:
        raise NotesError("The transcript is empty, so there is nothing to make notes from.")
    check_model()

    language_name = LANGUAGE_NAMES.get(language or "", "the same language as the transcript")
    parts = _split_parts(segments, settings.notes_chunk_words)
    total_steps = len(parts) + 1
    mcqs_per_part = max(1, min(3, round(8 / len(parts))))
    log.info("Generating notes with %s: %d segments in %d part(s)", settings.notes_model, len(segments), len(parts))

    def start(segment: int, allowed: list[int]) -> float:
        # Clamp to this part's segments: models occasionally cite a number outside the part.
        idx = min(max(segment, allowed[0]), allowed[-1])
        return float(segments[idx]["start"])

    sections, definitions, mcqs, seen_terms = [], [], [], set()
    for n, idxs in enumerate(parts, 1):
        previous = ""
        if sections:
            titles = "; ".join(s["title"] for s in sections[-6:])
            previous = f"Topics already covered in earlier parts (don't repeat them): {titles}\n"
        prompt = PART_PROMPT.format(
            part=n, parts=len(parts), title=title, language=language_name, mcqs=mcqs_per_part,
            description=f"Description: {description}\n" if description else "",
            previous=previous,
            transcript="\n".join(f"[{i}] {segments[i]['text'].strip()}" for i in idxs),
        )
        part: PartNotes = _generate(prompt, PartNotes, check_stop)

        for s in part.sections:
            sections.append({
                "title": _clean(s.title),
                "start": start(s.segment, idxs),
                "summary": _clean(s.summary),
                "key_points": [{"text": _clean(k.text), "start": start(k.segment, idxs)} for k in s.key_points],
            })
        for d in part.definitions:
            key = d.term.strip().lower()
            if key and key not in seen_terms:
                seen_terms.add(key)
                definitions.append({"term": _clean(d.term), "definition": _clean(d.definition),
                                    "start": start(d.segment, idxs)})
        for q in part.mcqs:
            options = [_clean(o) for o in q.options]
            if len(options) < 2 or not 0 <= q.answer_index < len(options):
                continue  # drop malformed questions rather than show a wrong answer
            mcqs.append({"question": _clean(q.question), "options": options, "answer_index": q.answer_index,
                         "explanation": _clean(q.explanation), "start": start(q.segment, idxs)})
        if on_progress:
            on_progress(n / total_steps)

    if not sections:
        raise NotesError("The local model returned no notes for this lecture.")

    sections.sort(key=lambda s: s["start"])
    outline = "\n".join(f"- {s['title']}: {s['summary']}" for s in sections)
    summary: Summary = _generate(
        SUMMARY_PROMPT.format(title=title, language=language_name, sections=outline), Summary, check_stop
    )
    if on_progress:
        on_progress(1.0)

    return {
        "summary": _clean(summary.summary),
        "sections": sections,
        "definitions": definitions,
        "mcqs": mcqs[:10],
    }
