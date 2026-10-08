"""Automated voice calls through Twilio, using inline TwiML <Say>.

No webhook server is needed: the spoken message is sent inline with the call
request. Without Twilio credentials every call is logged as "DEMO - would call".
"""
from __future__ import annotations

import re
from xml.sax.saxutils import escape

from .config import Settings
from .notify import log_entry


def normalize_phone(phone, country_code: str = "+91") -> str | None:
    """Return an E.164 number (e.g. +919876543210) or None if it cannot be parsed."""
    if phone is None:
        return None
    raw = str(phone).strip()
    if not raw or raw.lower() == "nan":
        return None
    digits = re.sub(r"\D", "", raw)
    cc_digits = re.sub(r"\D", "", country_code)
    if raw.startswith("+"):
        number = "+" + digits
    elif raw.startswith("00"):
        number = "+" + digits[2:]
    elif len(digits) == 10:
        number = f"+{cc_digits}{digits}"
    elif len(digits) == 11 and digits.startswith("0"):
        number = f"+{cc_digits}{digits[1:]}"
    elif digits.startswith(cc_digits) and len(digits) == len(cc_digits) + 10:
        number = "+" + digits
    else:
        return None
    return number if 8 <= len(number) - 1 <= 15 else None


def _spoken_pct(value: float) -> str:
    return f"{value:.0f}" if abs(value - round(value)) < 0.05 else f"{value:.1f}"


def call_script(facts: dict) -> str:
    """The sentence spoken to the student. Kept short so it is easy to follow on a phone."""
    t = f"{facts['threshold']:g}"
    lines = [f"Hello {facts['first_name']}. This is an automated call from Attendance Guardian at your college."]
    critical = facts["critical"]
    for subject, pct, need in critical[:2]:
        lines.append(f"Your attendance in {subject} is {_spoken_pct(pct)} percent, below the required {t} percent. "
                     f"Please attend the next {need} {subject} classes without missing any.")
    if len(critical) > 2:
        lines.append(f"You are also below {t} percent in {len(critical) - 2} more subject"
                     f"{'s' if len(critical) - 2 > 1 else ''}.")
    if facts["weak"] or facts["falling"]:
        lines.append("Your recent test scores also need attention.")
    lines.append("We have emailed you the details and a link to book a meeting with your teacher. "
                 f"Please also speak to your faculty adviser, {facts['adviser']}. Thank you.")
    return " ".join(lines)


def build_twiml(message: str, voice: str = "Polly.Aditi", language: str = "en-IN") -> str:
    say = f'<Say voice="{escape(voice)}" language="{escape(language)}">{escape(message)}</Say>'
    return (f'<?xml version="1.0" encoding="UTF-8"?><Response>{say}<Pause length="1"/>'
            f'<Say voice="{escape(voice)}" language="{escape(language)}">I repeat.</Say>{say}</Response>')


def place_calls(settings: Settings, calls: list[dict]) -> list[dict]:
    """Place calls. Each item: {"name", "phone", "message"}. Never raises; returns log rows."""
    logs: list[dict] = []
    if not calls:
        return logs

    def target(item) -> tuple[str | None, str]:
        if settings.test_phone:
            return normalize_phone(settings.test_phone, settings.default_country_code), \
                f"redirected to TEST_PHONE for {item['name']} ({item['phone']})"
        return normalize_phone(item["phone"], settings.default_country_code), ""

    if not settings.calls_live:
        reason = "Demo mode is on" if settings.force_demo else "Twilio credentials not configured"
        for item in calls:
            number, note = target(item)
            logs.append(log_entry("Voice call", number or str(item["phone"]), item["name"],
                                  "DEMO - would call", f"{reason}. {note + '. ' if note else ''}Says: {item['message'][:140]}..."))
        return logs

    try:
        from twilio.rest import Client
    except ImportError:
        return [log_entry("Voice call", str(i["phone"]), i["name"], "FAILED",
                          "The twilio package is not installed (pip install twilio)") for i in calls]

    try:
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    except Exception as exc:
        return [log_entry("Voice call", str(i["phone"]), i["name"], "FAILED", f"Twilio setup failed: {exc}")
                for i in calls]

    for item in calls:
        # Each student is isolated: any error is logged and the loop moves on to the next student.
        number = str(item.get("phone", ""))
        try:
            parsed, note = target(item)
            if not parsed:
                raise ValueError(f"phone number {item.get('phone')!r} could not be parsed")
            number = parsed
            call = client.calls.create(
                to=number,
                from_=settings.twilio_from_number,
                twiml=build_twiml(item["message"], settings.twilio_voice),
            )
            logs.append(log_entry("Voice call", number, item["name"], "CALLED", f"Call SID {call.sid}. {note}".strip()))
        except Exception as exc:
            logs.append(log_entry("Voice call", number, item.get("name", ""), "FAILED", f"failed: {exc}"[:240]))
    return logs
