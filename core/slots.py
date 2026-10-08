"""Free-slot finder and appointment booking.

Bookings are appended to a CSV (default ``data/bookings.csv``). The Streamlit
app mirrors them in ``st.session_state`` so booking still works on hosts with
a read-only or ephemeral filesystem.
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

DEFAULT_BOOKINGS_PATH = Path(__file__).resolve().parents[1] / "data" / "bookings.csv"
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAY_ALIASES = {d[:3].lower(): d for d in DAYS}
BOOKING_COLUMNS = ["booking_id", "roll_no", "student_name", "student_email", "teacher_name", "teacher_email",
                   "subject", "day", "date", "slot", "booked_at"]


class SlotUnavailableError(ValueError):
    """The chosen slot is busy, unknown, or already booked."""


def normalize_day(day) -> str:
    key = str(day).strip().lower()[:3]
    return DAY_ALIASES.get(key, str(day).strip().title())


def normalize_slot(slot) -> str:
    """'9:00 - 10:00' -> '09:00-10:00'."""
    times = re.findall(r"(\d{1,2})[:.](\d{2})", str(slot))
    if len(times) >= 2:
        (h1, m1), (h2, m2) = times[:2]
        return f"{int(h1):02d}:{m1}-{int(h2):02d}:{m2}"
    return str(slot).strip()


def slot_start_minutes(slot: str) -> int:
    m = re.match(r"(\d{2}):(\d{2})", slot)
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 10_000


def clean_timetable(timetable: pd.DataFrame) -> pd.DataFrame:
    tt = timetable.copy()
    tt["teacher_name"] = tt["teacher_name"].astype(str).str.strip()
    tt["day"] = tt["day"].map(normalize_day)
    tt["slot"] = tt["slot"].map(normalize_slot)
    tt["status"] = tt["status"].astype(str).str.strip().str.lower()
    tt["day_order"] = tt["day"].map({d: i for i, d in enumerate(DAYS)}).fillna(99)
    tt["slot_order"] = tt["slot"].map(slot_start_minutes)
    return tt.sort_values(["teacher_name", "day_order", "slot_order"]).reset_index(drop=True)


def next_date(day: str, today: date | None = None) -> date:
    """Next calendar date for a weekday name, always in the future (1-7 days ahead)."""
    today = today or date.today()
    target = DAYS.index(day) if day in DAYS else today.weekday()
    ahead = (target - today.weekday()) % 7 or 7
    return today + timedelta(days=ahead)


# ----------------------------------------------------------------- bookings


def empty_bookings() -> pd.DataFrame:
    return pd.DataFrame(columns=BOOKING_COLUMNS)


def load_bookings(path: Path | str = DEFAULT_BOOKINGS_PATH) -> pd.DataFrame:
    try:
        df = pd.read_csv(path, dtype=str)
        for col in BOOKING_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        return df[BOOKING_COLUMNS]
    except Exception:  # missing, empty, or corrupt file: start fresh
        return empty_bookings()


def save_booking(booking: dict, path: Path | str = DEFAULT_BOOKINGS_PATH) -> bool:
    """Append one booking to the CSV. Returns False if the filesystem is not writable."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = pd.DataFrame([{c: booking.get(c, "") for c in BOOKING_COLUMNS}])
        row.to_csv(path, mode="a", header=not path.exists() or path.stat().st_size == 0, index=False)
        return True
    except OSError:
        return False


def merge_bookings(*frames: pd.DataFrame) -> pd.DataFrame:
    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return empty_bookings()
    return pd.concat(frames, ignore_index=True).drop_duplicates("booking_id", keep="last")[BOOKING_COLUMNS]


def _booked_keys(bookings: pd.DataFrame) -> set[tuple[str, str, str]]:
    if bookings is None or bookings.empty:
        return set()
    return {(str(t).strip(), normalize_day(d), normalize_slot(s))
            for t, d, s in bookings[["teacher_name", "day", "slot"]].itertuples(index=False)}


def timetable_with_bookings(timetable: pd.DataFrame, bookings: pd.DataFrame) -> pd.DataFrame:
    """Timetable where free slots that have a booking show status 'booked'."""
    tt = clean_timetable(timetable)
    booked = _booked_keys(bookings)
    keys = list(zip(tt["teacher_name"], tt["day"], tt["slot"]))
    is_booked = pd.Series([k in booked for k in keys], index=tt.index)
    tt.loc[is_booked & (tt["status"] == "free"), "status"] = "booked"
    return tt


def free_slots(timetable: pd.DataFrame, teacher_name: str, bookings: pd.DataFrame,
               today: date | None = None) -> pd.DataFrame:
    tt = timetable_with_bookings(timetable, bookings)
    free = tt[(tt["teacher_name"] == str(teacher_name).strip()) & (tt["status"] == "free")].copy()
    free["date"] = free["day"].map(lambda d: next_date(d, today).strftime("%d %b %Y"))
    free["label"] = free["day"] + ", " + free["date"] + "  |  " + free["slot"]
    return free[["day", "date", "slot", "label"]].reset_index(drop=True)


def teacher_week_grid(timetable: pd.DataFrame, teacher_name: str, bookings: pd.DataFrame) -> pd.DataFrame:
    """Day x slot grid of busy/free/booked for one teacher."""
    tt = timetable_with_bookings(timetable, bookings)
    tt = tt[tt["teacher_name"] == str(teacher_name).strip()]
    if tt.empty:
        return pd.DataFrame()
    slots = sorted(tt["slot"].unique(), key=slot_start_minutes)
    days = [d for d in DAYS if d in set(tt["day"])]
    grid = tt.pivot_table(index="day", columns="slot", values="status", aggfunc="first")
    return grid.reindex(index=days, columns=slots).fillna("-")


def book_slot(timetable: pd.DataFrame, bookings: pd.DataFrame, *, student: dict, teacher_name: str,
              teacher_email: str, subject: str, day: str, slot: str, today: date | None = None) -> dict:
    """Validate and create a booking. Raises SlotUnavailableError if the slot is not free."""
    day, slot = normalize_day(day), normalize_slot(slot)
    available = free_slots(timetable, teacher_name, bookings, today)
    if available[(available["day"] == day) & (available["slot"] == slot)].empty:
        raise SlotUnavailableError(f"{teacher_name} is not free on {day} at {slot}, or it was just booked.")
    roll = str(student["roll_no"])
    if bookings is not None and not bookings.empty:
        clash = bookings[(bookings["roll_no"] == roll) & (bookings["day"] == day) & (bookings["slot"] == slot)]
        if not clash.empty:
            raise SlotUnavailableError(f"You already have a meeting on {day} at {slot}.")
    return {
        "booking_id": f"BK-{uuid.uuid4().hex[:8].upper()}",
        "roll_no": roll,
        "student_name": str(student["name"]),
        "student_email": str(student.get("email", "")),
        "teacher_name": str(teacher_name),
        "teacher_email": str(teacher_email or ""),
        "subject": str(subject),
        "day": day,
        "date": next_date(day, today).strftime("%d %b %Y"),
        "slot": slot,
        "booked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
