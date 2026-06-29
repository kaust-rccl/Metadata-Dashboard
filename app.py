"""
app.py
------
Entry point for the Streamlit dashboard.

Responsibilities (and nothing more):
- Open the shared Database connection once per session
- Run startup reconciliation to heal any YML↔DB drift
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
from iolib.fs import list_staged_artifacts


st.set_page_config(
    page_title="Artifact Tracker — Shaheen3",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _startup(db: Database) -> None:
    """
    One-time-per-session startup tasks.
    Currently: reconcile the DB mirror against the YML files on disk so any
    artifact whose DB upsert previously failed (see services._sync_db) gets
    picked back up automatically instead of staying invisible in the UI.
    """
    yml_paths = list_staged_artifacts(PROJECTS_BASE)
    updated, skipped = db.reconcile_from_ymls(yml_paths)
    st.session_state["_reconcile_result"] = (updated, skipped)


def _sidebar_identity() -> None:
    """Render the signed-in user chip in the sidebar."""
    username = current_username()
    role     = current_role()

    role_colors = {
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

def _sidebar_pipeline() -> None:
    st.sidebar.markdown("---")
    
    pipeline = st.sidebar.radio(
        "Data movement pipeline",
        ["HPC-AI", "HPC"],
        key="pipeline_selector",
    )
    
    st.sidebar.markdown("---")
    
    if pipeline == "HPC-AI":
        st.sidebar.markdown("**HPC-AI pipeline**")
        st.sidebar.page_link("pages/1_curate.py",  label="Curate",  icon="✏️")
        st.sidebar.page_link("pages/2_move.py",    label="Move",    icon="🚚")
        st.sidebar.page_link("pages/3_browse.py",  label="Browse",  icon="🔍")
        st.sidebar.page_link("pages/4_import.py",  label="Import",  icon="📥")
    else:
        st.sidebar.markdown("**HPC pipeline**")
        st.sidebar.page_link("pages/5_hpc_transfer.py", label="HPC Transfer", icon="📦")

def main() -> None:
    # One Database instance per session stored in session_state
    first_load = "db" not in st.session_state
    if first_load:
        st.session_state["db"] = Database()

    db: Database = st.session_state["db"]

    if first_load:
        _startup(db)

    init_session(db)
    # _sidebar_identity()
    _sidebar_pipeline() 

    updated, skipped = st.session_state.get("_reconcile_result", (0, []))
    if skipped:
        st.sidebar.warning(
            f"{len(skipped)} artifact YML(s) could not be reconciled to the "
            "DB at startup. Check server logs.",
            icon="⚠️",
        )

    # Navigation hint — Streamlit renders pages/ automatically
    st.sidebar.markdown("---")
    st.sidebar.caption("Navigate using the pages above.")

    # Landing page content
    st.title("Artifact Tracker")
    st.markdown(
        "Welcome to the Shaheen3 artifact tracking dashboard.  \n"
        "Use the sidebar to navigate between pages."
    )

    role = current_role()
    col1, col2, col3 = st.columns(3)

    with col1:
        db_rows = db.list_artifacts()
        st.metric("Total artifacts", len(db_rows))
    with col2:
        staged = db.list_artifacts(status="staged")
        st.metric("Staged", len(staged))
    with col3:
        moved = db.list_artifacts(status="moved")
        st.metric("Moved", len(moved))

    if role == Role.READONLY:
        st.info(
            "You have read-only access. "
            "Contact your system administrator to request curator or mover access."
        )


main()