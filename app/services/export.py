"""Export a lecture's notes and transcript as Markdown, PDF or SRT subtitles."""

import logging
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from fpdf import FPDF
from fpdf.enums import MethodReturnValue

from app.config import settings
from app.models import Lecture
from app.templating import format_timestamp

log = logging.getLogger(__name__)
# fontTools warns about every font table it can't subset (e.g. "MERG"); harmless noise.
logging.getLogger("fontTools.subset").setLevel(logging.ERROR)

LETTERS = "ABCDEFGHIJ"


def safe_filename(title: str) -> str:
    # Keep letters in any script (including combining marks such as Hindi vowel signs);
    # drop only characters that are unsafe in file names.
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f.]+', " ", title)
    name = re.sub(r"\s+", "_", name.strip())
    return name[:80].strip("_") or "lecture"


def content_disposition(filename: str) -> str:
    """Attachment header that also works for non-ASCII titles (RFC 6266 / 5987)."""
    stem, _, ext = filename.rpartition(".")
    ascii_stem = re.sub(r"_+", "_", stem.encode("ascii", "ignore").decode()).strip("_") or "lecture"
    ascii_name = f"{ascii_stem}.{ext}"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def _notes_content(lecture: Lecture) -> dict | None:
    return lecture.notes.content if lecture.notes and lecture.notes.content else None


def transcript_paragraphs(segments: list[dict], min_words: int = 45, max_seconds: float = 60) -> list[tuple[float, str]]:
    """Merge Whisper's short segments into readable paragraphs: [(start_seconds, text), ...].

    A paragraph ends at the end of a sentence once it has enough words, or after max_seconds regardless.
    """
    paragraphs, start, parts, words = [], None, [], 0
    for seg in segments:
        text = seg["text"].strip()
        if not text:
            continue
        if start is None:
            start = seg["start"]
        parts.append(text)
        words += len(text.split())
        sentence_end = text[-1] in ".?!。？！।"
        if (sentence_end and words >= min_words) or seg["end"] - start >= max_seconds:
            paragraphs.append((start, " ".join(parts)))
            start, parts, words = None, [], 0
    if parts:
        paragraphs.append((start, " ".join(parts)))
    return paragraphs


def _meta(lecture: Lecture) -> list[str]:
    meta = [lecture.teacher.name, lecture.created_at.strftime("%d %b %Y")]
    if lecture.duration:
        meta.append(f"{format_timestamp(lecture.duration)} long")
    return meta


# ---------------------------------------------------------------- Markdown

def to_markdown(lecture: Lecture) -> str:
    out = [f"# {lecture.title}", "", " · ".join(_meta(lecture)), ""]
    if lecture.description:
        out += [lecture.description, ""]
    if lecture.source_url:
        out += [f"Source: <{lecture.source_url}>", ""]

    notes = _notes_content(lecture)
    if notes:
        out += ["## Summary", "", notes["summary"], ""]
        for s in notes["sections"]:
            out += [f"## {s['title']} `{format_timestamp(s['start'])}`", "", s["summary"], ""]
            out += [f"- {k['text']} `{format_timestamp(k['start'])}`" for k in s["key_points"]]
            out.append("")
        if notes["definitions"]:
            out += ["## Key definitions", "", "| Term | Definition |", "|---|---|"]
            out += [f"| **{d['term']}** | {d['definition']} |" for d in notes["definitions"]]
            out.append("")
        if notes["mcqs"]:
            out += ["## Practice questions", ""]
            for i, q in enumerate(notes["mcqs"], 1):
                out += [f"**{i}. {q['question']}**", ""]
                out += [f"- {LETTERS[j]}. {opt}" for j, opt in enumerate(q["options"])]
                out.append("")
            out += ["### Answer key", ""]
            for i, q in enumerate(notes["mcqs"], 1):
                out.append(f"{i}. **{LETTERS[q['answer_index']]}**: {q['explanation']} `{format_timestamp(q['start'])}`")
            out.append("")

    if lecture.transcript:
        out += ["## Transcript", ""]
        for start, text in transcript_paragraphs(lecture.transcript.segments):
            out += [f"`{format_timestamp(start)}` {text}", ""]
    return "\n".join(out)


# ---------------------------------------------------------------- SRT subtitles

def _srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(lecture: Lecture) -> str:
    blocks = [
        f"{i}\n{_srt_time(seg['start'])} --> {_srt_time(seg['end'])}\n{seg['text'].strip()}\n"
        for i, seg in enumerate(lecture.transcript.segments, 1)
    ]
    return "\n".join(blocks)


# ---------------------------------------------------------------- PDF

# Unicode fonts to try (regular, bold). fpdf2's built-in fonts only cover Latin-1.
_FONT_CANDIDATES = [
    ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
]

# Extra fonts for scripts the main font lacks (Indic, CJK, Korean, symbols). Used per missing glyph.
_FALLBACK_CANDIDATES = [
    "C:/Windows/Fonts/Nirmala.ttc",  # Devanagari, Bengali, Tamil, Telugu, ... (Windows 10+)
    "C:/Windows/Fonts/mangal.ttf",
    "C:/Windows/Fonts/msyh.ttc",  # Chinese
    "C:/Windows/Fonts/YuGothR.ttc",  # Japanese
    "C:/Windows/Fonts/malgun.ttf",  # Korean
    "C:/Windows/Fonts/seguisym.ttf",  # symbols
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/Supplemental/Devanagari Sangam MN.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]


@lru_cache(maxsize=1)
def _find_fonts() -> tuple[str, str | None, list[str]] | None:
    """(regular, bold, fallbacks) or None to use the Latin-1 built-in font."""
    fallbacks = [f for f in _FALLBACK_CANDIDATES if Path(f).exists()]
    if settings.pdf_font:
        if Path(settings.pdf_font).exists():
            return settings.pdf_font, None, fallbacks
        log.warning("PDF_FONT %s does not exist; auto-detecting a font instead.", settings.pdf_font)
    for regular, bold in _FONT_CANDIDATES:
        if Path(regular).exists():
            return regular, bold if bold and Path(bold).exists() else None, fallbacks
    log.warning("No Unicode font found for PDF export; non-Latin characters will be replaced. Set PDF_FONT.")
    return None


def _has_shaping() -> bool:
    try:
        import uharfbuzz  # noqa: F401
        return True
    except ImportError:
        return False


# Colours (RGB) - match the web app
INK = (28, 32, 48)
BODY = (55, 60, 75)
MUTED = (120, 126, 142)
ACCENT = (79, 70, 229)
ACCENT_SOFT = (238, 240, 255)
RULE = (226, 229, 238)
OK = (21, 128, 61)


class _PDF(FPDF):
    def __init__(self, lecture: Lecture):
        super().__init__(format="A4")
        self.lecture_title = lecture.title
        self.set_margins(20, 22, 20)
        self.set_auto_page_break(True, margin=20)
        self.set_title(lecture.title)
        self.set_author(lecture.teacher.name)
        self.set_creator(settings.app_name)
        fonts = _find_fonts()
        if fonts:
            regular, bold, fallbacks = fonts
            self.add_font("body", "", regular)
            self.add_font("body", "B", bold or regular)
            names = []
            for i, path in enumerate(fallbacks):
                try:
                    self.add_font(f"fb{i}", "", path)
                    names.append(f"fb{i}")
                except Exception as exc:  # noqa: BLE001 - a broken system font shouldn't break export
                    log.warning("Skipping fallback font %s: %s", path, exc)
            if names:
                self.set_fallback_fonts(names, exact_match=False)
            if _has_shaping():  # correct ligatures/conjuncts for Indic and Arabic scripts
                self.set_text_shaping(True)
            self.family_name, self.unicode = "body", True
        else:
            self.family_name, self.unicode = "helvetica", False

    @property
    def content_w(self) -> float:
        return self.w - self.l_margin - self.r_margin

    def clean(self, text: str) -> str:
        return text if self.unicode else text.encode("latin-1", "replace").decode("latin-1")

    def font(self, size: float, bold: bool = False, color=BODY):
        self.set_font(self.family_name, "B" if bold else "", size)
        self.set_text_color(*color)

    # Running header (from page 2) and footer
    def header(self):
        if self.page_no() == 1:
            return
        self.font(8, color=MUTED)
        self.set_y(11)
        self.cell(0, 5, self.clean(self.lecture_title), align="L")
        self.set_draw_color(*RULE)
        self.set_line_width(0.2)
        self.line(self.l_margin, 17, self.w - self.r_margin, 17)
        self.set_y(self.t_margin)

    def footer(self):
        self.set_y(-13)
        self.font(8, color=MUTED)
        self.cell(self.content_w / 2, 5, self.clean(settings.app_name), align="L")
        self.cell(self.content_w / 2, 5, f"Page {self.page_no()} of {{nb}}", align="R")

    def ensure_space(self, height: float):
        if self.get_y() + height > self.h - self.b_margin:
            self.add_page()

    def text_height(self, w: float, line_h: float, text: str) -> float:
        return self.multi_cell(w, line_h, self.clean(text), dry_run=True, output=MethodReturnValue.HEIGHT)

    def para(self, text: str, size: float = 10.5, color=BODY, bold: bool = False, line_h: float = 5.4, indent: float = 0):
        self.font(size, bold, color)
        self.set_x(self.l_margin + indent)
        self.multi_cell(self.content_w - indent, line_h, self.clean(text), align="L", new_x="LMARGIN", new_y="NEXT")

    def section_heading(self, text: str, stamp: str | None = None, size: float = 14, gap: float = 7):
        self.ensure_space(26)
        self.ln(gap)
        self.font(size, True, INK)
        stamp_w = 18 if stamp else 0
        y = self.get_y()
        self.multi_cell(self.content_w - stamp_w, size * 0.48, self.clean(text), align="L", new_x="LMARGIN", new_y="NEXT")
        end_y = self.get_y()
        if stamp:
            self.set_xy(self.w - self.r_margin - stamp_w, y + 0.6)
            self.font(9, color=ACCENT)
            self.cell(stamp_w, 5, stamp, align="R")
            self.set_y(end_y)
        self.ln(1.5)

    def stamped(self, stamp: str, text: str, bullet: bool = False):
        """'03:41  Text' with the timestamp in a narrow accent-coloured column."""
        col = 14
        self.font(10.5)
        h = self.text_height(self.content_w - col, 5.3, text)
        self.ensure_space(h + 1)
        y = self.get_y()
        self.font(8.5, color=ACCENT)
        self.set_xy(self.l_margin, y + 0.4)
        self.cell(col, 5, stamp)
        self.set_xy(self.l_margin + col, y)
        self.font(10.5)
        self.multi_cell(self.content_w - col, 5.3, self.clean(text), align="L", new_x="LMARGIN", new_y="NEXT")
        self.ln(1.2)


def to_pdf(lecture: Lecture) -> bytes:
    pdf = _PDF(lecture)
    pdf.add_page()
    W = pdf.content_w

    # ---- Title block
    pdf.font(8.5, True, ACCENT)
    pdf.cell(0, 5, "LECTURE NOTES", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    pdf.font(22, True, INK)
    pdf.multi_cell(W, 10, pdf.clean(lecture.title), align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    pdf.para("   ·   ".join(_meta(lecture)), size=10, color=MUTED)
    if lecture.source_url:
        pdf.para(lecture.source_url, size=8.5, color=MUTED, line_h=4.6)
    pdf.ln(3)
    pdf.set_draw_color(*ACCENT)
    pdf.set_line_width(0.6)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + 24, pdf.get_y())
    pdf.ln(5)
    if lecture.description:
        pdf.para(lecture.description, color=BODY)
        pdf.ln(2)

    notes = _notes_content(lecture)
    if notes:
        # ---- Summary box
        pad = 5
        pdf.font(10.5)
        body_h = pdf.text_height(W - 2 * pad, 5.6, notes["summary"])
        box_h = body_h + 2 * pad + 6
        pdf.ensure_space(box_h)
        y = pdf.get_y()
        pdf.set_fill_color(*ACCENT_SOFT)
        pdf.rect(pdf.l_margin, y, W, box_h, style="F")
        pdf.set_fill_color(*ACCENT)
        pdf.rect(pdf.l_margin, y, 1.2, box_h, style="F")
        pdf.set_xy(pdf.l_margin + pad, y + pad - 0.5)
        pdf.font(8.5, True, ACCENT)
        pdf.cell(0, 5, "SUMMARY", new_x="LMARGIN", new_y="NEXT")
        pdf.set_xy(pdf.l_margin + pad, y + pad + 5.5)
        pdf.font(10.5, color=INK)
        pdf.multi_cell(W - 2 * pad, 5.6, pdf.clean(notes["summary"]), align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.set_y(y + box_h + 2)

        # ---- Contents
        if len(notes["sections"]) > 1:
            pdf.section_heading("Contents", size=12, gap=6)
            for i, s in enumerate(notes["sections"], 1):
                pdf.font(10.5)
                pdf.cell(8, 6, f"{i}.")
                pdf.cell(W - 8 - 16, 6, pdf.clean(s["title"]))
                pdf.font(9, color=ACCENT)
                pdf.cell(16, 6, format_timestamp(s["start"]), align="R", new_x="LMARGIN", new_y="NEXT")

        # ---- Sections
        for i, s in enumerate(notes["sections"], 1):
            pdf.section_heading(f"{i}. {s['title']}", format_timestamp(s["start"]))
            pdf.para(s["summary"], color=BODY)
            pdf.ln(2.5)
            for k in s["key_points"]:
                pdf.stamped(format_timestamp(k["start"]), k["text"])

        # ---- Definitions table
        if notes["definitions"]:
            pdf.section_heading("Key definitions")
            term_w, pad = 48, 2.5
            pdf.set_draw_color(*RULE)
            pdf.set_line_width(0.2)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + W, pdf.get_y())
            for d in notes["definitions"]:
                pdf.font(10.5, True)
                th = pdf.text_height(term_w - 3, 5.2, d["term"])
                pdf.font(10.5)
                dh = pdf.text_height(W - term_w, 5.2, d["definition"]) + 5
                row_h = max(th, dh) + 2 * pad
                pdf.ensure_space(row_h)
                y = pdf.get_y()
                pdf.set_xy(pdf.l_margin, y + pad)
                pdf.font(10.5, True, INK)
                pdf.multi_cell(term_w - 3, 5.2, pdf.clean(d["term"]), align="L")
                pdf.set_xy(pdf.l_margin + term_w, y + pad)
                pdf.font(10.5)
                pdf.multi_cell(W - term_w, 5.2, pdf.clean(d["definition"]), align="L", new_x="LEFT", new_y="NEXT")
                pdf.set_x(pdf.l_margin + term_w)
                pdf.font(8.5, color=ACCENT)
                pdf.cell(20, 5, format_timestamp(d["start"]))
                pdf.set_y(y + row_h)
                pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + W, pdf.get_y())

        # ---- Practice questions + answer key
        if notes["mcqs"]:
            pdf.section_heading("Practice questions")
            for i, q in enumerate(notes["mcqs"], 1):
                pdf.font(10.5, True)
                need = pdf.text_height(W - 7, 5.4, q["question"]) + 6.4 * len(q["options"]) + 4
                pdf.ensure_space(need)
                y = pdf.get_y()
                pdf.font(10.5, True, ACCENT)
                pdf.cell(7, 5.4, f"{i}.")
                pdf.set_xy(pdf.l_margin + 7, y)
                pdf.font(10.5, True, INK)
                pdf.multi_cell(W - 7, 5.4, pdf.clean(q["question"]), align="L", new_x="LMARGIN", new_y="NEXT")
                pdf.ln(1)
                for j, opt in enumerate(q["options"]):
                    pdf.set_x(pdf.l_margin + 7)
                    pdf.font(10, True, MUTED)
                    pdf.cell(6, 5.6, LETTERS[j])
                    pdf.font(10.5)
                    pdf.multi_cell(W - 13, 5.6, pdf.clean(opt), align="L", new_x="LMARGIN", new_y="NEXT")
                pdf.ln(3.5)

            pdf.section_heading("Answer key", size=12, gap=3)
            for i, q in enumerate(notes["mcqs"], 1):
                text = f"{q['explanation']}  ({format_timestamp(q['start'])})"
                pdf.font(10)
                h = pdf.text_height(W - 16, 5.1, text)
                pdf.ensure_space(h + 2)
                y = pdf.get_y()
                pdf.font(10, True, OK)
                pdf.cell(16, 5.1, f"{i}.  {LETTERS[q['answer_index']]}")
                pdf.set_xy(pdf.l_margin + 16, y)
                pdf.font(10)
                pdf.multi_cell(W - 16, 5.1, pdf.clean(text), align="L", new_x="LMARGIN", new_y="NEXT")
                pdf.ln(1.5)

    # ---- Transcript, grouped into paragraphs
    if lecture.transcript and lecture.transcript.segments:
        pdf.add_page()
        pdf.section_heading("Transcript", gap=0)
        pdf.ln(1)
        for start, text in transcript_paragraphs(lecture.transcript.segments):
            pdf.stamped(format_timestamp(start), text)
            pdf.ln(1.5)

    return bytes(pdf.output())
