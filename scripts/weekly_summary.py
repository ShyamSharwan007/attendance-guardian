"""Weekly summary job. Runs without Streamlit (used by GitHub Actions).

Loads the attendance data, computes risk, and emails each faculty adviser a
summary of their own advisees. Optionally auto-calls students below the
threshold. Without credentials it runs in DEMO MODE and only prints what it
would send, so it is always safe to run.

    python scripts/weekly_summary.py                  # per-adviser summaries
    python scripts/weekly_summary.py --to hod@x.edu   # one full summary to one address
    python scripts/weekly_summary.py --call           # also auto-call students below threshold

Environment: GMAIL_USER, GMAIL_APP_PASSWORD, APP_URL, TEST_EMAIL, TWILIO_*,
ATTENDANCE_THRESHOLD (default 85), WARNING_MARGIN (default 3), DATA_DIR,
SUMMARY_TO, AUTO_CALL (true/false).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import ai_text, caller, notify, risk  # noqa: E402
from core.config import TRUE_VALUES, load_settings  # noqa: E402


def env(key: str, default: str) -> str:
    value = os.environ.get(key, "")
    return value if value.strip() else default


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Email the weekly attendance risk summary to faculty advisers.")
    p.add_argument("--data-dir", default=env("DATA_DIR", str(risk.SAMPLE_DIR)),
                   help="Folder with students.csv, attendance.csv, marks.csv, teachers.csv, timetable.csv")
    p.add_argument("--threshold", type=float, default=float(env("ATTENDANCE_THRESHOLD", "85")))
    p.add_argument("--margin", type=float, default=float(env("WARNING_MARGIN", "3")))
    p.add_argument("--to", default=env("SUMMARY_TO", ""),
                   help="Send one full summary to this address instead of one per adviser")
    p.add_argument("--call", action="store_true",
                   default=env("AUTO_CALL", "false").strip().lower() in TRUE_VALUES,
                   help="Also auto-call every student below the threshold")
    p.add_argument("--out", default=env("SUMMARY_OUT", str(ROOT / "output")),
                   help="Folder for HTML copies of the summaries")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings()

    try:
        result = risk.analyze(risk.load_sample_data(args.data_dir), args.threshold, args.margin)
    except (risk.DataValidationError, FileNotFoundError) as exc:
        print(f"ERROR: could not load or analyse data in {args.data_dir}: {exc}")
        return 2
    for w in result.warnings:
        print(f"WARNING: {w}")

    s = result.students
    print(f"Threshold {args.threshold:g}% (warning band +{args.margin:g}). Students: {len(s)}, "
          f"critical: {(s['status'] == risk.CRITICAL).sum()}, warning: {(s['status'] == risk.WARNING).sum()}, "
          f"weak marks: {s['weak_subjects'].map(bool).sum()}, falling marks: {s['falling_subjects'].map(bool).sum()}")
    print(f"Email: {'LIVE' if settings.email_live else 'DEMO MODE (no Gmail credentials)'}; "
          f"calls: {'LIVE' if settings.calls_live else 'DEMO MODE (no Twilio credentials)'}")

    if args.to:
        emails = [notify.weekly_summary_email(result, args.to, "Faculty", settings.app_url)]
    else:
        emails = [notify.weekly_summary_email(sub, email, name, settings.app_url)
                  for name, email, sub in notify.adviser_groups(result)]

    out_dir = Path(args.out)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        for i, e in enumerate(emails, start=1):
            (out_dir / f"weekly_summary_{i}.html").write_text(e.html, encoding="utf-8")
        print(f"Saved {len(emails)} HTML preview(s) to {out_dir}")
    except OSError as exc:
        print(f"Could not save previews: {exc}")

    logs = notify.send_emails(settings, emails)

    if args.call:
        calls = []
        for _, row in result.below_threshold_students.iterrows():
            facts = ai_text.student_facts(row, result.student_subjects(row["roll_no"]), result.threshold)
            calls.append({"name": row["name"], "phone": row["phone"], "message": caller.call_script(facts)})
        logs += caller.place_calls(settings, calls)

    for row in logs:
        print(f"[{row['status']}] {row['channel']} -> {row['recipient']}: {row['subject']}"
              + (f" ({row['detail']})" if row["detail"] else ""))
    failed = sum(r["status"] == "FAILED" for r in logs)
    print(f"Done: {len(logs) - failed} ok, {failed} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
