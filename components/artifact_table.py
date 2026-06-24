"""
components/artifact_table.py
-----------------------------
Renders a filterable, sortable artifact listing from DB rows.
Returns the yml_path of the selected artifact, or None.
"""

from __future__ import annotations

from typing import Optional

import streamlit as st

from components.fields import status_badge
from config import ArtifactType, Status


def render_artifact_table(
    rows: list,
    key_prefix: str = "tbl",
    status_filter: Optional[Status] = None,
) -> Optional[str]:
    """
    Render a searchable artifact table from sqlite3.Row list.

    Returns the yml_path of the row the user clicked,
    or None if no row was selected.
    """
    if not rows:
        st.info("No artifacts found.")
        return None

    # ── filters ───────────────────────────────────────────────────────────────
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        search = st.text_input(
            "Search", placeholder="artifact name or project…",
            key=f"{key_prefix}_search", label_visibility="collapsed"
        )
    with col2:
        type_opts = ["All types"] + [t.value for t in ArtifactType]
        type_sel = st.selectbox(
            "Type", type_opts, key=f"{key_prefix}_type",
            label_visibility="collapsed"
        )
    with col3:
        if status_filter is None:
            status_opts = ["All statuses"] + [s.value for s in Status]
            status_sel = st.selectbox(
                "Status", status_opts, key=f"{key_prefix}_status",
                label_visibility="collapsed"
            )
        else:
            status_sel = status_filter.value

    # ── filter rows ───────────────────────────────────────────────────────────
    filtered = list(rows)
    if search:
        q = search.lower()
        filtered = [
            r for r in filtered
            if q in r["artifact_name"].lower()
            or q in (r["project_id"] or "").lower()
        ]
    if type_sel not in ("All types", None):
        filtered = [r for r in filtered if r["artifact_type"] == type_sel]
    if status_sel not in ("All statuses", None):
        filtered = [r for r in filtered if r["status"] == status_sel]

    if not filtered:
        st.info("No artifacts match the current filters.")
        return None

    # ── table header ──────────────────────────────────────────────────────────
    hcols = st.columns([3, 1, 1, 1, 2, 1])
    headers = ["Name", "Type", "Version", "Status", "Last modified", ""]
    for col, hdr in zip(hcols, headers):
        col.markdown(f"**{hdr}**")

    st.divider()

    # ── rows ──────────────────────────────────────────────────────────────────
    selected_yml_path: Optional[str] = None

    for row in filtered:
        rcols = st.columns([3, 1, 1, 1, 2, 1])
        rcols[0].markdown(f"`{row['artifact_name']}`")
        rcols[1].caption(row["artifact_type"])
        rcols[2].caption(row["artifact_version"] or "—")
        rcols[3].markdown(
            status_badge(Status(row["status"])),
            unsafe_allow_html=True,
        )
        rcols[4].caption(
            (row["last_modified"] or "—")[:19].replace("T", " ")
        )
        btn_key = f"{key_prefix}_open_{row['yml_path']}"
        if rcols[5].button("Open", key=btn_key):
            selected_yml_path = row["yml_path"]

    return selected_yml_path
