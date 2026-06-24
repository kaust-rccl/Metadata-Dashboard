"""
components/modlog_viewer.py
---------------------------
Renders an ArtifactModLog as a read-only timeline.
No editing — the log is append-only.
"""

from __future__ import annotations

import streamlit as st

from config import Action


_ACTION_COLORS = {
    Action.STAGED:   "#1D9E75",
    Action.MOVED:    "#534AB7",
    Action.BLOCKED:  "#BA7517",
    Action.REJECTED: "#A32D2D",
    Action.UPDATED:  "#5F5E5A",
}


def render_modlog(modlog) -> None:
    """
    Render all entries in a modlog as a vertical timeline.
    Most recent entry first.
    """
    if modlog is None or not modlog.entries:
        st.caption("No modification log entries.")
        return

    for entry in reversed(modlog.entries):
        color = _ACTION_COLORS.get(entry.action, "#888")
        ts = (
            entry.timestamp.strftime("%Y-%m-%d %H:%M UTC")
            if entry.timestamp else "—"
        )
        note_html = (
            f"<div style='color:#555;font-size:0.88em;margin-top:3px'>"
            f"{entry.note}</div>"
            if entry.note else ""
        )
        st.markdown(
            f"<div style='"
            f"border-left:3px solid {color};"
            f"padding:6px 12px;"
            f"margin-bottom:8px;"
            f"background:var(--background-color)'>"
            f"<span style='font-weight:600;color:{color}'>"
            f"{entry.action.value.upper()}</span>"
            f" &nbsp;·&nbsp; <b>{entry.actor}</b>"
            f" <span style='color:#888;font-size:0.85em'>({entry.role.value})</span>"
            f" &nbsp;·&nbsp; <span style='color:#888;font-size:0.85em'>{ts}</span>"
            f"{note_html}"
            f"</div>",
            unsafe_allow_html=True,
        )
