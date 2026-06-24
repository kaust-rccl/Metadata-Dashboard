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
    if role not in allowed:
        st.error(
            f"Access denied. This page requires one of: "
            f"{', '.join(r.value for r in allowed)}. "
            f"Your role is: **{role.value}**."
        )
        return False
    return True
