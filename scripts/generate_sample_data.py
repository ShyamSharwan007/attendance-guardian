"""Regenerate the demo CSVs in sample_data/.

The data is seeded and hand-profiled so the demo always shows a mix of
critical, warning, falling-marks, weak-marks and safe students.

    python scripts/generate_sample_data.py
"""
from __future__ import annotations

import math
import random
from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parents[1] / "sample_data"
SEED = 42

SUBJECTS = ["Engineering Mathematics", "Data Structures", "Digital Electronics", "Computer Networks"]
TEACHERS = [
    ("Prof. Anitha Ramesh", "anitha.ramesh@faculty.example.edu", "Engineering Mathematics"),
    ("Prof. Suresh Babu", "suresh.babu@faculty.example.edu", "Data Structures"),
    ("Prof. Farhan Qureshi", "farhan.qureshi@faculty.example.edu", "Digital Electronics"),
    ("Prof. Lavanya Krishnan", "lavanya.krishnan@faculty.example.edu", "Computer Networks"),
]
ADVISERS = {
    "CSE": ("Dr. Meenakshi Sundaram", "meenakshi.sundaram@faculty.example.edu"),
    "ECE": ("Dr. Rajesh Iyengar", "rajesh.iyengar@faculty.example.edu"),
}
CLASSES_HELD = {
    "CSE": {"Engineering Mathematics": 42, "Data Structures": 40, "Digital Electronics": 36, "Computer Networks": 38},
    "ECE": {"Engineering Mathematics": 40, "Data Structures": 38, "Digital Electronics": 42, "Computer Networks": 36},
}
M, DS, DE, CN = SUBJECTS

# (name, {subject: attendance target}, {subject: marks pattern})
# Unlisted subjects get a healthy 90-98% attendance and "good" marks.
PROFILES = {
    "CSE": [
        ("Aarav Sharma", {DS: 0.70, CN: 0.78}, {DS: "weak_falling"}),
        ("Ananya Iyer", {}, {}),
        ("Rohan Mehta", {M: 0.86}, {M: "falling_drop"}),
        ("Priya Nair", {}, {}),
        ("Karthik Reddy", {DE: 0.80}, {}),
        ("Sneha Kulkarni", {}, {CN: "falling_steady"}),
        ("Vikram Singh", {M: 0.74, DS: 0.78, DE: 0.80, CN: 0.76}, {M: "weak"}),
        ("Diya Menon", {}, {}),
        ("Arjun Patel", {DS: 0.87, CN: 0.86}, {}),
        ("Kavya Rao", {}, {}),
        ("Siddharth Joshi", {M: 0.83}, {M: "falling_steady"}),
        ("Meera Pillai", {}, {}),
        ("Rahul Verma", {DE: 0.865}, {DE: "weak"}),
        ("Ishita Das", {}, {}),
        ("Nikhil Gupta", {}, {CN: "weak"}),
    ],
    "ECE": [
        ("Aditya Kumar", {DE: 0.65}, {DE: "falling_drop"}),
        ("Pooja Hegde", {}, {}),
        ("Varun Krishnan", {M: 0.855, DS: 0.87}, {}),
        ("Neha Agarwal", {}, {}),
        ("Harsh Vardhan", {CN: 0.78, DS: 0.82}, {CN: "weak"}),
        ("Lakshmi Subramanian", {}, {}),
        ("Manoj Bhat", {DE: 0.86}, {DE: "falling_steady"}),
        ("Riya Chatterjee", {}, {M: "falling_drop"}),
        ("Sanjay Prakash", {M: 0.75}, {}),
        ("Tanvi Deshpande", {}, {}),
        ("Yash Malhotra", {M: 0.72, DS: 0.70, DE: 0.78, CN: 0.80}, {DS: "weak_falling", M: "weak"}),
        ("Divya Shetty", {}, {}),
        ("Abhishek Mishra", {CN: 0.87}, {}),
        ("Swathi Raman", {}, {}),
        ("Kiran Naidu", {DS: 0.84}, {}),
    ],
}

MARK_PATTERNS = {
    "falling_drop": lambda r: (r.randint(70, 78), r.randint(68, 74), r.randint(46, 52)),
    "falling_steady": lambda r: (r.randint(70, 74), r.randint(62, 66), r.randint(54, 58)),
    "weak": lambda r: (r.randint(34, 38), r.randint(30, 33), r.randint(35, 38)),
    "weak_falling": lambda r: (r.randint(56, 60), r.randint(44, 48), r.randint(28, 33)),
}


def good_marks(r: random.Random) -> tuple[int, int, int]:
    base = r.randint(58, 86)
    t1 = base + r.randint(-5, 5)
    t2 = base + r.randint(-4, 6)
    t3 = max(t2, base + r.randint(-2, 8))  # never strictly falling
    return tuple(min(98, max(45, t)) for t in (t1, t2, t3))


def attended_for(target: float, held: int) -> int:
    return min(held, int(math.floor(target * held + 0.5)))


def main() -> None:
    r = random.Random(SEED)
    OUT.mkdir(exist_ok=True)
    students, attendance, marks = [], [], []
    for dept, rows in PROFILES.items():
        adviser_name, adviser_email = ADVISERS[dept]
        for i, (name, att_over, marks_over) in enumerate(rows, start=1):
            roll = f"22{dept}{i:03d}"
            first, last = name.lower().split(" ", 1)
            students.append({
                "roll_no": roll,
                "name": name,
                "department": dept,
                "email": f"{first}.{last.replace(' ', '')}@student.example.edu",
                # Deliberately invalid numbers (Indian mobiles never start with 0),
                # so a live Twilio account can never ring a real person by accident.
                "phone": f"+910000{1 if dept == 'CSE' else 2}{i:05d}",
                "adviser_name": adviser_name,
                "adviser_email": adviser_email,
            })
            for subject in SUBJECTS:
                held = CLASSES_HELD[dept][subject]
                target = att_over.get(subject, r.uniform(0.90, 0.98))
                attendance.append({
                    "roll_no": roll, "subject": subject,
                    "classes_held": held, "classes_attended": attended_for(target, held),
                })
                pattern = marks_over.get(subject)
                t1, t2, t3 = MARK_PATTERNS[pattern](r) if pattern else good_marks(r)
                marks.append({"roll_no": roll, "subject": subject, "test1": t1, "test2": t2, "test3": t3})

    teachers = [{"teacher_name": n, "email": e, "subject": s} for n, e, s in TEACHERS]
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    slots = ["09:00-10:00", "10:00-11:00", "11:15-12:15", "12:15-13:15", "14:00-15:00", "15:00-16:00"]
    timetable = []
    for name, _, _ in TEACHERS:
        for day in days:
            free_idx = set(r.sample(range(len(slots)), k=r.choice([1, 2, 2, 3])))
            for j, slot in enumerate(slots):
                timetable.append({"teacher_name": name, "day": day, "slot": slot,
                                  "status": "free" if j in free_idx else "busy"})

    pd.DataFrame(students).to_csv(OUT / "students.csv", index=False)
    pd.DataFrame(attendance).to_csv(OUT / "attendance.csv", index=False)
    pd.DataFrame(marks).to_csv(OUT / "marks.csv", index=False)
    pd.DataFrame(teachers).to_csv(OUT / "teachers.csv", index=False)
    pd.DataFrame(timetable).to_csv(OUT / "timetable.csv", index=False)
    print(f"Wrote sample data to {OUT}")


if __name__ == "__main__":
    main()
