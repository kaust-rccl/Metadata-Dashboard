"""
components/fields.py
--------------------
Reusable field renderers.

The core rule: every field goes through render_field().
If the field name is in the editable set → widget.
If not → st.text() styled as read-only.

This is the single place that enforces field locking in the UI.
The model layer enforces it again on write — defence in depth.
"""

from __future__ import annotations

from typing import Any, Optional

import streamlit as st

from config import (
    ArtifactType, ChecksumSource, ChecksumType,
    Partition, Status,
)


def render_field(
    label: str,
    value: Any,
    editable: bool,
    field_key: str,
    help: str = "",
    options: Optional[list] = None,
) -> Any:
    """
    Render a single field as either an editable widget or a read-only display.

    Returns the current value (widget output if editable, original if locked).
    field_key must be unique within the form — used as st widget key.
    """
    if not editable:
        st.text_input(
            label,
            value=str(value) if value is not None else "",
            disabled=True,
            key=f"ro_{field_key}",
            help=help or "This field is locked.",
        )
        return value

    if options is not None:
        idx = options.index(value) if value in options else 0
        return st.selectbox(label, options, index=idx, key=field_key, help=help)

    if isinstance(value, bool):
        return st.checkbox(label, value=value, key=field_key, help=help)

    if isinstance(value, float):
        return st.number_input(
            label, value=value, min_value=0.0,
            step=0.1, format="%.3f", key=field_key, help=help
        )

    return st.text_input(label, value=str(value or ""), key=field_key, help=help)


def render_text_area(
    label: str,
    value: str,
    editable: bool,
    field_key: str,
    help: str = "",
    height: int = 100,
) -> str:
    """Render a text area — used for notes fields."""
    if not editable:
        st.text_area(
            label,
            value=value or "",
            disabled=True,
            key=f"ro_{field_key}",
            height=height,
            help=help or "This field is locked.",
        )
        return value
    return st.text_area(
        label, value=value or "", key=field_key,
        height=height, help=help
    )


def render_checksum_verification(
    label: str,
    verification,
    editable: bool,
    field_key: str,
) -> dict:
    """
    Render a ChecksumVerification block as three sub-fields.
    Returns a dict with verified/verified_by/verified_date keys.
    """
    st.markdown(f"**{label}**")
    col1, col2 = st.columns(2)

    with col1:
        verified = render_field(
            "Verified", verification.verified, editable,
            f"{field_key}_verified",
            help="Was the checksum verified on this partition?",
        )
    with col2:
        verified_by = render_field(
            "Verified by", verification.verified_by, editable,
            f"{field_key}_verified_by",
            help="Username of who ran the verification.",
        )

    return {
        "verified": verified,
        "verified_by": verified_by,
        "verified_date": verification.verified_date,
    }


def render_status_reason_list(label: str, entries: list) -> None:
    """
    Render a status_reason list as a read-only timeline.
    Append-only — no editing widget.
    """
    st.markdown(f"**{label}**")
    if not entries:
        st.caption("No entries.")
        return
    for entry in entries:
        ts = entry.timestamp.strftime("%Y-%m-%d %H:%M UTC") if entry.timestamp else ""
        st.markdown(
            f"<div style='border-left:3px solid var(--primary-color);"
            f"padding:4px 10px;margin-bottom:6px;font-size:0.9em'>"
            f"<b>{entry.actor}</b> · {ts}<br/>{entry.reason}"
            f"</div>",
            unsafe_allow_html=True,
        )


def section_header(title: str, color: str = "#1D9E75") -> None:
    """Render a colored section divider with title."""
    st.markdown(
        f"<div style='border-top:2px solid {color};"
        f"padding-top:8px;margin:18px 0 10px;"
        f"font-size:0.85em;font-weight:600;color:{color};'>"
        f"{title.upper()}</div>",
        unsafe_allow_html=True,
    )


def status_badge(status: Status) -> str:
    """Return an HTML badge for a status value."""
    colors = {
        Status.STAGED:   ("#1D9E75", "#E1F5EE"),
        Status.MOVED:    ("#534AB7", "#EEEDFE"),
        Status.BLOCKED:  ("#BA7517", "#FAEEDA"),
        Status.REJECTED: ("#A32D2D", "#FCEBEB"),
    }
    fg, bg = colors.get(status, ("#444", "#eee"))
    return (
        f"<span style='background:{bg};color:{fg};"
        f"padding:2px 10px;border-radius:12px;"
        f"font-size:0.82em;font-weight:600;'>"
        f"{status.value.upper()}</span>"
    )
