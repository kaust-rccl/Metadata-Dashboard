"""
app.py
------
Entry point for the Streamlit dashboard.

Responsibilities (and nothing more):
- Open the shared Database connection once per session
- Run the two-way YML ↔ DB sync once per session (iolib.sync)
- Initialise session state (username, role)
- Render the sidebar identity chip
- Streamlit handles page routing via pages/ directory

To add a new page: create pages/N_name.py — no changes needed here.
To add a new startup task: add a function and call it in _startup().
"""

from __future__ import annotations

import streamlit as st

from auth.session import current_role, current_username, init_session
from config import PROJECTS_BASE, Role
from iolib.database import Database
from iolib.sync import SyncReport, sync


st.set_page_config(
    page_title="Artifact Tracker — Shaheen3",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _startup(db: Database) -> None:
    """
    One-time-per-session startup tasks.
    Currently: two-way sync between artifact YMLs and the DB — YMLs missing
    from the DB are mirrored in, DB rows with no YML get one written at the
    canonical path. YAML files that are not ours are never parsed.
    A failure here must never stop the app from loading.
    """
    st.session_state["_db_problems"] = db.quick_check()

    try:
        report = sync(db, PROJECTS_BASE, actor="SYSTEM")
    except Exception as exc:   # defensive — sync collects per-record errors itself
        report = SyncReport(errors=[f"sync aborted: {exc}"])
    st.session_state["_sync_report"] = report

    # Checksum jobs that finished while nobody had the record open: tick the
    # CPUP checksum box for every one that verified all files OK.
    try:
        from iolib.checksum_jobs import refresh_pending
        st.session_state["_checksum_applied"] = refresh_pending(db)
    except Exception as exc:
        st.session_state["_checksum_applied"] = [f"checksum refresh failed: {exc}"]


def _sidebar_db_health() -> None:
    problems = st.session_state.get("_db_problems") or []
    if problems:
        st.sidebar.error(
            "**The tracker database is corrupt** — listings may show rows twice and "
            "sync is paused. Stop all dashboard/CLI processes and rebuild it "
            "(docs/db_recovery.md).",
            icon="🛑",
        )
        with st.sidebar.expander("integrity details"):
            for line in problems[:20]:
                st.caption(line)


def _sidebar_checksum_status() -> None:
    applied = st.session_state.get("_checksum_applied") or []
    if applied:
        st.sidebar.success(f"{len(applied)} checksum job(s) verified since last visit.")
        with st.sidebar.expander("Checksum details"):
            for line in applied:
                st.caption(line)


def _sidebar_sync_status() -> None:
    report: SyncReport | None = st.session_state.get("_sync_report")
    if report is None:
        return
    if report.locked:
        st.sidebar.caption("Sync skipped — another session was already syncing.")
        return
    if report.yml_created or report.db_inserted:
        st.sidebar.caption(
            f"Sync: {len(report.yml_created)} YML(s) recreated from DB, "
            f"{len(report.db_inserted)} DB record(s) added from YML."
        )
    if report.needs_attention:
        dup_records = sum(1 for d in report.duplicates if not d.startswith("  "))
        st.sidebar.warning(
            f"Sync: {dup_records} duplicate group(s) split, "
            f"{len(report.conflicts)} conflict(s), {len(report.errors)} "
            "error(s) need attention.",
            icon="⚠️",
        )
        with st.sidebar.expander("Sync details"):
            for line in report.duplicates + report.conflicts + report.errors:
                st.caption(line)
    if report.deletes_completed:
        st.sidebar.caption(
            f"Sync finished {len(report.deletes_completed)} interrupted delete(s)."
        )
    if report.ignored:
        st.sidebar.caption(
            f"{len(report.ignored)} non-metadata YML file(s) in the projects "
            "tree were ignored."
        )


def _sidebar_identity() -> None:
    """Render the signed-in user chip in the sidebar."""
    username = current_username()
    role     = current_role()

    role_colors = {
        Role.ADMIN:    "#C2410C",
        Role.CURATOR:  "#1D9E75",
        Role.MOVER:    "#534AB7",
        Role.READONLY: "#5F5E5A",
    }
    color = role_colors.get(role, "#888")

    st.sidebar.markdown(
        f"<div style='"
        f"border:1px solid {color};"
        f"border-radius:8px;"
        f"padding:10px 14px;"
        f"margin-bottom:16px'>"
        f"<div style='font-size:0.8em;color:#888'>Signed in as</div>"
        f"<div style='font-weight:600'>{username}</div>"
        f"<div style='font-size:0.8em;color:{color};"
        f"font-weight:600;margin-top:2px'>{role.value.upper()}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

def main() -> None:
    # One Database instance per session stored in session_state
    first_load = "db" not in st.session_state
    if first_load:
        st.session_state["db"] = Database()

    db: Database = st.session_state["db"]

    if first_load:
        _startup(db)

    init_session(db)   # renders the sidebar (identity + pipeline selector)

    _sidebar_db_health()
    _sidebar_sync_status()
    _sidebar_checksum_status()

    # Navigation hint — links are rendered by auth.session._render_sidebar
    st.sidebar.caption("Navigate using the pages above.")

    # Landing page content
    st.title("Artifact Tracker")
    st.markdown(
        "Welcome to the Shaheen3 artifact tracking dashboard.  \n"
        "Use the sidebar to navigate between pages."
    )

    role = current_role()
    col1, col2, col3 = st.columns(3)
    try:
        all_rows = db.list_artifacts()
        unique   = {r["_rowid"]: r for r in all_rows}.values()   # corruption-safe
        with col1:
            st.metric("Total artifacts", len(unique))
        with col2:
            st.metric("Staged", sum(1 for r in unique if r["status"] == "staged"))
        with col3:
            st.metric("Moved", sum(1 for r in unique if r["status"] == "moved"))
    except Exception as exc:
        st.error(f"Could not read the artifact counts: {exc}. See docs/db_recovery.md.")

    if role == Role.ADMIN:
        st.info("Superuser session — all pages and both pipelines are available.")

    if role == Role.READONLY:
        st.info(
            "You have read-only access. "
            "Contact your system administrator to request curator or mover access."
        )


main()