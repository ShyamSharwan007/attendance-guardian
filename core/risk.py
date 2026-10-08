"""Pure pandas risk logic. No Streamlit, no network.

Main entry point: :func:`analyze`, which turns the raw tables into one row per
(student, subject) and one row per student, each with a 0-100 risk score,
sorted most-at-risk first.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data"

REQUIRED_COLUMNS: dict[str, list[str]] = {
    "students": ["roll_no", "name", "department", "email", "phone", "adviser_name", "adviser_email"],
    "attendance": ["roll_no", "subject", "classes_held", "classes_attended"],
    "marks": ["roll_no", "subject"],  # plus at least one test column (test1, test2, ...)
    "teachers": ["teacher_name", "email", "subject"],
    "timetable": ["teacher_name", "day", "slot", "status"],
}
DATASET_LABELS = {
    "students": "Students",
    "attendance": "Attendance",
    "marks": "Marks",
    "teachers": "Teachers",
    "timetable": "Timetable",
}
COLUMN_ALIASES = {
    "roll": "roll_no", "rollno": "roll_no", "roll_number": "roll_no", "register_no": "roll_no",
    "student_name": "name", "dept": "department", "branch": "department",
    "email_id": "email", "mobile": "phone", "phone_no": "phone", "phone_number": "phone",
    "held": "classes_held", "total_classes": "classes_held", "classes_conducted": "classes_held",
    "attended": "classes_attended", "present": "classes_attended",
    "teacher": "teacher_name", "faculty_name": "teacher_name", "course": "subject",
    "time": "slot", "time_slot": "slot",
}
TEST_COLUMN = re.compile(r"^test_?(\d+)$")

CRITICAL, WARNING, SAFE = "CRITICAL", "WARNING", "SAFE"
STATUS_RANK = {CRITICAL: 0, WARNING: 1, SAFE: 2}

WEAK_MARK = 40          # latest test below this is "weak"
FALLING_DROP = 15       # latest dropped by at least this much from the previous test
ATTENDANCE_WEIGHT = 0.6
MARKS_WEIGHT = 0.4
CRITICAL_SPAN = 20.0    # points below the threshold at which attendance risk maxes out


class DataValidationError(ValueError):
    """Raised when an input table is missing columns or is unusable."""


# --------------------------------------------------------------------------- I/O


def read_csv(source) -> pd.DataFrame:
    """Read a CSV from a path or an uploaded file object, keeping every value as text."""
    if hasattr(source, "getvalue"):
        raw = source.getvalue()
    elif hasattr(source, "read"):
        raw = source.read()
    else:
        raw = Path(source).read_bytes()
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not raw.strip():
        raise DataValidationError("The file is empty.")
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(raw), dtype=str, encoding=encoding)
        except UnicodeDecodeError:
            continue
        except pd.errors.EmptyDataError as exc:
            raise DataValidationError("The file is empty.") from exc
        except pd.errors.ParserError as exc:
            raise DataValidationError(f"Could not parse the CSV: {exc}") from exc
    raise DataValidationError("Could not decode the file. Save it as a UTF-8 CSV.")


def load_sample_data(base_dir: Path | str | None = None) -> dict[str, pd.DataFrame]:
    base = Path(base_dir) if base_dir else SAMPLE_DIR
    return {name: read_csv(base / f"{name}.csv") for name in REQUIRED_COLUMNS}


# -------------------------------------------------------------------- validation


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    cols = [re.sub(r"[^0-9a-z]+", "_", str(c).strip().lower()).strip("_") for c in df.columns]
    df.columns = [COLUMN_ALIASES.get(c, c) for c in cols]
    for col in df.columns[df.dtypes == object]:
        df[col] = df[col].map(lambda v: v.strip() if isinstance(v, str) else v)
    return df.dropna(how="all")


def test_columns(df: pd.DataFrame) -> list[str]:
    found = [(int(m.group(1)), c) for c in df.columns if (m := TEST_COLUMN.match(str(c)))]
    return [c for _, c in sorted(found)]


def validate(name: str, df: pd.DataFrame | None) -> pd.DataFrame:
    """Normalise column names and check required columns. Raises DataValidationError."""
    label = DATASET_LABELS.get(name, name)
    if df is None:
        raise DataValidationError(f"{label}: no file provided.")
    df = normalize_columns(df)
    if df.empty:
        raise DataValidationError(f"{label}: the file has no data rows.")
    required = REQUIRED_COLUMNS[name]
    missing = [c for c in required if c not in df.columns]
    if name == "marks" and not test_columns(df):
        missing.append("test1/test2/test3 (at least one test column)")
    if missing:
        expected = required + (["test1", "test2", "test3"] if name == "marks" else [])
        raise DataValidationError(
            f"{label} file is missing required column(s): {', '.join(missing)}. "
            f"Expected: {', '.join(expected)}. Found: {', '.join(map(str, df.columns))}."
        )
    if "roll_no" in df.columns:
        df["roll_no"] = df["roll_no"].astype(str).str.strip().str.upper()
    return df


def validate_all(raw: dict[str, pd.DataFrame | None]) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Validate every table. Returns (clean tables, list of error messages)."""
    clean, errors = {}, []
    for name in REQUIRED_COLUMNS:
        try:
            clean[name] = validate(name, raw.get(name))
        except DataValidationError as exc:
            errors.append(str(exc))
    return clean, errors


# --------------------------------------------------------------------- formulas


def recovery_classes(held, attended, threshold_pct: float):
    """Consecutive classes a student must attend to reach the threshold.

    x = ceil((r * held - attended) / (1 - r)), or 0 if already at or above r.
    Works on scalars and on numpy/pandas arrays.
    """
    r = min(max(threshold_pct / 100.0, 0.0), 0.99)
    held_arr = np.asarray(held, dtype=float)
    att_arr = np.asarray(attended, dtype=float)
    need = (r * held_arr - att_arr) / (1.0 - r)
    need = np.ceil(np.round(need, 6))  # round first so 6.0000000001 does not become 7
    need = np.where(held_arr <= 0, 0, np.clip(need, 0, None)).astype(int)
    return int(need) if need.ndim == 0 else need


def attendance_status(pct, threshold: float, margin: float):
    pct = np.asarray(pct, dtype=float)
    out = np.where(pct < threshold, CRITICAL, np.where(pct < threshold + margin, WARNING, SAFE))
    return str(out) if out.ndim == 0 else out


def attendance_risk(pct, threshold: float, margin: float):
    """0 at the warning cutoff, 0.25 at the threshold, 1.0 at CRITICAL_SPAN points below it."""
    pct = np.asarray(pct, dtype=float)
    cutoff = threshold + margin
    warning_part = 0.25 * np.clip((cutoff - pct) / max(margin, 1e-9), 0, 1)
    critical_part = 0.25 + 0.75 * np.clip((threshold - pct) / CRITICAL_SPAN, 0, 1)
    return np.where(pct < threshold, critical_part, np.where(pct < cutoff, warning_part, 0.0))


def marks_features(values: Iterable) -> dict:
    """Latest score, drop from the previous test, and trend flags for one row of tests."""
    vals = [float(v) for v in values if v is not None and not pd.isna(v)]
    if not vals:
        return {"latest_mark": np.nan, "previous_mark": np.nan, "mark_drop": np.nan,
                "weak_marks": False, "falling_marks": False, "trend": "No marks", "marks_risk": 0.0}
    latest = vals[-1]
    previous = vals[-2] if len(vals) >= 2 else np.nan
    drop = previous - latest if len(vals) >= 2 else 0.0
    last3 = vals[-3:]
    strictly_down = len(last3) == 3 and last3[0] > last3[1] > last3[2]
    sharp_drop = drop >= FALLING_DROP
    falling = bool(sharp_drop or strictly_down)
    weak = latest < WEAK_MARK

    if sharp_drop:
        trend = "Sharp drop"
    elif strictly_down:
        trend = "Declining"
    elif len(vals) >= 2 and latest > previous:
        trend = "Improving"
    else:
        trend = "Stable"

    level = float(np.clip((60.0 - latest) / 40.0, 0, 1))   # 0 at 60+, 0.5 at 40, 1 at 20 or less
    slope = float(np.clip(max(drop, 0.0) / 30.0, 0, 1))   # 1 at a 30-point drop
    if strictly_down:
        slope = max(slope, 0.5)
    return {"latest_mark": latest, "previous_mark": previous, "mark_drop": drop,
            "weak_marks": bool(weak), "falling_marks": falling, "trend": trend,
            "marks_risk": 0.6 * level + 0.4 * slope}


# ---------------------------------------------------------------------- analysis


@dataclass
class AnalysisResult:
    subjects: pd.DataFrame      # one row per (student, subject)
    students: pd.DataFrame      # one row per student
    threshold: float
    margin: float
    warnings: list[str] = field(default_factory=list)

    @property
    def at_risk_students(self) -> pd.DataFrame:
        return self.students[self.students["at_risk"]]

    @property
    def below_threshold_students(self) -> pd.DataFrame:
        return self.students[self.students["status"] == CRITICAL]

    def student(self, roll_no: str) -> pd.Series | None:
        hit = self.students[self.students["roll_no"] == str(roll_no).strip().upper()]
        return None if hit.empty else hit.iloc[0]

    def student_subjects(self, roll_no: str) -> pd.DataFrame:
        return self.subjects[self.subjects["roll_no"] == str(roll_no).strip().upper()]

    def subset(self, roll_nos: Iterable[str]) -> "AnalysisResult":
        """Same analysis restricted to some students (e.g. one adviser's class)."""
        keep = set(roll_nos)
        return AnalysisResult(
            subjects=self.subjects[self.subjects["roll_no"].isin(keep)].reset_index(drop=True),
            students=self.students[self.students["roll_no"].isin(keep)].reset_index(drop=True),
            threshold=self.threshold, margin=self.margin, warnings=list(self.warnings))

    def for_subjects(self, subjects: Iterable[str]) -> "AnalysisResult":
        """Same analysis restricted to some subjects, with the student rollup recomputed."""
        rows = self.subjects[self.subjects["subject"].isin(set(subjects))].reset_index(drop=True)
        students = _student_rollup(rows) if not rows.empty else self.students.iloc[0:0]
        return AnalysisResult(subjects=rows, students=students, threshold=self.threshold,
                              margin=self.margin, warnings=list(self.warnings))


def _clean_attendance(att: pd.DataFrame, warnings: list[str]) -> pd.DataFrame:
    att = att.copy()
    att["subject"] = att["subject"].astype(str).str.strip()
    for col in ("classes_held", "classes_attended"):
        att[col] = pd.to_numeric(att[col], errors="coerce")
    bad = att[["classes_held", "classes_attended"]].isna().any(axis=1) | (att["classes_held"] < 0)
    if bad.any():
        warnings.append(f"Attendance: skipped {int(bad.sum())} row(s) with missing or invalid class counts.")
        att = att[~bad]
    over = att["classes_attended"] > att["classes_held"]
    if over.any():
        warnings.append(f"Attendance: {int(over.sum())} row(s) had attended > held; capped at held.")
        att.loc[over, "classes_attended"] = att.loc[over, "classes_held"]
    att["classes_attended"] = att["classes_attended"].clip(lower=0)
    if att.duplicated(["roll_no", "subject"]).any():
        warnings.append("Attendance: duplicate (roll_no, subject) rows were summed together.")
        att = att.groupby(["roll_no", "subject"], as_index=False)[["classes_held", "classes_attended"]].sum()
    return att[["roll_no", "subject", "classes_held", "classes_attended"]]


def _clean_marks(marks: pd.DataFrame, warnings: list[str]) -> pd.DataFrame:
    marks = marks.copy()
    marks["subject"] = marks["subject"].astype(str).str.strip()
    tcols = test_columns(marks)
    for c in tcols:
        marks[c] = pd.to_numeric(marks[c], errors="coerce")
    if marks.duplicated(["roll_no", "subject"]).any():
        warnings.append("Marks: duplicate (roll_no, subject) rows found; kept the last one.")
        marks = marks.drop_duplicates(["roll_no", "subject"], keep="last")
    feats = pd.DataFrame(
        [marks_features(row) for row in marks[tcols].itertuples(index=False)], index=marks.index)
    marks["marks_history"] = marks[tcols].apply(
        lambda r: " / ".join("-" if pd.isna(v) else f"{v:g}" for v in r), axis=1)
    return pd.concat([marks[["roll_no", "subject", "marks_history"] + tcols], feats], axis=1)


def analyze(data: dict[str, pd.DataFrame], threshold: float = 85.0, margin: float = 3.0) -> AnalysisResult:
    """Compute the risk tables. ``data`` needs students, attendance and marks; teachers is optional."""
    warnings: list[str] = []
    students = validate("students", data.get("students")).drop_duplicates("roll_no", keep="last")
    attendance = _clean_attendance(validate("attendance", data.get("attendance")), warnings)
    marks = _clean_marks(validate("marks", data.get("marks")), warnings)
    teachers = data.get("teachers")

    unknown = sorted(set(attendance["roll_no"]) - set(students["roll_no"]))
    if unknown:
        more = "..." if len(unknown) > 5 else ""
        warnings.append(
            f"Attendance: {len(unknown)} roll number(s) not in the students file were ignored "
            f"({', '.join(unknown[:5])}{more}).")
    df = attendance.merge(students, on="roll_no", how="inner")
    if df.empty:
        raise DataValidationError("No attendance rows match the roll numbers in the students file.")
    no_att = sorted(set(students["roll_no"]) - set(attendance["roll_no"]))
    if no_att:
        warnings.append(f"Students: {len(no_att)} student(s) have no attendance rows and are not scored.")

    df = df.merge(marks, on=["roll_no", "subject"], how="left")
    missing_marks = int(df["latest_mark"].isna().sum())
    if missing_marks:
        warnings.append(
            f"Marks: {missing_marks} student-subject row(s) have no marks; scored on attendance only.")
    df["weak_marks"] = df["weak_marks"].astype("boolean").fillna(False).astype(bool)
    df["falling_marks"] = df["falling_marks"].astype("boolean").fillna(False).astype(bool)
    df["marks_risk"] = df["marks_risk"].astype(float).fillna(0.0)
    df["trend"] = df["trend"].fillna("No marks")
    df["marks_history"] = df["marks_history"].fillna("-")

    if teachers is not None:
        try:
            t = validate("teachers", teachers)
            t["subject"] = t["subject"].astype(str).str.strip()
            t = t.drop_duplicates("subject")[["subject", "teacher_name", "email"]]
            df = df.merge(t.rename(columns={"email": "teacher_email"}), on="subject", how="left")
        except DataValidationError as exc:
            warnings.append(str(exc))
    for col in ("teacher_name", "teacher_email"):
        if col not in df.columns:
            df[col] = np.nan

    held = df["classes_held"].to_numpy(float)
    attended = df["classes_attended"].to_numpy(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        pct = np.where(held > 0, attended / held * 100.0, 100.0)
    df["attendance_pct"] = np.round(pct, 2)
    df["status"] = attendance_status(pct, threshold, margin)
    df["recovery_classes"] = recovery_classes(held, attended, threshold)
    df["attendance_risk"] = attendance_risk(pct, threshold, margin)
    df["risk_score"] = np.round(
        100 * (ATTENDANCE_WEIGHT * df["attendance_risk"] + MARKS_WEIGHT * df["marks_risk"]), 1)
    df["at_risk"] = (df["status"] != SAFE) | df["weak_marks"] | df["falling_marks"]
    df["status_rank"] = df["status"].map(STATUS_RANK)
    df = df.sort_values(["risk_score", "attendance_pct"], ascending=[False, True]).reset_index(drop=True)

    return AnalysisResult(subjects=df, students=_student_rollup(df), threshold=threshold,
                          margin=margin, warnings=warnings)


def _student_rollup(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for roll, g in df.groupby("roll_no", sort=False):
        first = g.iloc[0]
        held, attended = g["classes_held"].sum(), g["classes_attended"].sum()
        rows.append({
            "roll_no": roll,
            "name": first["name"],
            "department": first["department"],
            "email": first["email"],
            "phone": first["phone"],
            "adviser_name": first["adviser_name"],
            "adviser_email": first["adviser_email"],
            "overall_pct": round(attended / held * 100, 2) if held else 100.0,
            "min_subject_pct": float(g["attendance_pct"].min()),
            "status": g.loc[g["status_rank"].idxmin(), "status"],
            "risk_score": float(g["risk_score"].max()),
            "critical_subjects": g.loc[g["status"] == CRITICAL, "subject"].tolist(),
            "warning_subjects": g.loc[g["status"] == WARNING, "subject"].tolist(),
            "weak_subjects": g.loc[g["weak_marks"], "subject"].tolist(),
            "falling_subjects": g.loc[g["falling_marks"], "subject"].tolist(),
            "recovery_total": int(g["recovery_classes"].sum()),
            "recovery_max": int(g["recovery_classes"].max()),
            "at_risk": bool(g["at_risk"].any()),
        })
    out = pd.DataFrame(rows)
    out["status_rank"] = out["status"].map(STATUS_RANK)
    return out.sort_values(["risk_score", "status_rank", "overall_pct"],
                           ascending=[False, True, True]).reset_index(drop=True)


# ---------------------------------------------------------------- summaries


def _count(value: str):
    return lambda x: int((x == value).sum())


def department_summary(result: AnalysisResult) -> pd.DataFrame:
    s = result.students
    out = s.groupby("department").agg(
        students=("roll_no", "count"),
        critical=("status", _count(CRITICAL)),
        warning=("status", _count(WARNING)),
        safe=("status", _count(SAFE)),
        weak_marks=("weak_subjects", lambda x: int(x.map(bool).sum())),
        avg_attendance=("overall_pct", "mean"),
        avg_risk=("risk_score", "mean"),
    ).reset_index()
    out[["avg_attendance", "avg_risk"]] = out[["avg_attendance", "avg_risk"]].round(1)
    return out.sort_values(["critical", "avg_risk"], ascending=False).reset_index(drop=True)


def subject_summary(result: AnalysisResult) -> pd.DataFrame:
    d = result.subjects
    out = d.groupby("subject").agg(
        students=("roll_no", "nunique"),
        critical=("status", _count(CRITICAL)),
        warning=("status", _count(WARNING)),
        safe=("status", _count(SAFE)),
        weak_marks=("weak_marks", "sum"),
        falling_marks=("falling_marks", "sum"),
        avg_attendance=("attendance_pct", "mean"),
        avg_risk=("risk_score", "mean"),
    ).reset_index()
    out[["avg_attendance", "avg_risk"]] = out[["avg_attendance", "avg_risk"]].round(1)
    return out.sort_values(["critical", "avg_risk"], ascending=False).reset_index(drop=True)


def attendance_matrix(result: AnalysisResult) -> pd.DataFrame:
    """Average attendance %, subjects as rows and departments as columns."""
    return result.subjects.pivot_table(
        index="subject", columns="department", values="attendance_pct", aggfunc="mean").round(1)


def at_risk_table(result: AnalysisResult) -> pd.DataFrame:
    """Ranked (student, subject) rows that need attention, most at risk first."""
    d = result.subjects[result.subjects["at_risk"]].copy().reset_index(drop=True)
    d.insert(0, "rank", range(1, len(d) + 1))
    return d
