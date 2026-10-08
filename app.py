"""Attendance Guardian: early warning for attendance shortfalls and falling marks.

Run locally with:  streamlit run app.py
"""
from __future__ import annotations

import json
from collections import Counter

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

from core import ai_text, caller, notify, risk, slots
from core.config import load_settings

st.set_page_config(
    page_title="Attendance Guardian",
    page_icon=":material/verified_user:",
    layout="wide",
    initial_sidebar_state="expanded",
)

PAGES = ["Upload & Analyze", "Risk Dashboard", "Send Alerts", "Book Appointment", "Weekly Summary"]
PAGE_ALIASES = {"upload": 0, "analyze": 0, "dashboard": 1, "alerts": 2, "book": 3, "booking": 3, "summary": 4}

# Status palette (fixed, never reused for series). Always shown with its text label.
STATUS_COLORS = {"CRITICAL": "#d03b3b", "WARNING": "#fab219", "SAFE": "#0ca30c"}
STATUS_CELL = {
    "CRITICAL": "background-color: #fbe3e3; color: #a32020; font-weight: 600;",
    "WARNING": "background-color: #fff1cc; color: #7a5200; font-weight: 600;",
    "SAFE": "background-color: #e3f4e3; color: #0a6b0a; font-weight: 600;",
}
LOG_CELL = {
    "SENT": STATUS_CELL["SAFE"], "CALLED": STATUS_CELL["SAFE"], "FAILED": STATUS_CELL["CRITICAL"],
    "DEMO - would send": "background-color: #e6effb; color: #184f95; font-weight: 600;",
    "DEMO - would call": "background-color: #e6effb; color: #184f95; font-weight: 600;",
}
SLOT_CELL = {
    "free": STATUS_CELL["SAFE"],
    "busy": "background-color: #f0efec; color: #6b6a65;",
    "booked": "background-color: #dbe8fb; color: #184f95; font-weight: 600;",
}
BLUE_SCALE = [[0.0, "#cde2fb"], [0.25, "#9ec5f4"], [0.5, "#5598e7"], [0.75, "#256abf"], [1.0, "#0d366b"]]
STATUS_MD = {"CRITICAL": ":red-background[CRITICAL]", "WARNING": ":orange-background[WARNING]",
             "SAFE": ":green-background[SAFE]"}

st.markdown(
    """
    <style>
      .block-container {padding-top: 2.2rem; padding-bottom: 3rem;}
      h1 {font-weight: 700; letter-spacing: -0.01em;}
      [data-testid="stSidebar"] .brand {font-size: 1.25rem; font-weight: 700; margin-bottom: 0;}
      [data-testid="stSidebar"] .tagline {color: #6b7280; font-size: 0.85rem; margin-top: 0.1rem;}
      [data-testid="stMetricValue"] {font-weight: 700;}
    </style>
    """,
    unsafe_allow_html=True,
)


# ----------------------------------------------------------------- state


def init_state() -> None:
    ss = st.session_state
    if "data" not in ss:
        ss.data = risk.load_sample_data()
        ss.data_source = "Sample data (30 students, CSE and ECE)"
    ss.setdefault("log", [])
    ss.setdefault("bookings", slots.load_bookings())
    if "nav" not in ss:
        page = str(st.query_params.get("page", "")).lower()
        ss.nav = PAGES[PAGE_ALIASES.get(page, 0)]
    if "book_roll" not in ss:
        ss.book_roll = str(st.query_params.get("roll", "")).upper()
    ss.setdefault("force_demo", False)


def add_logs(rows: list[dict]) -> Counter:
    st.session_state.log.extend(rows)
    return Counter(r["status"] for r in rows)


def outcome_message(counts: Counter, noun: str) -> None:
    if not counts:
        st.info(f"No {noun} to process.")
        return
    parts = [f"{n} {status.lower()}" for status, n in counts.items()]
    text = f"{sum(counts.values())} {noun} processed: " + ", ".join(parts) + "."
    if counts.get("FAILED"):
        st.error(text + " See the delivery log for details.")
    elif any(s.startswith("DEMO") for s in counts):
        st.info(text + " Demo mode: nothing was actually delivered.")
    else:
        st.success(text)


def styled(df: pd.DataFrame, column: str, palette: dict[str, str]):
    styler = df.style
    fn = styler.map if hasattr(styler, "map") else styler.applymap
    styler = fn(lambda v: palette.get(v, ""), subset=[column])
    float_cols = [c for c in df.columns if pd.api.types.is_float_dtype(df[c])]
    return styler.format({c: "{:.1f}" for c in float_cols}, na_rep="-")


def subject_view(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame({
        "Roll no": df["roll_no"],
        "Name": df["name"],
        "Dept": df["department"],
        "Subject": df["subject"],
        "Held": df["classes_held"].astype(int),
        "Attended": df["classes_attended"].astype(int),
        "Attendance %": df["attendance_pct"].astype(float),
        "Status": df["status"],
        "Classes needed": df["recovery_classes"].astype(int),
        "Tests": df["marks_history"],
        "Trend": df["trend"],
        "Risk score": df["risk_score"].astype(float),
    })
    if "rank" in df.columns:
        out.insert(0, "Rank", df["rank"].astype(int))
    return out


def student_view(df: pd.DataFrame) -> pd.DataFrame:
    join = lambda items: ", ".join(items) if items else "-"  # noqa: E731
    return pd.DataFrame({
        "Roll no": df["roll_no"],
        "Name": df["name"],
        "Dept": df["department"],
        "Overall %": df["overall_pct"].astype(float),
        "Lowest subject %": df["min_subject_pct"].astype(float),
        "Status": df["status"],
        "Below threshold in": df["critical_subjects"].map(join),
        "Close to threshold in": df["warning_subjects"].map(join),
        "Weak marks": df["weak_subjects"].map(join),
        "Falling marks": df["falling_subjects"].map(join),
        "Classes needed": df["recovery_total"].astype(int),
        "Risk score": df["risk_score"].astype(float),
    })


COLUMN_CONFIG = {
    "Risk score": st.column_config.ProgressColumn(
        "Risk score", min_value=0, max_value=100, format="%.0f",
        help="0-100. 60% attendance shortfall, 40% marks weakness and trend."),
    "Classes needed": st.column_config.NumberColumn(
        "Classes needed", help="Consecutive classes to attend to reach the required attendance."),
    "Tests": st.column_config.TextColumn("Tests", help="Test 1 / Test 2 / Test 3 out of 100"),
    "Rank": st.column_config.NumberColumn("Rank", width="small"),
    "Dept": st.column_config.TextColumn("Dept", width="small"),
    "Held": st.column_config.NumberColumn("Held", width="small", help="Classes held"),
    "Attended": st.column_config.NumberColumn("Attended", width="small", help="Classes attended"),
}


def show_table(df: pd.DataFrame, column: str = "Status", palette: dict[str, str] = STATUS_CELL,
               height: int | None = None) -> None:
    kwargs = {"height": height} if height else {}
    st.dataframe(styled(df, column, palette), hide_index=True, width="stretch",
                 column_config=COLUMN_CONFIG, **kwargs)


def need_result(result, error) -> bool:
    if result is None:
        st.error(f"The current data could not be analysed: {error}")
        st.info("Go to Upload & Analyze to fix the files, or reload the sample data.")
        if st.button("Load sample data"):
            st.session_state.data = risk.load_sample_data()
            st.session_state.data_source = "Sample data (30 students, CSE and ECE)"
            st.rerun()
        return False
    return True


def section_header(title: str, caption: str | None = None) -> None:
    st.subheader(title)
    if caption:
        st.caption(caption)


# ----------------------------------------------------------------- sidebar


init_state()
ss = st.session_state

with st.sidebar:
    st.markdown('<p class="brand">Attendance Guardian</p>'
                '<p class="tagline">Early warning for attendance and marks</p>', unsafe_allow_html=True)
    st.radio("Navigate", PAGES, key="nav", label_visibility="collapsed",
             format_func=lambda p: f"{PAGES.index(p) + 1}.  {p}")
    st.divider()
    threshold = st.slider("Required attendance (%)", min_value=50, max_value=95, value=85, step=1,
                          key="threshold")
    margin = st.slider("Warning band above it (points)", min_value=1, max_value=10, value=3, step=1,
                       key="margin")
    st.caption(f"CRITICAL below {threshold}%  ·  WARNING {threshold}% to {threshold + margin}%  ·  "
               f"SAFE {threshold + margin}% and above")
    st.divider()
    st.toggle("Demo mode (never send)", key="force_demo",
              help="Simulate every email and call even if credentials are configured.")
    settings = load_settings(force_demo=ss.force_demo)
    st.caption(
        f"Email: **{'Live (Gmail)' if settings.email_live else 'Demo'}**  \n"
        f"Calls: **{'Live (Twilio)' if settings.calls_live else 'Demo'}**  \n"
        f"Email text: **{'Gemini + template fallback' if settings.ai_enabled else 'Template'}**")
    if settings.test_email or settings.test_phone:
        st.caption("Test overrides active: messages go to TEST_EMAIL / TEST_PHONE.")
    st.divider()
    st.caption(f"Data: {ss.data_source}")

try:
    result = risk.analyze(ss.data, threshold=threshold, margin=margin)
    analysis_error = None
except risk.DataValidationError as exc:
    result, analysis_error = None, str(exc)

page = ss.nav


# ----------------------------------------------------------------- page 1


def page_upload() -> None:
    st.title("Upload & Analyze")
    st.caption("Upload the attendance sheet and recent test results, or explore with the bundled sample data. "
               "Column names are matched case-insensitively.")

    with st.container(border=True):
        section_header("Data files")
        specs = {
            "students": "Students (required)",
            "attendance": "Attendance (required)",
            "marks": "Marks (required)",
            "teachers": "Teachers (optional)",
            "timetable": "Timetable (optional)",
        }
        uploads = {}
        cols = st.columns(3)
        for i, (name, label) in enumerate(specs.items()):
            needed = risk.REQUIRED_COLUMNS[name] + (["test1", "test2", "test3"] if name == "marks" else [])
            with cols[i % 3]:
                uploads[name] = st.file_uploader(label, type=["csv"], key=f"up_{name}",
                                                 help="Columns: " + ", ".join(needed))
        b1, b2, _ = st.columns([1.3, 1.1, 3])
        analyze_clicked = b1.button("Analyze uploaded files", type="primary", width="stretch")
        sample_clicked = b2.button("Use sample data", width="stretch")

        if sample_clicked:
            ss.data = risk.load_sample_data()
            ss.data_source = "Sample data (30 students, CSE and ECE)"
            st.success("Sample data loaded.")
            st.rerun()

        if analyze_clicked:
            missing = [specs[n] for n in ("students", "attendance", "marks") if uploads[n] is None]
            if missing:
                st.error("Please upload: " + ", ".join(missing) + ".")
            else:
                raw, errors, notes = {}, [], []
                sample = risk.load_sample_data()
                for name, file in uploads.items():
                    if file is None:
                        raw[name] = sample[name]
                        notes.append(f"No {name} file uploaded, so the sample {name} file is used.")
                        continue
                    try:
                        raw[name] = risk.read_csv(file)
                    except risk.DataValidationError as exc:
                        errors.append(f"{specs[name]}: {exc}")
                clean, validation_errors = risk.validate_all(raw) if not errors else ({}, [])
                errors += validation_errors
                if not errors:
                    try:
                        risk.analyze(clean, threshold, margin)
                    except risk.DataValidationError as exc:
                        errors.append(str(exc))
                if errors:
                    for e in errors:
                        st.error(e)
                else:
                    ss.data = clean
                    names = [f.name for f in uploads.values() if f is not None]
                    ss.data_source = "Uploaded: " + ", ".join(names)
                    for n in notes:
                        st.info(n)
                    st.success("Files validated and analysed.")
                    st.rerun()

        with st.expander("Download CSV templates (the sample files)"):
            tcols = st.columns(5)
            for i, name in enumerate(risk.REQUIRED_COLUMNS):
                path = risk.SAMPLE_DIR / f"{name}.csv"
                tcols[i].download_button(f"{name}.csv", path.read_bytes(), file_name=f"{name}.csv",
                                         mime="text/csv", key=f"tpl_{name}", width="stretch")

    if not need_result(result, analysis_error):
        return

    for w in result.warnings:
        st.warning(w)

    s = result.students
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Students analysed", len(s), border=True)
    c2.metric("Subject records", len(result.subjects), border=True)
    c3.metric("Critical", int((s["status"] == risk.CRITICAL).sum()), border=True)
    c4.metric("Need attention", int(s["at_risk"].sum()), border=True,
              help="Critical or warning attendance, or weak or falling marks.")

    st.markdown("")
    section_header("Risk table", f"Sorted most at risk first. Required attendance {threshold}%.")
    f1, f2, f3, f4 = st.columns([1.7, 1.1, 1.5, 0.9])
    statuses = f1.multiselect("Status", [risk.CRITICAL, risk.WARNING, risk.SAFE],
                              default=[risk.CRITICAL, risk.WARNING, risk.SAFE])
    depts = f2.multiselect("Department", sorted(s["department"].unique()))
    query = f3.text_input("Search name or roll no", placeholder="e.g. 22CSE001 or Aarav")
    view = f4.radio("View", ["By subject", "By student"], horizontal=True)

    if view == "By student":
        df = s[s["status"].isin(statuses)]
        if depts:
            df = df[df["department"].isin(depts)]
        if query:
            q = query.strip().lower()
            df = df[df["roll_no"].str.lower().str.contains(q, regex=False)
                    | df["name"].str.lower().str.contains(q, regex=False)]
        table = student_view(df)
    else:
        df = result.subjects[result.subjects["status"].isin(statuses)]
        if depts:
            df = df[df["department"].isin(depts)]
        if query:
            q = query.strip().lower()
            df = df[df["roll_no"].str.lower().str.contains(q, regex=False)
                    | df["name"].str.lower().str.contains(q, regex=False)]
        table = subject_view(df)
    show_table(table, height=520)
    st.download_button("Download risk report (CSV)", table.to_csv(index=False).encode("utf-8"),
                       file_name="attendance_risk_report.csv", mime="text/csv")


# ----------------------------------------------------------------- page 2


def _bar_layout(fig: go.Figure, title: str, xaxis_title: str) -> go.Figure:
    fig.update_layout(
        barmode="stack", bargap=0.45, barcornerradius=4,
        title=dict(text=title, x=0, xanchor="left", font=dict(size=15)),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1, title=None,
                    traceorder="normal"),
        margin=dict(l=10, r=10, t=60, b=10), height=360, hovermode="closest",
        xaxis=dict(title=xaxis_title, showgrid=False, tickangle=0),
        yaxis=dict(title="Count", gridcolor="rgba(128,128,128,0.18)", zeroline=False, rangemode="tozero"),
    )
    return fig


def status_bars(frame: pd.DataFrame, category: str, title: str, xaxis_title: str) -> go.Figure:
    fig = go.Figure()
    # Wrap long category names onto two lines so labels stay horizontal.
    labels = [str(c).replace(" ", "<br>", 1) if len(str(c)) > 12 else str(c) for c in frame[category]]
    for status in (risk.CRITICAL, risk.WARNING, risk.SAFE):
        fig.add_bar(
            x=labels, y=frame[status.lower()], name=status.title(),
            marker=dict(color=STATUS_COLORS[status], line=dict(color="#ffffff", width=1.5)),
            hovertemplate=f"<b>%{{x}}</b><br>{status.title()}: %{{y}}<extra></extra>",
        )
    return _bar_layout(fig, title, xaxis_title)


def heatmap(matrix: pd.DataFrame) -> go.Figure:
    z = matrix.to_numpy(dtype=float)
    finite = z[~pd.isna(z)]
    zmin = float(min(finite.min(), threshold)) - 2 if finite.size else 60
    fig = go.Figure(go.Heatmap(
        z=z, x=list(matrix.columns), y=list(matrix.index), colorscale=BLUE_SCALE, zmin=zmin, zmax=100,
        texttemplate="%{z:.1f}%", textfont=dict(size=14), xgap=3, ygap=3,
        colorbar=dict(title=dict(text="Avg %"), thickness=12),
        hovertemplate="<b>%{y}</b><br>%{x}: %{z:.1f}% average attendance<extra></extra>",
    ))
    fig.update_layout(
        title=dict(text="Average attendance by subject and department", x=0, xanchor="left", font=dict(size=15)),
        margin=dict(l=10, r=10, t=50, b=10), height=330,
        xaxis=dict(title="Department", side="bottom"), yaxis=dict(title=None, autorange="reversed"),
    )
    return fig


def page_dashboard() -> None:
    st.title("Risk Dashboard")
    st.caption(f"Department-wise and subject-wise risk at a required attendance of {threshold}%. "
               "Most at-risk students are listed first.")
    if not need_result(result, analysis_error):
        return

    f1, f2 = st.columns(2)
    depts = f1.multiselect("Department", sorted(result.students["department"].unique()),
                           placeholder="All departments")
    subjects = f2.multiselect("Subject", sorted(result.subjects["subject"].unique()), placeholder="All subjects")
    rolls = result.students["roll_no"]
    if depts:
        rolls = result.students.loc[result.students["department"].isin(depts), "roll_no"]
    view = result.subset(rolls)
    if subjects:
        view = view.for_subjects(subjects)
    if view.students.empty:
        st.info("No students match these filters.")
        return

    s = view.students
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Total students", len(s), border=True)
    k2.metric("Critical", int((s["status"] == risk.CRITICAL).sum()), border=True,
              help=f"Below {threshold}% in at least one subject.")
    k3.metric("Warning", int((s["status"] == risk.WARNING).sum()), border=True,
              help=f"Between {threshold}% and {threshold + margin}% in at least one subject.")
    k4.metric("Weak marks", int(s["weak_subjects"].map(bool).sum()), border=True,
              help="Latest test below 40 in at least one subject.")
    k5.metric("Falling marks", int(s["falling_subjects"].map(bool).sum()), border=True,
              help="Dropped 15+ points since the previous test, or fell across three tests.")

    c1, c2 = st.columns(2)
    with c1, st.container(border=True):
        dept = risk.department_summary(view)
        st.plotly_chart(status_bars(dept, "department", "Students by status, per department", "Department"),
                        width="stretch", config={"displayModeBar": False})
    with c2, st.container(border=True):
        subj = risk.subject_summary(view)
        st.plotly_chart(status_bars(subj, "subject", "Student-subject records by status, per subject", "Subject"),
                        width="stretch", config={"displayModeBar": False})

    h1, h2 = st.columns([1.1, 1])
    with h1, st.container(border=True):
        st.plotly_chart(heatmap(risk.attendance_matrix(view)), width="stretch", config={"displayModeBar": False})
    with h2, st.container(border=True):
        st.markdown("**Department summary**")
        d = dept[["department", "students", "critical", "warning", "weak_marks", "avg_attendance"]].rename(
            columns={"department": "Dept", "students": "Students", "critical": "Critical", "warning": "Warning",
                     "weak_marks": "Weak marks", "avg_attendance": "Avg %"})
        st.dataframe(d, hide_index=True, width="stretch")
        st.markdown("**Subject summary**")
        sj = subj[["subject", "critical", "warning", "weak_marks", "falling_marks", "avg_attendance"]].rename(
            columns={"subject": "Subject", "critical": "Critical", "warning": "Warning", "weak_marks": "Weak",
                     "falling_marks": "Falling", "avg_attendance": "Avg %"})
        st.dataframe(sj, hide_index=True, width="stretch")

    section_header("Ranked at-risk list",
                   "Critical or warning attendance, or weak or falling marks. "
                   "'Classes needed' is how many classes in a row the student must attend to recover.")
    tab1, tab2 = st.tabs(["By subject", "By student"])
    with tab1:
        show_table(subject_view(risk.at_risk_table(view)), height=460)
    with tab2:
        show_table(student_view(view.at_risk_students), height=460)


# ----------------------------------------------------------------- page 3


@st.cache_data(show_spinner=False, ttl=3600)
def cached_message(facts_json: str, use_ai: bool, ai_key: str, _settings) -> tuple[str, str]:
    """Cache personalised text per student so reruns do not call Gemini again."""
    return ai_text.personalised_message(_settings, json.loads(facts_json), use_ai)


def student_email(row: pd.Series, use_ai: bool) -> tuple[notify.Email, str]:
    subs = result.student_subjects(row["roll_no"])
    facts = ai_text.student_facts(row, subs, result.threshold)
    ai_key = f"{settings.gemini_model}:{bool(settings.gemini_api_key)}"
    message, source = cached_message(json.dumps(facts, default=float), use_ai, ai_key, settings)
    email = notify.student_warning_email(row, subs, result.threshold, message, settings.booking_link(row["roll_no"]))
    return email, source


def show_log() -> None:
    section_header("Delivery log", "Every email and call from this session, newest first.")
    if not ss.log:
        st.caption("Nothing sent yet.")
        return
    log = pd.DataFrame(ss.log).iloc[::-1].rename(columns=str.title)
    show_table(log, column="Status", palette=LOG_CELL, height=320)
    c1, c2, _ = st.columns([1, 1, 4])
    c1.download_button("Download log (CSV)", log.to_csv(index=False).encode("utf-8"), file_name="delivery_log.csv",
                       mime="text/csv", width="stretch")
    c2.button("Clear log", width="stretch", on_click=lambda: ss.update(log=[]))


def page_alerts() -> None:
    st.title("Send Alerts")
    st.caption("Warn at-risk students, alert their subject teachers and faculty advisers, "
               "and auto-call students below the required attendance.")
    if not need_result(result, analysis_error):
        return
    if not settings.email_live or not settings.calls_live:
        missing = [n for n, live in (("email", settings.email_live), ("calls", settings.calls_live)) if not live]
        st.info(f"Demo mode for {' and '.join(missing)}: nothing is delivered on those channels. "
                "Each message is logged as 'would send' or 'would call' instead. "
                "Add credentials in .streamlit/secrets.toml to go live.")
    if settings.test_email or settings.test_phone:
        st.warning("Test overrides are on. Emails go to TEST_EMAIL and calls go to TEST_PHONE.")

    at_risk = result.at_risk_students
    critical = result.below_threshold_students

    # Students -----------------------------------------------------------
    with st.container(border=True):
        section_header(f"1. Email at-risk students ({len(at_risk)})",
                       "Each email shows current attendance, classes needed in a row, weak subjects, "
                       "and a link to book a slot with the teacher.")
        use_ai = st.checkbox("Personalise the message with Gemini", value=settings.ai_enabled,
                             disabled=not settings.ai_enabled,
                             help="Set GEMINI_API_KEY to enable. Falls back to the template on any error.")
        if not at_risk.empty:
            options = at_risk["roll_no"].tolist()
            labels = dict(zip(at_risk["roll_no"], at_risk["name"] + "  (" + at_risk["roll_no"] + ", "
                              + at_risk["status"] + ")"))
            with st.expander("Preview a student email"):
                pick = st.selectbox("Student", options, format_func=labels.get, key="preview_student")
                row = at_risk[at_risk["roll_no"] == pick].iloc[0]
                with st.spinner("Writing the message..."):
                    email, source = student_email(row, use_ai)
                st.caption(f"To: {email.to}  ·  Subject: {email.subject}  ·  Text source: {source}")
                components.html(email.html, height=720, scrolling=True)
        if st.button(f"Email at-risk students ({len(at_risk)})", type="primary", disabled=at_risk.empty):
            emails, sources = [], Counter()
            progress = st.progress(0.0, text="Preparing emails...")
            for i, (_, row) in enumerate(at_risk.iterrows(), start=1):
                email, source = student_email(row, use_ai)
                emails.append(email)
                sources[source] += 1
                progress.progress(i / len(at_risk), text=f"Prepared {i} of {len(at_risk)}")
            progress.progress(1.0, text="Sending...")
            counts = add_logs(notify.send_emails(settings, emails))
            progress.empty()
            outcome_message(counts, "student emails")
            if sources.get("gemini"):
                st.caption(f"{sources['gemini']} message(s) written by Gemini, {sources.get('template', 0)} "
                           f"from the template.")

    # Staff ---------------------------------------------------------------
    with st.container(border=True):
        staff_emails, problems = notify.staff_alert_emails(result)
        n_teachers = sum(e.kind == "Teacher alert" for e in staff_emails)
        n_advisers = sum(e.kind == "Adviser alert" for e in staff_emails)
        section_header(f"2. Alert teachers and advisers ({n_teachers} teachers, {n_advisers} advisers)",
                       "Teachers get the at-risk students in their subject. Advisers get every at-risk advisee.")
        for p in problems:
            st.warning(p)
        if staff_emails:
            with st.expander("Preview a staff email"):
                idx = st.selectbox("Recipient", range(len(staff_emails)),
                                   format_func=lambda i: f"{staff_emails[i].kind}: {staff_emails[i].to}")
                components.html(staff_emails[idx].html, height=560, scrolling=True)
        if st.button("Alert teachers and advisers", type="primary", disabled=not staff_emails):
            with st.spinner("Sending..."):
                counts = add_logs(notify.send_emails(settings, staff_emails))
            outcome_message(counts, "staff emails")

    # Calls ---------------------------------------------------------------
    with st.container(border=True):
        section_header(f"3. Auto-call students below {threshold}% ({len(critical)})",
                       "An automated voice call reads out the subjects below the requirement "
                       "and how many classes in a row are needed.")
        if critical.empty:
            st.success(f"No student is below {threshold}% in any subject.")
        else:
            preview = pd.DataFrame({
                "Roll no": critical["roll_no"], "Name": critical["name"], "Phone": critical["phone"],
                "Below threshold in": critical["critical_subjects"].map(", ".join),
                "Classes needed": critical["recovery_total"],
            })
            st.dataframe(preview, hide_index=True, width="stretch")
            calls = []
            for _, row in critical.iterrows():
                facts = ai_text.student_facts(row, result.student_subjects(row["roll_no"]), result.threshold)
                calls.append({"name": row["name"], "phone": row["phone"], "message": caller.call_script(facts)})
            with st.expander("Preview the call script"):
                st.write(calls[0]["message"])
                st.code(caller.build_twiml(calls[0]["message"], settings.twilio_voice), language="xml")
            confirm = st.checkbox(f"I confirm: place automated calls to {len(calls)} students", key="confirm_calls")
            if st.button("Auto-call students below threshold", type="primary", disabled=not confirm):
                with st.spinner("Placing calls..."):
                    counts = add_logs(caller.place_calls(settings, calls))
                outcome_message(counts, "calls")

    show_log()


# ----------------------------------------------------------------- page 4


def book_selected_slot(student: dict, teacher_name: str, teacher_email: str, subject: str,
                       day: str, slot: str) -> None:
    """Button callback: runs before the page re-renders, so the grid shows the new booking at once."""
    timetable = risk.validate("timetable", ss.data.get("timetable"))
    bookings = slots.merge_bookings(slots.load_bookings(), ss.bookings)
    try:
        booking = slots.book_slot(timetable, bookings, student=student, teacher_name=teacher_name,
                                  teacher_email=teacher_email, subject=subject, day=day, slot=slot)
    except slots.SlotUnavailableError as exc:
        ss.booking_error = str(exc)
        return
    ss.bookings = slots.merge_bookings(ss.bookings, pd.DataFrame([booking]))
    if not slots.save_booking(booking):
        ss.log.append(notify.log_entry("Booking", booking["roll_no"], booking["booking_id"], "SESSION ONLY",
                                       "Could not write data/bookings.csv; booking kept for this session"))
    counts = add_logs(notify.send_emails(settings, notify.booking_confirmation_emails(booking)))
    ss.last_booking = {"booking": booking, "counts": counts}
    ss.pop("slot_choice", None)


def page_booking() -> None:
    st.title("Book Appointment")
    st.caption("Student view. Enter your roll number to see your at-risk subjects and book a free slot "
               "in your subject teacher's timetable.")
    if not need_result(result, analysis_error):
        return

    if "booking_error" in ss:
        st.error(ss.pop("booking_error"))
    last = ss.pop("last_booking", None)
    if last:
        b = last["booking"]
        st.success(f"Booked: {b['teacher_name']} on {b['day']}, {b['date']} at {b['slot']} "
                   f"for {b['subject']}. Booking ID {b['booking_id']}.")
        outcome_message(last["counts"], "confirmation emails")

    roll = st.text_input("Roll number", key="book_roll", placeholder="e.g. 22CSE001").strip().upper()
    if not roll:
        st.info("Try a sample roll number such as 22CSE001 or 22ECE011.")
        return
    student = result.student(roll)
    if student is None:
        st.error(f"Roll number {roll} was not found. Check the format, for example 22CSE001.")
        return

    subs = result.student_subjects(roll)
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
        c1.markdown(f"**{student['name']}**  \n{student['department']}  ·  Adviser: {student['adviser_name']}")
        c2.metric("Overall attendance", f"{student['overall_pct']:.1f}%")
        c3.metric("Classes needed", int(student["recovery_total"]), help="Sum across subjects below the threshold.")
        c4.markdown(f"Status  \n{STATUS_MD.get(student['status'], student['status'])}")
        show_table(subject_view(subs).drop(columns=["Roll no", "Name", "Dept"]))

    at_risk_subjects = subs.loc[subs["at_risk"], "subject"].tolist()
    if at_risk_subjects:
        st.markdown(f"Subjects that need attention: **{', '.join(at_risk_subjects)}**")
        subject_options = at_risk_subjects + [s for s in subs["subject"] if s not in at_risk_subjects]
    else:
        st.success("You are on track in every subject. You can still book a slot if you want help.")
        subject_options = subs["subject"].tolist()

    teachers = risk.validate("teachers", ss.data.get("teachers"))
    timetable = risk.validate("timetable", ss.data.get("timetable"))
    bookings = slots.merge_bookings(slots.load_bookings(), ss.bookings)

    with st.container(border=True):
        section_header("Choose a slot")
        c1, c2 = st.columns(2)
        subject = c1.selectbox("Subject", subject_options)
        options = teachers[teachers["subject"].str.strip().str.lower() == subject.strip().lower()]
        if options.empty:
            st.error(f"No teacher is listed for {subject} in the teachers file.")
            return
        teacher_name = c2.selectbox("Teacher", options["teacher_name"].tolist())
        teacher_email = options.loc[options["teacher_name"] == teacher_name, "email"].iloc[0]

        grid = slots.teacher_week_grid(timetable, teacher_name, bookings)
        if grid.empty:
            st.warning(f"No timetable found for {teacher_name}.")
            return
        st.markdown(f"**{teacher_name}'s week**  (free, busy, booked)")
        grid_display = grid.reset_index().rename(columns={"day": "Day"})
        styler = grid_display.style
        fn = styler.map if hasattr(styler, "map") else styler.applymap
        st.dataframe(fn(lambda v: SLOT_CELL.get(v, ""), subset=list(grid.columns)), hide_index=True, width="stretch")

        free = slots.free_slots(timetable, teacher_name, bookings)
        if free.empty:
            st.warning(f"{teacher_name} has no free slots left this week. Please contact the department office.")
            return
        if ss.get("slot_choice") not in set(free["label"]):
            ss.pop("slot_choice", None)  # the previously chosen slot is no longer free
        label = st.selectbox("Free slot", free["label"].tolist(), key="slot_choice")
        chosen = free[free["label"] == label].iloc[0]
        st.button("Book this slot", type="primary", on_click=book_selected_slot, kwargs=dict(
            student=student.to_dict(), teacher_name=teacher_name, teacher_email=teacher_email,
            subject=subject, day=chosen["day"], slot=chosen["slot"]))

    mine = bookings[bookings["roll_no"] == roll] if not bookings.empty else bookings
    if not mine.empty:
        section_header("Your bookings")
        st.dataframe(mine[["booking_id", "subject", "teacher_name", "day", "date", "slot", "booked_at"]].rename(
            columns=lambda c: c.replace("_", " ").title()), hide_index=True, width="stretch")


# ----------------------------------------------------------------- page 5


def page_summary() -> None:
    st.title("Weekly Summary")
    st.caption("GitHub Actions emails this summary to every faculty adviser each Monday at 9:00 AM IST "
               "(scripts/weekly_summary.py). Preview it here or send it now.")
    if not need_result(result, analysis_error):
        return

    groups = notify.adviser_groups(result)
    choices = ["All departments"] + [f"{name} ({email})" for name, email, _ in groups]
    pick = st.selectbox("Preview for", choices)
    if pick == "All departments":
        view, audience = result, "Faculty"
    else:
        name, _, view = groups[choices.index(pick) - 1]
        audience = name
    summary_html = notify.weekly_summary_html(view, audience=audience, app_url=settings.app_url)

    with st.container(border=True):
        section_header("Send now")
        mode = st.radio("Recipients", ["Each faculty adviser (their own advisees)", "One address (all departments)"],
                        horizontal=True)
        address = ""
        if mode.startswith("One"):
            address = st.text_input("Email address", placeholder="hod@college.edu")
        c1, c2, _ = st.columns([1, 1.2, 3])
        if c1.button("Send now", type="primary", width="stretch"):
            if mode.startswith("Each"):
                emails = [notify.weekly_summary_email(sub, email, name, settings.app_url) for name, email, sub in groups]
            elif not notify._valid_address(address):
                emails = []
                st.error("Enter a valid email address.")
            else:
                emails = [notify.weekly_summary_email(result, address.strip(), "Faculty", settings.app_url)]
            if emails:
                with st.spinner("Sending..."):
                    counts = add_logs(notify.send_emails(settings, emails))
                outcome_message(counts, "summary emails")
        c2.download_button("Download preview (HTML)", summary_html.encode("utf-8"), file_name="weekly_summary.html",
                           mime="text/html", width="stretch")

    section_header("Preview")
    components.html(summary_html, height=1100, scrolling=True)


ROUTES = {
    "Upload & Analyze": page_upload,
    "Risk Dashboard": page_dashboard,
    "Send Alerts": page_alerts,
    "Book Appointment": page_booking,
    "Weekly Summary": page_summary,
}
ROUTES[page]()
