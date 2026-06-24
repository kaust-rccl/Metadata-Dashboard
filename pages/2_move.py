"""
pages/2_move.py
---------------
Mover view.

Responsibilities:
- List artifacts with status STAGED (and BLOCKED if mover can act)
- Show full artifact detail (curation fields locked/read-only)
- Fill moving fields: checksum_verified_gpup, moving_notes
- Confirm move → MoveService
- Block / reject / reinstate from moving phase → respective services
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from auth.session import current_db, current_role, current_username, init_session, require_role
from components.artifact_table import render_artifact_table
from components.fields import (
    render_checksum_verification, render_field,
    render_status_reason_list, render_text_area,
    section_header, status_badge,
)
from components.modlog_viewer import render_modlog
from config import Role, Status
from iolib.database import Database
from iolib.fs import path_exists
from iolib.services import (
    BlockService, MoveService, ReinstateService,
    RejectService, ServiceResult,
)
from iolib.yaml_io import load_artifact_yml, load_modlog
from config import modlog_path
from models.artifact import ChecksumVerification


def main() -> None:
    init_session()
    db: Database = current_db()
    
    if not require_role(Role.MOVER):
        st.stop()

    actor = current_username()
    role  = current_role()

    st.title("Mover workspace")

    rows = db.list_artifacts(status=Status.STAGED.value)
    blocked = db.list_artifacts(status=Status.BLOCKED.value)
    all_rows = rows + blocked

    st.caption(
        f"{len(rows)} staged · {len(blocked)} blocked"
    )

    yml_path = render_artifact_table(
        all_rows, key_prefix="mov",
        status_filter=None,
    )
    if yml_path:
        st.session_state["mov_selected_yml"] = yml_path

    if "mov_selected_yml" in st.session_state:
        st.divider()
        _render_mover_detail(
            Path(st.session_state["mov_selected_yml"]),
            actor, role, db
        )


def _render_mover_detail(
    yml_path: Path, actor: str, role: Role, db: Database
) -> None:
    yml = load_artifact_yml(yml_path)
    if yml is None:
        st.error(f"Could not load YML at {yml_path}")
        return

    editable = yml.editable_by(role)

    st.subheader(yml.artifact_name)
    st.markdown(
        status_badge(yml.status) +
        f"&nbsp;&nbsp;<span style='color:#888;font-size:0.85em'>"
        f"Curated by <b>{yml.curated_by}</b></span>",
        unsafe_allow_html=True,
    )
    st.write("")

    # ── curation fields — all locked for mover ─────────────────────────────
    with st.expander("Curation fields (read-only)", expanded=False):
        section_header("Curation", color="#1D9E75")
        c1, c2 = st.columns(2)
        with c1:
            render_field("Source path", yml.source_path, False, "m_src")
            render_field("Checksum filename", yml.checksum_filename, False, "m_cfn")
            render_field("Size (GB)", yml.size_gb, False, "m_size")
        with c2:
            render_field("Destination path", yml.destination_path, False, "m_dst")
            render_field("Checksum type", yml.checksum_type.value, False, "m_ctype")
            render_field("Curated by", yml.curated_by, False, "m_cby")

        render_checksum_verification(
            "Checksum — cpup (verified by curator)",
            yml.checksum_verified_cpup, editable=False, field_key="m_cv_cpup"
        )
        render_text_area("Curation notes", yml.curation_notes, False, "m_cnotes")

        if yml.curation_status_reason:
            render_status_reason_list(
                "Curation status history", yml.curation_status_reason
            )

    # ── destination path check ─────────────────────────────────────────────
    with st.expander("Destination verification", expanded=True):
        section_header("Destination — gpup", color="#534AB7")

        dst = yml.destination_path
        dst_ok = path_exists(dst)
        if dst_ok:
            st.success(f"Destination accessible: `{dst}`")
        else:
            st.error(
                f"Destination path not accessible: `{dst}`  \n"
                "Resolve before confirming the move."
            )

    # ── mover fields ───────────────────────────────────────────────────────
    with st.expander("Moving fields", expanded=True):
        section_header("Moving", color="#534AB7")

        cv_data = render_checksum_verification(
            "Checksum — gpup",
            yml.checksum_verified_gpup,
            editable="checksum_verified_gpup" in editable,
            field_key="m_cv_gpup",
        )

        new_moving_notes = render_text_area(
            "Moving notes",
            yml.moving_notes,
            editable="moving_notes" in editable,
            field_key="m_mnotes",
        )

        if yml.moving_status_reason:
            render_status_reason_list(
                "Moving status history", yml.moving_status_reason
            )

    # ── confirm move ───────────────────────────────────────────────────────
    if yml.status in (Status.STAGED, Status.BLOCKED):
        with st.expander("Confirm move", expanded=True):
            section_header("Move confirmation", color="#534AB7")

            if not dst_ok:
                st.warning("Destination path must be accessible before confirming.")
            else:
                st.info(
                    "Confirming the move will:  \n"
                    "- Set status to **moved**  \n"
                    "- Record your username and timestamp  \n"
                    "- Update partition to **gpu**  \n"
                    "- Write a YML snapshot to the destination  \n"
                    "- Append to the modification log"
                )

                if st.button("Confirm move", type="primary", key="mov_confirm"):
                    # Apply any gpup checksum edits from the form
                    yml.checksum_verified_gpup = ChecksumVerification(
                        verified=cv_data["verified"],
                        verified_by=cv_data["verified_by"],
                        verified_date=cv_data["verified_date"],
                    )
                    yml.moving_notes = new_moving_notes

                    result: ServiceResult = MoveService().execute(
                        yml, actor=actor, db=db,
                    )
                    if result.success:
                        st.success(result.message)
                        st.balloons()
                        del st.session_state["mov_selected_yml"]
                        st.rerun()
                    else:
                        for err in result.errors:
                            st.error(err)

    # ── block / reject / reinstate ─────────────────────────────────────────
    if yml.status in (Status.STAGED, Status.BLOCKED, Status.REJECTED):
        with st.expander("Actions", expanded=False):
            section_header("Mover actions", color="#BA7517")

            action_col, reason_col = st.columns([1, 2])
            with action_col:
                action = st.radio(
                    "Action",
                    ["Block", "Reject", "Reinstate"],
                    key="mov_action",
                )
            with reason_col:
                reason = st.text_area("Reason *", height=80, key="mov_reason")

            if st.button("Apply action", key="mov_apply"):
                result = None
                if action == "Block":
                    result = BlockService().execute(
                        yml, actor, Role.MOVER, "moving",
                        reason, db
                    )
                elif action == "Reject":
                    result = RejectService().execute(
                        yml, actor, Role.MOVER, "moving",
                        reason, db
                    )
                elif action == "Reinstate":
                    result = ReinstateService().execute(
                        yml, actor, Role.MOVER, "moving",
                        reason, db
                    )
                if result:
                    if result.success:
                        st.success(result.message)
                        st.rerun()
                    else:
                        for err in result.errors:
                            st.error(err)

    # ── modlog ─────────────────────────────────────────────────────────────
    with st.expander("Modification log", expanded=False):
        ml = load_modlog(modlog_path(yml.aimcr_reference, yml.artifact_name, yml.artifact_type))
        render_modlog(ml)


main()
