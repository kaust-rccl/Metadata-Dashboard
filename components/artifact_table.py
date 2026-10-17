"""
components/artifact_table.py
-----------------------------
Renders a filterable, sortable artifact listing from DB rows.
Returns the yml_path of the selected artifact, or None.

Rows are keyed by their DB rowid, never by yml_path: on the shared DB two
rows can share a yml_path, which made Streamlit raise
StreamlitDuplicateElementKey. Duplicates are flagged instead (⚠ marker and
a warning above the table) until sync splits them or one is deleted.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Callable, Optional

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

    # ── corruption: the same physical row returned more than once ────────────
    if "_rowid" in rows[0].keys():
        counts = Counter(r["_rowid"] for r in rows)
        repeated = [rid for rid, n in counts.items() if n > 1]
        if repeated:
            st.error(
                f"**The tracker database is corrupt** — {len(repeated)} row(s) are "
                "returned more than once by the same query (one physical row, read "
                "twice). Each is shown once below. Rebuild the DB before editing: "
                "`docs/db_recovery.md`.",
                icon="🛑",
            )
            seen, unique = set(), []
            for r in rows:
                if r["_rowid"] not in seen:
                    seen.add(r["_rowid"])
                    unique.append(r)
            rows = unique


    # ── filters ───────────────────────────────────────────────────────────────
    # Filters survive opening a record and coming back: Streamlit drops a
    # widget's state on any run where the widget is not drawn, so each value
    # is mirrored in a plain "<key>__keep" entry and restored from it.
    for suffix in ("search", "type", "status"):
        k = f"{key_prefix}_{suffix}"
        if k not in st.session_state and f"{k}__keep" in st.session_state:
            st.session_state[k] = st.session_state[f"{k}__keep"]

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

    for suffix, val in (("search", search), ("type", type_sel), ("status", status_sel)):
        st.session_state[f"{key_prefix}_{suffix}__keep"] = val

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

    # ── duplicate alert ───────────────────────────────────────────────────────
    def _ident(r) -> tuple:
        ref = r["aimcr_reference"] if "aimcr_reference" in r.keys() else None
        ref = str(ref) if ref not in (None, "") else str(r["project_id"] or "")
        return (ref, r["artifact_type"], r["artifact_name"])

    path_counts  = Counter(str(r["yml_path"]) for r in rows)
    ident_counts = Counter(_ident(r) for r in rows)

    def _is_dup(r) -> bool:
        return path_counts[str(r["yml_path"])] > 1 or ident_counts[_ident(r)] > 1

    dup_rows = [r for r in rows if _is_dup(r)]
    if dup_rows:
        names = sorted({f"{_ident(r)[0]}/{r['artifact_type']}/{r['artifact_name']}" for r in dup_rows})
        st.warning(
            f"**{len(dup_rows)} duplicate DB records** ({len(names)} artifact(s)): "
            + ", ".join(f"`{n}`" for n in names[:8])
            + (" …" if len(names) > 8 else "")
            + "  \nThe next sync gives each its own name (`_dup2`, …). Review them "
              "and delete the ones that are true duplicates.",
            icon="⚠️",
        )

    # buttons never wrap ("Open" was breaking over two lines in narrow columns)
    st.markdown(
        "<style>div[data-testid='stButton'] button{white-space:nowrap;min-width:fit-content;}</style>",
        unsafe_allow_html=True,
    )

    # ── table header ──────────────────────────────────────────────────────────
    hcols = st.columns([3, 1, 1, 1.4, 2, 1.2])
    headers = ["Name", "Type", "Version", "Status", "Last modified", ""]
    for col, hdr in zip(hcols, headers):
        col.markdown(f"**{hdr}**")

    st.divider()

    # ── rows ──────────────────────────────────────────────────────────────────
    selected_yml_path: Optional[str] = None

    for i, row in enumerate(filtered):
        rcols = st.columns([3, 1, 1, 1.4, 2, 1.2])
        marker = " ⚠️" if _is_dup(row) else ""
        rcols[0].markdown(f"`{row['artifact_name']}`{marker}")
        rcols[1].caption(row["artifact_type"])
        rcols[2].caption(row["artifact_version"] or "—")
        rcols[3].markdown(
            status_badge(Status(row["status"])),
            unsafe_allow_html=True,
        )
        rcols[4].caption(
            (row["last_modified"] or "—")[:19].replace("T", " ")
        )
        # Position + rowid: unique per render even if the DB ever hands back a
        # repeated identifier (e.g. a user column shadowing rowid).
        row_id = row["_rowid"] if "_rowid" in row.keys() else "x"
        btn_key = f"{key_prefix}_open_{i}_{row_id}"
        if rcols[5].button("Open", key=btn_key):
            selected_yml_path = row["yml_path"]

    return selected_yml_path


def render_list_or_detail(
    rows: list,
    key_prefix: str,
    selected_key: str,
    render_detail: Callable[[Path], None],
    back_label: str = "← Back to all artifacts",
    **table_kwargs,
) -> None:
    """
    Show either the artifact table or the opened record — never both.

    Opening a record replaces the list (so it is not drawn underneath every
    row), with a back button on top. The table's filters are kept.
    `selected_key` is the session key holding the open record's yml_path.
    """
    if selected_key in st.session_state:
        if st.button(back_label, key=f"{key_prefix}_back"):
            st.session_state.pop(selected_key, None)
            st.rerun()
        render_detail(Path(st.session_state[selected_key]))
        return

    yml_path = render_artifact_table(rows, key_prefix=key_prefix, **table_kwargs)
    if yml_path:
        st.session_state[selected_key] = yml_path
        st.rerun()
