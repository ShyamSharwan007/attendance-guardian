"""Gmail SMTP sending (SSL, port 465) and the HTML email builders.

All builders return :class:`Email` objects; :func:`send_emails` sends them over
one SMTP connection, or logs "DEMO - would send" when credentials are missing.
Email HTML uses inline styles because most mail clients strip <style> blocks.
"""
from __future__ import annotations

import html
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

from .config import Settings
from .risk import CRITICAL, SAFE, WARNING, AnalysisResult, department_summary, subject_summary

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

STATUS_COLORS = {CRITICAL: "#d03b3b", WARNING: "#b07800", SAFE: "#0a7f0a"}
STATUS_BG = {CRITICAL: "#fdecec", WARNING: "#fff6dd", SAFE: "#e8f6e8"}
INK, MUTED, BORDER, BRAND = "#1f2933", "#52606d", "#e4e7eb", "#1c5cab"


@dataclass
class Email:
    to: str
    subject: str
    html: str
    kind: str = "Email"
    cc: list[str] = field(default_factory=list)
    text: str = ""


def log_entry(channel: str, recipient: str, subject: str, status: str, detail: str = "") -> dict:
    return {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "channel": channel,
        "recipient": recipient,
        "subject": subject,
        "status": status,
        "detail": detail,
    }


# ------------------------------------------------------------------- sending


def _html_to_text(markup: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", markup)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</tr>|</h\d>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _valid_address(addr: str | None) -> bool:
    return bool(addr) and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", str(addr).strip()) is not None


def _route(settings: Settings, email: Email) -> tuple[str, list[str], str, str]:
    """Apply the TEST_EMAIL override. Returns (to, cc, subject, note)."""
    if settings.test_email:
        return (settings.test_email, [], f"[TEST for {email.to}] {email.subject}",
                f"redirected to TEST_EMAIL ({settings.test_email})")
    return email.to, [c for c in email.cc if _valid_address(c)], email.subject, ""


def _mime(settings: Settings, email: Email, to: str, cc: list[str], subject: str):
    from email.message import EmailMessage
    from email.utils import formataddr

    msg = EmailMessage()
    msg["From"] = formataddr((settings.sender_name, settings.gmail_user or ""))
    msg["To"] = to
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg.set_content(email.text or _html_to_text(email.html))
    msg.add_alternative(email.html, subtype="html")
    return msg


def send_emails(settings: Settings, emails: list[Email]) -> list[dict]:
    """Send a batch over one SMTP_SSL connection. Never raises; returns log rows."""
    logs: list[dict] = []
    if not emails:
        return logs
    if not settings.email_live:
        reason = "Demo mode is on" if settings.force_demo else "Gmail credentials not configured"
        for e in emails:
            to, cc, subject, note = _route(settings, e)
            cc_note = f"; cc {', '.join(cc)}" if cc else ""
            logs.append(log_entry(e.kind, to, subject, "DEMO - would send", f"{reason}{cc_note}. {note}".strip()))
        return logs

    pending = list(emails)
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=30) as server:
            server.login(settings.gmail_user, settings.gmail_app_password)
            while pending:
                e = pending.pop(0)
                to, cc, subject, note = _route(settings, e)
                if not _valid_address(to):
                    logs.append(log_entry(e.kind, str(to), subject, "FAILED", "Invalid email address"))
                    continue
                try:
                    server.send_message(_mime(settings, e, to, cc, subject))
                    cc_note = f"cc {', '.join(cc)}. " if cc else ""
                    logs.append(log_entry(e.kind, to, subject, "SENT", f"{cc_note}{note}".strip()))
                except Exception as exc:  # one bad recipient should not stop the batch
                    logs.append(log_entry(e.kind, to, subject, "FAILED", str(exc)[:200]))
    except smtplib.SMTPAuthenticationError:
        detail = "Gmail rejected the login. Use a 16-character App Password, not your normal password."
        logs += [log_entry(e.kind, e.to, e.subject, "FAILED", detail) for e in pending]
    except Exception as exc:
        logs += [log_entry(e.kind, e.to, e.subject, "FAILED", f"SMTP error: {exc}"[:200]) for e in pending]
    return logs


# ------------------------------------------------------------------ building


def esc(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    return html.escape(str(value))


def _layout(title: str, body: str, accent: str = BRAND, preheader: str = "") -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title></head>
<body style="margin:0;padding:0;background:#f4f5f7;font-family:Segoe UI,Helvetica,Arial,sans-serif;color:{INK};">
<span style="display:none;max-height:0;overflow:hidden;">{esc(preheader)}</span>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;background:#ffffff;border:1px solid {BORDER};border-radius:10px;overflow:hidden;">
<tr><td style="background:{accent};height:6px;font-size:0;line-height:0;">&nbsp;</td></tr>
<tr><td style="padding:22px 28px 6px 28px;">
<div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:{MUTED};font-weight:600;">Attendance Guardian</div>
<h1 style="margin:6px 0 0 0;font-size:21px;line-height:1.3;color:{INK};">{esc(title)}</h1>
</td></tr>
<tr><td style="padding:12px 28px 26px 28px;font-size:15px;line-height:1.6;color:{INK};">{body}</td></tr>
<tr><td style="padding:14px 28px;border-top:1px solid {BORDER};font-size:12px;color:{MUTED};">
This is an automated message from Attendance Guardian. Please do not reply to this email.</td></tr>
</table></td></tr></table></body></html>"""


def _badge(status: str) -> str:
    return (f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;font-weight:700;'
            f'color:{STATUS_COLORS.get(status, INK)};background:{STATUS_BG.get(status, "#eee")};">{esc(status)}</span>')


def _table(headers: list[str], rows: list[list[str]], align: list[str] | None = None) -> str:
    align = align or ["left"] * len(headers)
    th = "".join(
        f'<th align="{a}" style="padding:8px 10px;border-bottom:2px solid {BORDER};font-size:12px;'
        f'text-transform:uppercase;letter-spacing:.04em;color:{MUTED};">{esc(h)}</th>'
        for h, a in zip(headers, align))
    body = "".join(
        "<tr>" + "".join(
            f'<td align="{a}" style="padding:8px 10px;border-bottom:1px solid {BORDER};font-size:14px;">{cell}</td>'
            for cell, a in zip(row, align)) + "</tr>"
        for row in rows)
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse;margin:12px 0;">'
            f"<tr>{th}</tr>{body}</table>")


def _button(label: str, url: str) -> str:
    return (f'<p style="margin:20px 0;"><a href="{esc(url)}" style="background:{BRAND};color:#ffffff;'
            f'text-decoration:none;padding:11px 20px;border-radius:6px;font-weight:600;display:inline-block;">'
            f"{esc(label)}</a></p>")


def _paragraphs(text: str) -> str:
    return "".join(f'<p style="margin:0 0 12px 0;">{esc(p.strip())}</p>' for p in text.split("\n\n") if p.strip())


def _mark(value) -> str:
    return "-" if value is None or pd.isna(value) else f"{value:g}"


def student_warning_email(student, subjects: pd.DataFrame, threshold: float, message: str,
                          booking_link: str) -> Email:
    """Personal warning: current %, classes needed in a row, weak subjects, how to book."""
    first = str(student["name"]).split()[0]
    status = str(student["status"])
    accent = STATUS_COLORS.get(status, BRAND) if status != SAFE else BRAND
    rows = []
    for r in subjects.sort_values(["status_rank", "attendance_pct"]).itertuples():
        need = f"<b>{r.recovery_classes}</b>" if r.recovery_classes else "0"
        mark_note = _mark(r.latest_mark)
        if r.weak_marks:
            mark_note = f'<span style="color:{STATUS_COLORS[CRITICAL]};font-weight:700;">{mark_note} (weak)</span>'
        trend = esc(r.trend)
        if r.falling_marks:
            trend = f'<span style="color:{STATUS_COLORS[CRITICAL]};font-weight:600;">{trend}</span>'
        rows.append([esc(r.subject), f"{r.attendance_pct:.1f}%", _badge(r.status), need, mark_note, trend])

    actions = []
    for r in subjects[subjects["status"] == CRITICAL].itertuples():
        actions.append(f"<li>Attend the next <b>{r.recovery_classes} {esc(r.subject)}</b> classes in a row "
                       f"to reach {threshold:g}%.</li>")
    for r in subjects[subjects["status"] == WARNING].itertuples():
        actions.append(f"<li>Do not miss any <b>{esc(r.subject)}</b> class. You are at {r.attendance_pct:.1f}%, "
                       f"just above the {threshold:g}% line.</li>")
    weak = subjects[subjects["weak_marks"] | subjects["falling_marks"]]
    for r in weak.itertuples():
        actions.append(f"<li>Meet your <b>{esc(r.subject)}</b> teacher about your test scores "
                       f"({esc(r.marks_history)}).</li>")
    action_html = (f'<h3 style="font-size:16px;margin:18px 0 6px 0;">What to do next</h3>'
                   f'<ul style="margin:0 0 8px 18px;padding:0;">{"".join(actions)}</ul>') if actions else ""

    body = (
        f'<p style="margin:0 0 12px 0;">Dear {esc(first)},</p>'
        f"{_paragraphs(message)}"
        f'<p style="margin:14px 0 0 0;">Overall attendance: <b>{student["overall_pct"]:.1f}%</b> '
        f"&nbsp;|&nbsp; Required: <b>{threshold:g}%</b> &nbsp;|&nbsp; Status: {_badge(status)}</p>"
        + _table(["Subject", "Attendance", "Status", "Classes needed in a row", "Latest test", "Trend"], rows,
                 ["left", "right", "left", "right", "right", "left"])
        + action_html
        + _button("Book a slot with your teacher", booking_link)
        + f'<p style="margin:0;font-size:13px;color:{MUTED};">If the button does not work, open Attendance '
          f'Guardian, go to <b>Book Appointment</b> and enter your roll number <b>{esc(student["roll_no"])}</b>.</p>'
        + f'<p style="margin:18px 0 0 0;">Regards,<br>{esc(student.get("adviser_name", "Faculty Adviser"))}<br>'
          f'<span style="color:{MUTED};font-size:13px;">Faculty Adviser, {esc(student["department"])}</span></p>'
    )
    subject = (f"Action needed: your attendance is below {threshold:g}%" if status == CRITICAL
               else f"Heads up: your attendance is close to {threshold:g}%" if status == WARNING
               else "Heads up: your recent test scores need attention")
    return Email(to=str(student["email"]), subject=subject, kind="Student warning",
                 html=_layout(f"Attendance and progress update for {student['name']}", body, accent,
                              preheader=subject))


def _student_rows(subjects: pd.DataFrame, with_subject: bool) -> list[list[str]]:
    rows = []
    for r in subjects.itertuples():
        row = [esc(r.roll_no), esc(r.name)]
        if with_subject:
            row.append(esc(r.subject))
        row += [f"{r.attendance_pct:.1f}%", _badge(r.status), str(r.recovery_classes),
                esc(r.marks_history), f"{r.risk_score:.0f}"]
        rows.append(row)
    return rows


def teacher_alert_email(teacher_name: str, teacher_email: str, subject_name: str,
                        rows: pd.DataFrame, threshold: float) -> Email:
    headers = ["Roll no", "Name", "Attendance", "Status", "Classes needed", "Tests", "Risk"]
    body = (
        f'<p style="margin:0 0 12px 0;">Dear {esc(teacher_name)},</p>'
        f'<p style="margin:0 0 12px 0;">{len(rows)} student(s) in <b>{esc(subject_name)}</b> need attention. '
        f"They are below or close to the {threshold:g}% attendance requirement, or their test scores are weak "
        f"or falling. Each student has been emailed and can book one of your free timetable slots.</p>"
        + _table(headers, _student_rows(rows, with_subject=False),
                 ["left", "left", "right", "left", "right", "left", "right"])
        + f'<p style="margin:12px 0 0 0;color:{MUTED};font-size:13px;">"Classes needed" is the number of '
          f"consecutive classes the student must attend to reach {threshold:g}%.</p>"
    )
    return Email(to=teacher_email, subject=f"At-risk students in {subject_name}: {len(rows)} need attention",
                 kind="Teacher alert", html=_layout(f"At-risk students in {subject_name}", body))


def adviser_alert_email(adviser_name: str, adviser_email: str, rows: pd.DataFrame, threshold: float) -> Email:
    n_students = rows["roll_no"].nunique()
    headers = ["Roll no", "Name", "Subject", "Attendance", "Status", "Classes needed", "Tests", "Risk"]
    body = (
        f'<p style="margin:0 0 12px 0;">Dear {esc(adviser_name)},</p>'
        f'<p style="margin:0 0 12px 0;"><b>{n_students}</b> of your advisees are at risk. The table lists every '
        f"subject that needs attention, most at risk first. The students and their subject teachers have been "
        f"notified.</p>"
        + _table(headers, _student_rows(rows, with_subject=True),
                 ["left", "left", "left", "right", "left", "right", "left", "right"])
    )
    return Email(to=adviser_email, subject=f"Advisee alert: {n_students} student(s) at risk",
                 kind="Adviser alert", html=_layout("Your advisees at risk", body))


def _kpi_cells(items: list[tuple[str, str, str]]) -> str:
    cells = "".join(
        f'<td align="center" style="padding:10px 6px;border:1px solid {BORDER};border-radius:8px;">'
        f'<div style="font-size:22px;font-weight:700;color:{color};">{esc(value)}</div>'
        f'<div style="font-size:12px;color:{MUTED};">{esc(label)}</div></td>'
        for label, value, color in items)
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="6" '
            f'style="margin:8px 0 4px 0;"><tr>{cells}</tr></table>')


def weekly_summary_html(result: AnalysisResult, audience: str = "Faculty", app_url: str = "",
                        week_of: date | None = None, top_n: int = 10) -> str:
    week_of = week_of or (date.today() - timedelta(days=date.today().weekday()))  # Monday of this week
    s = result.students
    total = len(s)
    crit = int((s["status"] == CRITICAL).sum())
    warn = int((s["status"] == WARNING).sum())
    weak = int(s["weak_subjects"].map(bool).sum())
    falling = int(s["falling_subjects"].map(bool).sum())
    avg = f"{s['overall_pct'].mean():.1f}%" if total else "-"

    kpis = _kpi_cells([
        ("Students", str(total), INK),
        ("Critical", str(crit), STATUS_COLORS[CRITICAL]),
        ("Warning", str(warn), STATUS_COLORS[WARNING]),
        ("Weak marks", str(weak), INK),
        ("Avg attendance", avg, INK),
    ])

    dept = department_summary(result) if total else pd.DataFrame()
    dept_rows = [[esc(r.department), str(r.students), str(r.critical), str(r.warning), str(r.weak_marks),
                  f"{r.avg_attendance:.1f}%"] for r in dept.itertuples()]
    subj = subject_summary(result) if total else pd.DataFrame()
    subj_rows = [[esc(r.subject), str(r.critical), str(r.warning), str(int(r.weak_marks)),
                  str(int(r.falling_marks)), f"{r.avg_attendance:.1f}%"] for r in subj.itertuples()]

    top = s[s["at_risk"]].head(top_n)
    top_rows = []
    for r in top.itertuples():
        focus = r.critical_subjects or r.warning_subjects or r.falling_subjects or r.weak_subjects
        focus_text = esc(", ".join(focus[:2]))
        if len(focus) > 2:
            focus_text += f' <span style="color:{MUTED};">+{len(focus) - 2} more</span>'
        top_rows.append([esc(r.roll_no), esc(r.name), esc(r.department), f"{r.overall_pct:.1f}%",
                         _badge(r.status), focus_text, str(r.recovery_total), f"{r.risk_score:.0f}"])

    body = (
        f'<p style="margin:0 0 6px 0;">Dear {esc(audience)},</p>'
        f'<p style="margin:0 0 6px 0;">Here is the attendance and performance summary for the week of '
        f"<b>{week_of.strftime('%d %b %Y')}</b>. Required attendance is <b>{result.threshold:g}%</b>; "
        f"students within {result.margin:g} points above it are flagged as warning. {falling} student(s) have "
        f"falling test scores.</p>"
        + kpis
        + '<h3 style="font-size:16px;margin:18px 0 0 0;">Most at-risk students</h3>'
        + (_table(["Roll no", "Name", "Dept", "Overall", "Status", "Focus subjects", "Classes needed", "Risk"],
                  top_rows, ["left", "left", "left", "right", "left", "left", "right", "right"])
           if top_rows else '<p style="margin:8px 0;">No students are at risk this week.</p>')
        + '<h3 style="font-size:16px;margin:18px 0 0 0;">By department</h3>'
        + _table(["Department", "Students", "Critical", "Warning", "Weak marks", "Avg attendance"], dept_rows,
                 ["left", "right", "right", "right", "right", "right"])
        + '<h3 style="font-size:16px;margin:18px 0 0 0;">By subject (student-subject rows)</h3>'
        + _table(["Subject", "Critical", "Warning", "Weak", "Falling", "Avg attendance"], subj_rows,
                 ["left", "right", "right", "right", "right", "right"])
        + (_button("Open the risk dashboard", app_url) if app_url else "")
    )
    return _layout(f"Weekly attendance summary, week of {week_of.strftime('%d %b %Y')}", body,
                   preheader=f"{crit} critical, {warn} warning out of {total} students")


def weekly_summary_email(result: AnalysisResult, to: str, audience: str, app_url: str = "") -> Email:
    s = result.students
    crit = int((s["status"] == CRITICAL).sum())
    monday = date.today() - timedelta(days=date.today().weekday())
    subject = f"Weekly attendance summary: {crit} critical of {len(s)} students (week of {monday:%d %b %Y})"
    return Email(to=to, subject=subject, kind="Weekly summary",
                 html=weekly_summary_html(result, audience=audience, app_url=app_url))


def adviser_groups(result: AnalysisResult) -> list[tuple[str, str, AnalysisResult]]:
    """(adviser_name, adviser_email, that adviser's slice of the analysis) for each adviser."""
    groups = []
    for (name, email), g in result.students.groupby(["adviser_name", "adviser_email"], sort=True):
        groups.append((str(name), str(email), result.subset(g["roll_no"])))
    return groups


def booking_confirmation_emails(booking: dict) -> list[Email]:
    when = f"{booking['day']}, {booking['date']} at {booking['slot']}"
    details = _table(["Detail", "Value"], [
        ["Student", f"{esc(booking['student_name'])} ({esc(booking['roll_no'])})"],
        ["Teacher", esc(booking["teacher_name"])],
        ["Subject", esc(booking["subject"])],
        ["When", esc(when)],
        ["Booking ID", esc(booking["booking_id"])],
    ])
    student_body = (
        f'<p style="margin:0 0 12px 0;">Dear {esc(str(booking["student_name"]).split()[0])},</p>'
        f'<p style="margin:0 0 12px 0;">Your meeting with <b>{esc(booking["teacher_name"])}</b> about '
        f"<b>{esc(booking['subject'])}</b> is confirmed for <b>{esc(when)}</b>. Please bring your notes and "
        f"a list of the topics you found difficult.</p>" + details)
    teacher_body = (
        f'<p style="margin:0 0 12px 0;">Dear {esc(booking["teacher_name"])},</p>'
        f'<p style="margin:0 0 12px 0;"><b>{esc(booking["student_name"])}</b> ({esc(booking["roll_no"])}) '
        f"booked your free slot on <b>{esc(when)}</b> to discuss <b>{esc(booking['subject'])}</b>. "
        f"The slot is now marked as booked in your timetable.</p>" + details)
    emails = []
    if booking.get("student_email"):
        emails.append(Email(to=booking["student_email"], kind="Booking confirmation",
                            subject=f"Confirmed: meeting with {booking['teacher_name']} on {when}",
                            html=_layout("Your appointment is confirmed", student_body)))
    if booking.get("teacher_email"):
        emails.append(Email(to=booking["teacher_email"], kind="Booking notice",
                            subject=f"New booking: {booking['student_name']} on {when}",
                            html=_layout("A student booked your free slot", teacher_body)))
    return emails


def staff_alert_emails(result: AnalysisResult) -> tuple[list[Email], list[str]]:
    """One email per subject teacher and one per faculty adviser, covering their at-risk students.

    Returns (emails, problems) where problems lists subjects that have no teacher email.
    """
    from .risk import at_risk_table

    rows = at_risk_table(result)
    emails: list[Email] = []
    problems: list[str] = []
    if rows.empty:
        return emails, problems
    for subject_name, g in rows.groupby("subject", sort=False):
        teacher_email = g["teacher_email"].dropna().astype(str)
        teacher_email = teacher_email[teacher_email.str.contains("@")]
        if teacher_email.empty:
            problems.append(f"No teacher email for {subject_name}; add it to the teachers file.")
            continue
        teacher_name = str(g["teacher_name"].dropna().iloc[0]) if g["teacher_name"].notna().any() else "Teacher"
        emails.append(teacher_alert_email(teacher_name, teacher_email.iloc[0], str(subject_name), g,
                                          result.threshold))
    for (adviser_name, adviser_email), g in rows.groupby(["adviser_name", "adviser_email"], sort=False):
        emails.append(adviser_alert_email(str(adviser_name), str(adviser_email), g, result.threshold))
    return emails, problems
