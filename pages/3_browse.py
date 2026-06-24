"""
pages/3_browse.py
-----------------
Read-only browse view — accessible to all roles.

Shows all artifacts with full detail on selection.
No write operations permitted from this page.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from auth.session import current_db, current_role, current_username, init_session
from components.artifact_table import render_artifact_table
from components.fields import (
    render_checksum_verification, render_field,
    render_status_reason_list, render_text_area,
    section_header, status_badge,
)
from components.modlog_viewer import render_modlog
from config import Role
from iolib.yaml_io import load_artifact_yml, load_modlog
from config import modlog_path


def main() -> None:
    init_session()
    db: Database = current_db()

    st.title("Browse artifacts")

    role = current_role()
    st.caption(f"Signed in as **{current_username()}** · role: `{role.value}`")

    rows = db.list_artifacts()
    yml_path = render_artifact_table(rows, key_prefix="brw")
    if yml_path:
        st.session_state["brw_selected_yml"] = yml_path

    if "brw_selected_yml" in st.session_state:
        st.divider()
        _render_readonly_detail(Path(st.session_state["brw_selected_yml"]))


def _render_readonly_detail(yml_path: Path) -> None:
    yml = load_artifact_yml(yml_path)
    if yml is None:
        st.error(f"Could not load YML at {yml_path}")
        return

    st.subheader(yml.artifact_name)
    st.markdown(
        status_badge(yml.status) +
        f"&nbsp;&nbsp;<span style='color:#888;font-size:0.85em'>"
        f"Last modified by <b>{yml.last_modified_by}</b></span>",
        unsafe_allow_html=True,
    )
    st.write("")

    # ── identity ───────────────────────────────────────────────────────────
    with st.expander("Identity", expanded=True):
        section_header("Identity", color="#5F5E5A")
        c1, c2, c3 = st.columns(3)
        with c1:
            render_field("Artifact name", yml.artifact_name, False, "b_name")
            render_field("Project ID", yml.project_id, False, "b_pid")
        with c2:
            render_field("Version", yml.artifact_version, False, "b_ver")
            render_field("PI username", yml.pi_username, False, "b_pi")
        with c3:
            render_field("Type", yml.artifact_type.value, False, "b_type")
            render_field("AIMCR ref", yml.aimcr_reference, False, "b_aimcr")

        c1, c2 = st.columns(2)
        with c1:
            render_field("Tracking ticket", yml.tracking_ticket, False, "b_tt")
        with c2:
            render_field("DM ticket", yml.dm_ticket_number, False, "b_dm")

    # ── curation ───────────────────────────────────────────────────────────
    with st.expander("Curation", expanded=False):
        section_header("Curation", color="#1D9E75")
        c1, c2 = st.columns(2)
        with c1:
            render_field("Source path", yml.source_path, False, "b_src")
            render_field("Checksum filename", yml.checksum_filename, False, "b_cfn")
            render_field("Size (GB)", yml.size_gb, False, "b_size")
            render_field("Curated by", yml.curated_by, False, "b_cby")
        with c2:
            render_field("Destination path", yml.destination_path, False, "b_dst")
            render_field("Checksum type", yml.checksum_type.value, False, "b_ctype")
            render_field("Partition", yml.partition.value, False, "b_part")
            render_field("Date staged", str(yml.date_staged or ""), False, "b_dstaged")

        render_checksum_verification(
            "Checksum — cpup", yml.checksum_verified_cpup,
            editable=False, field_key="b_cv_cpup"
        )

        c1, c2 = st.columns(2)
        with c1:
            render_field("Reference", str(yml.reference), False, "b_ref")
        with c2:
            render_field("Reference group", yml.reference_group, False, "b_refg")

        render_text_area("Curation notes", yml.curation_notes, False, "b_cnotes")

        if yml.curation_status_reason:
            render_status_reason_list(
                "Curation status history", yml.curation_status_reason
            )

    # ── moving ─────────────────────────────────────────────────────────────
    with st.expander("Moving", expanded=False):
        section_header("Moving", color="#534AB7")
        c1, c2 = st.columns(2)
        with c1:
            render_field("Moved by", yml.moved_by or "—", False, "b_mby")
        with c2:
            render_field("Date moved", str(yml.date_moved or "—"), False, "b_dmoved")

        render_checksum_verification(
            "Checksum — gpup", yml.checksum_verified_gpup,
            editable=False, field_key="b_cv_gpup"
        )
        render_text_area("Moving notes", yml.moving_notes, False, "b_mnotes")

        if yml.moving_status_reason:
            render_status_reason_list(
                "Moving status history", yml.moving_status_reason
            )

    # ── modlog ─────────────────────────────────────────────────────────────
    with st.expander("Modification log", expanded=False):
        ml = load_modlog(modlog_path(yml.aimcr_reference, yml.artifact_name, yml.artifact_type))
        render_modlog(ml)


main()
