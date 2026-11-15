"""Graded quizzes: the questions come from the lecture's notes; answers are checked on the server
(the quiz page never contains the correct answers)."""

import hashlib
import json
import secrets
from statistics import mean

from app.models import Lecture, QuizAttempt, User


def questions(lecture: Lecture) -> list[dict]:
    notes = lecture.notes
    if notes is None or not notes.content:
        return []
    return notes.content.get("mcqs") or []


def version(lecture: Lecture) -> str:
    """Fingerprint of the current questions; changes only when the quiz itself changes."""
    qs = questions(lecture)
    return hashlib.sha1(json.dumps(qs, sort_keys=True).encode()).hexdigest()[:16] if qs else ""


def public_questions(lecture: Lecture) -> list[dict]:
    """Questions without answers or explanations, safe to put in the page."""
    return [{"question": q["question"], "options": q["options"]} for q in questions(lecture)]


def grade(lecture: Lecture, form: dict, name: str, user: User | None) -> QuizAttempt:
    """Grade submitted form fields q0..qN (option indexes) and return an unsaved attempt."""
    qs = questions(lecture)
    answers, score = [], 0
    for i, q in enumerate(qs):
        raw = form.get(f"q{i}")
        try:
            chosen = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            chosen = None
        if chosen is not None and not 0 <= chosen < len(q["options"]):
            chosen = None
        correct = q["answer_index"]
        score += chosen == correct
        answers.append({"chosen": chosen, "correct": correct})
    return QuizAttempt(
        public_id=secrets.token_urlsafe(16),
        lecture_id=lecture.id,
        user_id=user.id if user else None,
        name=name[:120],
        score=score,
        total=len(qs),
        answers=answers,
        quiz_version=version(lecture),
    )


def review(lecture: Lecture, attempt: QuizAttempt) -> list[dict]:
    """Per-question review for the results page (only shown after submitting)."""
    qs = questions(lecture)
    if attempt.quiz_version != version(lecture) or len(qs) != len(attempt.answers):
        return []  # the quiz was regenerated since; the old questions are gone
    return [
        {**q, "chosen": a["chosen"], "ok": a["chosen"] == a["correct"]}
        for q, a in zip(qs, attempt.answers)
    ]


def results(lecture: Lecture) -> dict:
    """Live results for the teacher: submissions on the current quiz, average, per-question accuracy."""
    qs = questions(lecture)
    current = version(lecture)
    attempts = [a for a in lecture.attempts if a.quiz_version == current]
    per_question = []
    for i, q in enumerate(qs):
        answered = [a.answers[i] for a in attempts if i < len(a.answers)]
        right = sum(1 for x in answered if x["chosen"] == x["correct"])
        per_question.append({
            "question": q["question"],
            "correct_pct": round(100 * right / len(answered)) if answered else None,
        })
    return {
        "total_questions": len(qs),
        "count": len(attempts),
        "average_pct": round(mean(a.percent for a in attempts)) if attempts else None,
        "attempts": [
            {
                "name": a.name,
                "account": a.user_id is not None,
                "score": a.score,
                "total": a.total,
                "percent": a.percent,
                "submitted_at": a.created_at.isoformat(),
            }
            for a in attempts
        ],
        "per_question": per_question,
        "older_attempts": len(lecture.attempts) - len(attempts),
    }
