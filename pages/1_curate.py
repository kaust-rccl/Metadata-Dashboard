"""
pages/1_curate.py
-----------------
Curator view.

Responsibilities:
- New artifact form (first save → CurationService)
- Edit curation_notes on existing staged artifact (CurationUpdateService)
- Block / reject / reinstate actions (BlockService, RejectService, ReinstateService)

All write operations go through services — no direct yaml_io calls here.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from auth.session import current_db, current_role, current_username, init_session, require_role
from components.artifact_table import render_artifact_table
from components.fields import (
    render_checksum_verification, render_field, render_status_reason_list,
    render_text_area, section_header, status_badge,
)
from components.modlog_viewer import render_modlog
from config import (
    ArtifactType, ChecksumSource, ChecksumType, Partition,
    Role, Status,
)
from iolib.database import Database
from iolib.services import (
    BlockService, CurationService, CurationUpdateService,
    ReinstateService, RejectService, ServiceResult,
)
from iolib.yaml_io import load_artifact_yml, load_modlog
from config import modlog_path
from models.artifact import ArtifactYML, ChecksumVerification
from iolib.fs import extract_artifact_info


def main() -> None:
    
    init_session()
    db: Database = current_db()

    if not require_role(Role.CURATOR):
        st.stop()

    actor    = current_username()
    role     = current_role()

    st.title("Curator workspace")

    tab_new, tab_existing = st.tabs(["New artifact", "Existing artifacts"])

    # ── new artifact form ─────────────────────────────────────────────────────
    with tab_new:
        _render_new_artifact_form(actor, db)

    # ── existing artifacts ────────────────────────────────────────────────────
    with tab_existing:
        rows = db.list_artifacts()
        yml_path = render_artifact_table(rows, key_prefix="cur")
        if yml_path:
            st.session_state["cur_selected_yml"] = yml_path

        if "cur_selected_yml" in st.session_state:
            st.divider()
            _render_artifact_detail(
                Path(st.session_state["cur_selected_yml"]),
                actor, role, db
            )


def _render_new_artifact_form(actor: str, db: Database) -> None:
    st.subheader("Stage new artifact")

    # ── auto-fill section (outside form so button triggers rerun) ──────────
    section_header("Quick fill from source path")
    path_col, btn_col = st.columns([3, 1])
    with path_col:
        autofill_path = st.text_input(
            "Source path",
            value=st.session_state.get("af_source_path", ""),
            placeholder="/scratch/project/k03/support_team/65616/models/models--ByteDance-Seed--Seed-X-PPO-7B",
            key="af_path_input",
            label_visibility="collapsed",
        )
    with btn_col:
        if st.button("Auto-fill", key="autofill_btn", use_container_width=True):
            if autofill_path.strip():
                with st.spinner("Reading path..."):
                    info = extract_artifact_info(autofill_path.strip())
                if "_error" in info:
                    st.error(info["_error"])
                else:
                    info["af_source_path"] = autofill_path.strip()
                    st.session_state.update(info)
                    found = [k for k in info if not k.startswith("_") and k != "af_source_path"]
                    st.success(f"Auto-filled: {', '.join(found)}")
            else:
                st.warning("Enter a source path first.")

    st.divider()

    # ── main form ──────────────────────────────────────────────────────────
    # Pre-populate from session state if auto-fill was used, otherwise blank.
    def _ss(key, default=""):
        return st.session_state.get(key, default)

    with st.form("new_artifact_form"):
        section_header("Identity")
        c1, c2, c3 = st.columns(3)
        artifact_name    = c1.text_input("Artifact name *",  value=_ss("artifact_name"))
        artifact_version = c2.text_input("Version *",        value=_ss("artifact_version", "1"))
        _type_opts = [t.value for t in ArtifactType]
        _type_idx  = _type_opts.index(_ss("artifact_type", ArtifactType.DATASET.value)) \
                     if _ss("artifact_type") in _type_opts else 0
        artifact_type = c3.selectbox("Type *", _type_opts, index=_type_idx)

        c1, c2 = st.columns(2)
        proposal_title = c1.text_input("Proposal title *", value=_ss("proposal_title"))
        pi_username    = c2.text_input("PI username *",    value=_ss("pi_username"))

        c1, c2, c3 = st.columns(3)
        project_id      = c1.text_input("Project ID (k#####) *",    value=_ss("project_id"))
        aimcr_reference = c2.text_input("AIMCR reference (6####) *", value=_ss("aimcr_reference"))
        tracking_ticket = c3.text_input("Tracking ticket (6####) *", value=_ss("tracking_ticket"))

        section_header("Curation")
        dm_ticket = st.text_input(
            "DM ticket number (RT) *",
            value=_ss("dm_ticket_number"),
            help="Required before saving. Open the RT ticket first.",
        )

        c1, c2 = st.columns(2)
        source_path      = c1.text_input("Source path (cpup) *",  value=_ss("af_source_path"))
        destination_path = c2.text_input("Destination path (gpup) *", value=_ss("destination_path"))

        c1, c2, c3 = st.columns(3)
        _ctype_opts = [t.value for t in ChecksumType]
        _ctype_idx  = _ctype_opts.index(_ss("checksum_type", ChecksumType.SHA256.value)) \
                      if _ss("checksum_type") in _ctype_opts else 0
        checksum_type = c1.selectbox("Checksum type", _ctype_opts, index=_ctype_idx)

        _csrc_opts = [s.value for s in ChecksumSource]
        _csrc_idx  = _csrc_opts.index(_ss("checksum_source", ChecksumSource.SELF_GENERATED.value)) \
                     if _ss("checksum_source") in _csrc_opts else 0
        checksum_source = c2.selectbox("Checksum source", _csrc_opts, index=_csrc_idx)

        checksum_file = c3.text_input(
            "Checksum filename *",
            value=_ss("checksum_filename", "checksum.sha256"),
        )

        c1, c2 = st.columns(2)
        size_gb   = c1.number_input(
            "Size (GB) *",
            value=float(_ss("size_gb", 0.0)),
            min_value=0.0, step=0.1, format="%.3f",
        )
        ref_group = c2.text_input("Reference group", value=_ss("reference_group", ""))

        reference = st.checkbox("Reference candidate", value=bool(_ss("reference", False)))

        section_header("Checksum verification — cpup")
        cv_verified    = st.checkbox("Verified on cpup")
        cv_verified_by = st.text_input("Verified by (cpup)")

        section_header("Notes")
        curation_notes = st.text_area("Curation notes", height=80)

        submitted = st.form_submit_button("Stage artifact", type="primary")

    if submitted:
        if not dm_ticket.strip():
            st.error("DM ticket number is required before saving.")
            return

        yml = ArtifactYML(
            artifact_name=artifact_name.strip(),
            artifact_type=ArtifactType(artifact_type),
            artifact_version=artifact_version.strip(),
            proposal_title=proposal_title.strip(),
            project_id=project_id.strip(),
            pi_username=pi_username.strip(),
            aimcr_reference=aimcr_reference.strip(),
            tracking_ticket=tracking_ticket.strip(),
            dm_ticket_number=dm_ticket.strip(),
            source_path=source_path.strip(),
            destination_path=destination_path.strip(),
            checksum_type=ChecksumType(checksum_type),
            checksum_source=ChecksumSource(checksum_source),
            checksum_filename=checksum_file.strip(),
            size_gb=float(size_gb),
            checksum_verified_cpup=ChecksumVerification(
                verified=cv_verified,
                verified_by=cv_verified_by.strip(),
            ),
            reference=reference,
            reference_group=ref_group.strip(),
            curation_notes=curation_notes.strip(),
        )

        result = CurationService().execute(yml, actor=actor, db=db)
        if result.success:
            st.success(result.message)
            st.balloons()
            # Clear auto-fill session state after successful save
            for k in ["artifact_name", "artifact_type", "artifact_version",
                      "aimcr_reference", "size_gb", "checksum_filename",
                      "checksum_type", "checksum_source", "af_source_path"]:
                st.session_state.pop(k, None)
        else:
            for err in result.errors:
                st.error(err)

def _render_artifact_detail(
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
        f"Last modified by <b>{yml.last_modified_by}</b></span>",
        unsafe_allow_html=True,
    )
    st.write("")

    # ── identity ───────────────────────────────────────────────────────────────
    with st.expander("Identity", expanded=False):
        section_header("Identity", color="#5F5E5A")

        with st.form("edit_identity_form"):
            c1, c2, c3 = st.columns(3)
            with c1:
                new_name = st.text_input("Artifact name", value=yml.artifact_name or "")
            with c2:
                new_ver = st.text_input("Version", value=yml.artifact_version or "")
            with c3:
                new_type = st.selectbox(
                    "Type",
                    [t.value for t in ArtifactType],
                    index=[t.value for t in ArtifactType].index(yml.artifact_type.value),
                )

            c1, c2, c3 = st.columns(3)
            with c1:
                new_pid = st.text_input("Project ID", value=yml.project_id or "")
            with c2:
                new_pi = st.text_input("PI username", value=yml.pi_username or "")
            with c3:
                new_aimcr = st.text_input("AIMCR reference", value=yml.aimcr_reference or "")

            c1, c2 = st.columns(2)
            with c1:
                new_tt = st.text_input("Tracking ticket", value=yml.tracking_ticket or "")
            with c2:
                new_ptitle = st.text_input("Proposal title", value=yml.proposal_title or "")

            id_saved = st.form_submit_button("Save identity fields", type="primary")

        if id_saved:
            result = CurationUpdateService().execute(
                yml,
                actor=actor,
                updated_fields={
                    "artifact_name":    new_name,
                    "artifact_version": new_ver,
                    "artifact_type":    ArtifactType(new_type),
                    "project_id":       new_pid,
                    "pi_username":      new_pi,
                    "aimcr_reference":  new_aimcr,
                    "tracking_ticket":  new_tt,
                    "proposal_title":   new_ptitle,
                },
                db=db,
            )
            if result.success:
                st.success(result.message)
                st.rerun()
            else:
                for err in result.errors:
                    st.error(err)

    # ── curation ───────────────────────────────────────────────────────────
    with st.expander("Curation fields", expanded=True):
        section_header("Curation", color="#1D9E75")

        with st.form("edit_curation_form"):
            c1, c2 = st.columns(2)
            with c1:
                new_source = st.text_input("Source path", value=yml.source_path or "")
            with c2:
                new_dest = st.text_input("Destination path", value=yml.destination_path or "")

            c1, c2, c3 = st.columns(3)
            with c1:
                new_ctype = st.selectbox(
                    "Checksum type",
                    [t.value for t in ChecksumType],
                    index=[t.value for t in ChecksumType].index(yml.checksum_type.value),
                )
            with c2:
                new_csrc = st.selectbox(
                    "Checksum source",
                    [s.value for s in ChecksumSource],
                    index=[s.value for s in ChecksumSource].index(yml.checksum_source.value),
                )
            with c3:
                new_cfn = st.text_input("Checksum filename", value=yml.checksum_filename or "")

            c1, c2, c3 = st.columns(3)
            with c1:
                new_size = st.number_input(
                    "Size (GB)", value=float(yml.size_gb or 0.0),
                    min_value=0.0, step=0.1, format="%.3f"
                )
            with c2:
                new_dm = st.text_input("DM ticket", value=yml.dm_ticket_number or "")
            with c3:
                new_ref_group = st.text_input("Reference group", value=yml.reference_group or "")

            new_ref = st.checkbox("Reference candidate", value=yml.reference)

            st.markdown("**Checksum — cpup**")
            cc1, cc2 = st.columns(2)
            with cc1:
                new_cv_verified = st.checkbox(
                    "Verified on cpup",
                    value=yml.checksum_verified_cpup.verified,
                )
            with cc2:
                new_cv_by = st.text_input(
                    "Verified by",
                    value=yml.checksum_verified_cpup.verified_by or "",
                )

            new_notes = st.text_area(
                "Curation notes", value=yml.curation_notes or "", height=80
            )

            cur_saved = st.form_submit_button("Save curation fields", type="primary")

        if cur_saved:
            from datetime import datetime, timezone
            cv_date = (
                datetime.now(tz=timezone.utc)
                if new_cv_verified and not yml.checksum_verified_cpup.verified_date
                else yml.checksum_verified_cpup.verified_date
            )
            result = CurationUpdateService().execute(
                yml,
                actor=actor,
                updated_fields={
                    "source_path":       new_source,
                    "destination_path":  new_dest,
                    "checksum_type":     ChecksumType(new_ctype),
                    "checksum_source":   ChecksumSource(new_csrc),
                    "checksum_filename": new_cfn,
                    "size_gb":           float(new_size),
                    "dm_ticket_number":  new_dm,
                    "reference":         new_ref,
                    "reference_group":   new_ref_group,
                    "checksum_verified_cpup": ChecksumVerification(
                        verified=new_cv_verified,
                        verified_by=new_cv_by,
                        verified_date=cv_date,
                    ),
                    "curation_notes": new_notes,
                },
                db=db,
            )
            if result.success:
                st.success(result.message)
                st.rerun()
            else:
                for err in result.errors:
                    st.error(err)

        if yml.curation_status_reason:
            render_status_reason_list(
                "Curation status history", yml.curation_status_reason
            )

        if cur_saved:
            from datetime import datetime, timezone
            cv_date = (
                datetime.now(tz=timezone.utc)
                if new_cv_verified and not yml.checksum_verified_cpup.verified_date
                else yml.checksum_verified_cpup.verified_date
            )
            result = CurationUpdateService().execute(
                yml,
                actor=actor,
                updated_fields={
                "source_path":        new_source,
                "destination_path":   new_dest,
                "checksum_type":      ChecksumType(new_ctype),
                "checksum_source":    ChecksumSource(new_csrc),
                "checksum_filename":  new_cfn,
                "size_gb":            float(new_size),
                "reference":          new_ref,
                "reference_group":    new_ref_group,
                "checksum_verified_cpup": ChecksumVerification(
                    verified=new_cv_verified,
                    verified_by=new_cv_by,
                    verified_date=cv_date,
                ),
                "curation_notes":     new_notes,
                "dm_ticket_number":   new_dm,
            },
            db=db,
            )
            if result.success:
                st.success(result.message)
                st.rerun()
            else:
                for err in result.errors:
                    st.error(err)

        if yml.curation_status_reason:
            render_status_reason_list(
                "Curation status history", yml.curation_status_reason
            )

    # ── block / reject / reinstate ─────────────────────────────────────────
    if yml.status in (Status.STAGED, Status.BLOCKED, Status.REJECTED):
        with st.expander("Actions", expanded=True):
            section_header("Curator actions", color="#BA7517")

            action_col, reason_col = st.columns([1, 2])
            with action_col:
                action = st.radio(
                    "Action",
                    ["Block", "Reject", "Reinstate"],
                    key="cur_action",
                    horizontal=False,
                )
            with reason_col:
                reason = st.text_area("Reason *", height=80, key="cur_reason")

            if st.button("Apply action", key="cur_apply", type="primary"):
                result = None
                if action == "Block":
                    result = BlockService().execute(
                        yml, actor, Role.CURATOR, "curation",
                        reason, db
                    )
                elif action == "Reject":
                    result = RejectService().execute(
                        yml, actor, Role.CURATOR, "curation",
                        reason, db
                    )
                elif action == "Reinstate":
                    result = ReinstateService().execute(
                        yml, actor, Role.CURATOR, "curation",
                        reason, db
                    )
                if result:
                    if result.success:
                        st.success(result.message)
                        st.rerun()
                    else:
                        for err in result.errors:
                            st.error(err)

    # ── moving fields (read-only for curator) ──────────────────────────────
    if yml.status == Status.MOVED:
        with st.expander("Moving fields", expanded=False):
            section_header("Moving", color="#534AB7")
            c1, c2 = st.columns(2)
            with c1:
                render_field("Moved by", yml.moved_by, False, "d_mby")
            with c2:
                render_field("Date moved", str(yml.date_moved or ""), False, "d_dm2")
            render_checksum_verification(
                "Checksum — gpup", yml.checksum_verified_gpup,
                editable=False, field_key="d_cv_gpup"
            )
            render_text_area("Moving notes", yml.moving_notes, False, "d_mnotes")

    # ── modlog ─────────────────────────────────────────────────────────────
    with st.expander("Modification log", expanded=False):
        ml = load_modlog(modlog_path(yml.aimcr_reference, yml.artifact_name, yml.artifact_type))
        render_modlog(ml)


main()
