"""
auth/session.py
---------------
Streamlit session state helpers.

Role and username are resolved once per session on first load
and stored in st.session_state. Every page calls require_role()
to guard access — unknown or insufficient roles get a hard stop
rendered in the UI.

To add session-scoped state for a new feature, add a key to
SESSION_DEFAULTS and call init_session() at the top of the page.
"""

from __future__ import annotations

import streamlit as st

from auth.ldap import get_current_username, resolve_role
from config import Role


# Keys stored in session_state
_KEY_USERNAME = "username"
_KEY_ROLE     = "role"
_KEY_DB       = "db"

# Pipeline selector.
# Streamlit discards widget state when the widget is not re-instantiated on
# the page being rendered, which is exactly what happens when the user
# navigates between pages. The radio's own key is therefore treated as
# scratch state; _KEY_PIPELINE holds the value that survives navigation.
_KEY_PIPELINE        = "pipeline"
_KEY_PIPELINE_WIDGET = "pipeline_selector"
_PIPELINES           = ["HPC-AI", "HPC"]


def init_session(db=None) -> None:
    """
    Initialise session state on first load.
    Safe to call on every page — idempotent after first run.
    Creates the Database instance if not already present.
    """
    if _KEY_DB not in st.session_state:
        if db is not None:
            st.session_state[_KEY_DB] = db
        else:
            from iolib.database import Database
            st.session_state[_KEY_DB] = Database()

    if _KEY_USERNAME not in st.session_state:
        username = get_current_username()
        role     = resolve_role(username)
        st.session_state[_KEY_USERNAME] = username
        st.session_state[_KEY_ROLE]     = role
    _render_sidebar() 

def current_username() -> str:
    return st.session_state.get(_KEY_USERNAME, "unknown")


def current_role() -> Role:
    return st.session_state.get(_KEY_ROLE, Role.READONLY)


def current_db():
    """Return the shared Database instance for this session."""
    return st.session_state.get(_KEY_DB)


def require_role(*allowed: Role) -> bool:
    """
    Render an access-denied message and return False if the current
    role is not in allowed. Returns True if access is granted.

    Usage:
        if not require_role(Role.CURATOR):
            st.stop()
    """
    role = current_role()
    if role == Role.ADMIN:
        return True
    if role not in allowed:
        st.error(
            f"Access denied. This page requires one of: "
            f"{', '.join(r.value for r in allowed)}. "
            f"Your role is: **{role.value}**."
        )
        return False
    return True


def is_admin() -> bool:
    """True if the current session belongs to a superuser."""
    return current_role() == Role.ADMIN


def current_pipeline() -> str:
    """Return the selected data movement pipeline: 'HPC-AI' or 'HPC'."""
    return st.session_state.get(_KEY_PIPELINE, _PIPELINES[0])


def _on_pipeline_change() -> None:
    """Persist the radio's value into non-widget state before the rerun."""
    st.session_state[_KEY_PIPELINE] = st.session_state[_KEY_PIPELINE_WIDGET]


def _render_sidebar() -> None:
    """Inject sidebar chrome — called from init_session so every page gets it."""
    import os

    # hide Streamlit's auto-generated page list
    st.markdown("""
    <style>
    [data-testid="stSidebarNav"] { display: none; }
    </style>
    """, unsafe_allow_html=True)

    # pipeline selector — value persisted across page navigation
    if _KEY_PIPELINE not in st.session_state:
        st.session_state[_KEY_PIPELINE] = _PIPELINES[0]

    # Seed the widget from persistent state. On a fresh page the widget key is
    # gone, so this restores the selection; on a rerun of the same page the
    # on_change callback has already written the new value here, so this is a
    # no-op rather than a revert.
    st.session_state[_KEY_PIPELINE_WIDGET] = st.session_state[_KEY_PIPELINE]

    st.sidebar.radio(
        "Data movement pipeline",
        _PIPELINES,
        key=_KEY_PIPELINE_WIDGET,
        on_change=_on_pipeline_change,
    )
    pipeline = st.session_state[_KEY_PIPELINE]

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

    st.sidebar.markdown("---")
