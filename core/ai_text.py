"""Personalised email text.

Uses the Gemini REST API when GEMINI_API_KEY is set and falls back to a
carefully written template otherwise (or on any API error), so the email
always goes out.
"""
from __future__ import annotations

import re

import pandas as pd

from .config import Settings
from .risk import CRITICAL, WARNING

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _fmt_pct(value: float) -> str:
    return f"{value:.1f}%"


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def student_facts(student, subjects: pd.DataFrame, threshold: float) -> dict:
    """Plain facts about one student, shared by the template, the AI prompt and the call script."""
    crit = subjects[subjects["status"] == CRITICAL].sort_values("attendance_pct")
    warn = subjects[subjects["status"] == WARNING].sort_values("attendance_pct")
    weak = subjects[subjects["weak_marks"]]
    falling = subjects[subjects["falling_marks"]]
    return {
        "name": str(student["name"]),
        "first_name": str(student["name"]).split()[0],
        "adviser": str(student.get("adviser_name", "your faculty adviser")),
        "threshold": threshold,
        "critical": [(r.subject, r.attendance_pct, int(r.recovery_classes)) for r in crit.itertuples()],
        "warning": [(r.subject, r.attendance_pct) for r in warn.itertuples()],
        "weak": [(r.subject, r.latest_mark) for r in weak.itertuples()],
        "falling": [(r.subject, r.previous_mark, r.latest_mark) for r in falling.itertuples()],
    }


def template_message(facts: dict) -> str:
    """A specific, encouraging two-paragraph message built from the facts."""
    t = f"{facts['threshold']:g}%"
    parts: list[str] = []
    if facts["critical"]:
        listed = _join([f"{s} ({_fmt_pct(p)})" for s, p, _ in facts["critical"]])
        plans = _join([f"the next {n} {s} classes" for s, _, n in facts["critical"]])
        parts.append(
            f"Your attendance is below the required {t} in {listed}. "
            f"To get back above {t}, you need to attend {plans} without missing any.")
    if facts["warning"]:
        listed = _join([f"{s} ({_fmt_pct(p)})" for s, p in facts["warning"]])
        parts.append(
            f"You are only just above the limit in {listed}. "
            f"Missing even one or two more classes could take you below {t}.")
    for subject, prev, latest in facts["falling"]:
        if pd.notna(prev):
            parts.append(f"Your latest {subject} test score was {latest:g}, down from {prev:g} in the previous test.")
    falling_subjects = {s for s, _, _ in facts["falling"]}
    weak_only = [(s, m) for s, m in facts["weak"] if s not in falling_subjects]
    if weak_only:
        listed = _join([f"{s} ({m:g})" for s, m in weak_only])
        parts.append(f"Your latest test score is below 40 in {listed}.")
    if not parts:
        parts.append("Your attendance and marks are on track. Keep it up.")

    closing = (
        "Every class from now on counts. Please book a short meeting with your subject "
        f"teacher using the link below so you can plan the next few weeks together. "
        f"{facts['adviser']}, your faculty adviser, has also been informed and is ready to help."
    )
    return " ".join(parts) + "\n\n" + closing


def _prompt(facts: dict) -> str:
    lines = [f"Student first name: {facts['first_name']}", f"Required attendance: {facts['threshold']:g}%"]
    for s, p, n in facts["critical"]:
        lines.append(f"- {s}: attendance {p:.1f}% (below requirement); must attend the next {n} classes in a row to recover")
    for s, p in facts["warning"]:
        lines.append(f"- {s}: attendance {p:.1f}% (just above requirement, at risk)")
    for s, prev, latest in facts["falling"]:
        lines.append(f"- {s}: latest test {latest:g}, previous {prev:g} (falling)")
    for s, m in facts["weak"]:
        lines.append(f"- {s}: latest test {m:g} out of 100 (below 40, weak)")
    lines.append(f"Faculty adviser: {facts['adviser']}")
    facts_block = "\n".join(lines)
    return (
        "You are a caring faculty mentor at an engineering college in India. Write the body of a short "
        "email to a student about their attendance and marks. Rules: two short paragraphs, at most 110 "
        "words in total, plain text only (no markdown, no bullet points), no greeting line and no sign-off "
        "(those are added separately). Be warm, direct and encouraging, never threatening. Use every number "
        "exactly as given and do not invent any facts. End by asking them to book a meeting with their "
        "subject teacher using the link below the message.\n\nFacts:\n" + facts_block
    )


def _gemini(settings: Settings, prompt: str) -> str:
    import requests

    resp = requests.post(
        GEMINI_URL.format(model=settings.gemini_model),
        headers={"x-goog-api-key": settings.gemini_api_key or "", "Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.6, "maxOutputTokens": 1024},
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    parts = data["candidates"][0]["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts).strip()
    text = re.sub(r"[*_#`]+", "", text)  # strip stray markdown
    if len(text) < 40:
        raise ValueError("Gemini returned an empty or truncated message.")
    return text


def personalised_message(settings: Settings, facts: dict, use_ai: bool = True) -> tuple[str, str]:
    """Return (message text, source) where source is 'gemini' or 'template'."""
    if use_ai and settings.ai_enabled and (facts["critical"] or facts["warning"] or facts["weak"] or facts["falling"]):
        try:
            return _gemini(settings, _prompt(facts)), "gemini"
        except Exception:
            pass
    return template_message(facts), "template"
