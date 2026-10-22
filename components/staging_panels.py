"""
components/staging_panels.py
----------------------------
Panels shown on an artifact's detail view while it moves through staging:

  render_checksum_panel  — submit / monitor / cancel the CPUP checksum job;
                           ticks the CPUP checksum box when every file is OK
  render_ticket_panel    — suggested DM-ticket subject + body, editable, kept
                           in the DB per artifact (not in the YML)
  render_mark_staged     — PREPARING → STAGED, gated on the DM ticket number
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import streamlit as st

from components.fields import section_header
from config import Role, Status
from iolib import checksum_jobs as cj
from iolib.services import CurationUpdateService, MarkStagedService
from iolib.ticket import default_ticket


def _k(yml_path: Path, name: str) -> str:
    """Widget key unique per artifact."""
    return f"{name}_{hashlib.md5(str(yml_path).encode()).hexdigest()[:10]}"


# ── checksum ──────────────────────────────────────────────────────────────────

def render_checksum_panel(yml, yml_path: Path, actor: str, db) -> None:
    with st.expander("Checksum verification job (CPUP)", expanded=yml.status == Status.PREPARING):
        status = cj.job_status(yml)

        msg = cj.auto_apply(yml, db, status)
        if msg:
            st.success(msg)
            st.rerun()

        icon = {"ok": "✅", "failed": "❌", "error": "⚠️", "incomplete": "⏸️",
                "running": "⏳", "running?": "⏳", "queued": "🕒"}.get(status.state, "•")
        st.markdown(f"**Status:** {icon} {status.label}")

        if status.state != "none":
            if status.total:
                st.progress(min(status.done / status.total, 1.0),
                            text=f"{status.done} / {status.total} files checked")
            c = st.columns(4)
            c[0].metric("OK", status.ok)
            c[1].metric("Mismatch", status.mismatch)
            c[2].metric("Missing", status.missing)
            c[3].metric("Remaining", status.remaining if status.total else "—")
            jobs = ", ".join(f"{j} ({status.live[j].lower()})" if j in status.live else j
                             for j in status.job_ids) or "not submitted yet"
            conv = (f" (verified via converted copy `{Path(status.checksum_file).name}`)"
                    if status.checksum_file != status.original_checksum_file else "")
            st.caption(f"Checksum file: `{status.original_checksum_file}`{conv}  \nJob(s): {jobs}  \n"
                       f"Job directory: `{status.job_dir}`")
            if status.result_line:
                st.caption(status.result_line)
            if status.error_lines:
                st.error("\n".join(status.error_lines))
            if not status.slurm:
                st.caption("Slurm commands are not visible from where the dashboard runs, "
                           "so live queue state is unknown; progress is read from the job's files.")

            b = st.columns(3)
            if b[0].button("Refresh", key=_k(yml_path, "ck_refresh")):
                st.rerun()
            if status.is_live and b[1].button("Cancel job", key=_k(yml_path, "ck_cancel")):
                ok, m = cj.cancel_job(yml)
                (st.success if ok else st.error)(m)

            if status.failed_sample:
                with st.expander(f"Failed files ({status.mismatch + status.missing})"):
                    st.code("\n".join(status.failed_sample))
            if status.log_path:
                with st.expander(f"Log — {status.log_path.name}"):
                    lines = status.log_path.read_text(errors="replace").splitlines()[-40:]
                    st.code("\n".join(lines) or "(empty)")

        if status.state == "ok":
            _render_audit(yml, yml_path, status)
            return
        if status.is_live:
            return

        _render_submit_form(yml, yml_path, actor, db, status)


def _render_audit(yml, yml_path: Path, status) -> None:
    section_header("Audit copy")
    a = status.audit
    if status.audit_done:
        st.caption(f"✅ Copied to `{a['path']}` — {a.get('files', '?')} files verified "
                   f"against the job directory, {str(a.get('copied_at', ''))[:16]}.")
        return
    st.warning(f"Not copied yet to `{a.get('path') or cj.audit_dir(yml.aimcr_reference, yml.artifact_type, yml.artifact_name)}`."
               + (f"  \nLast attempt: {a['error']}" if a.get("error") else ""))
    if st.button("Copy to audit now", key=_k(yml_path, "ck_audit")):
        ok, m = cj.audit_copy(yml, status)
        if ok:
            st.success(m)
            st.rerun()
        st.error(m.split(" Run on a login node")[0])
    st.caption("Compute nodes cannot write to /project. If this dashboard runs on one, "
               "run this on a login node:")
    st.code(f"dashboard audit-copy {yml.aimcr_reference} {yml.artifact_name}", language="bash")


def _render_submit_form(yml, yml_path: Path, actor: str, db, status) -> None:
    section_header("Submit checksum job")
    candidates = cj.suggest_checksum_files(yml.source_path,
                                           status.original_checksum_file or yml.checksum_filename)
    other = "Other path…"
    opts = candidates + [other]
    choice = st.selectbox(
        "Checksum file", opts, key=_k(yml_path, "ck_choice"),
        help="Suggested from the source path (checksum.sha256 and other *.sha256 / *.md5 files).",
    )
    if choice == other or not candidates:
        checksum_file = st.text_input("Checksum file path", key=_k(yml_path, "ck_path"),
                                      value=status.original_checksum_file)
    else:
        checksum_file = choice
    data_dir = st.text_input(
        "Data directory (optional)", value=status.data_dir, key=_k(yml_path, "ck_data"),
        help="Where the files listed in the checksum file live. Leave empty to use the "
             "checksum file's own directory.",
    )

    ready = False
    if checksum_file.strip():
        ready, info = cj.check_checksum_file(checksum_file.strip())
        (st.caption if ready else st.warning)(info)

    auto = st.checkbox("Resubmit automatically if the time limit is reached", value=True,
                       key=_k(yml_path, "ck_auto"))
    retry = False
    if status.state == "failed":
        retry = st.checkbox("Re-verify the failed files", value=True, key=_k(yml_path, "ck_retry"))

    label = {"incomplete": "Resubmit (continue)", "failed": "Resubmit"}.get(status.state,
                                                                        "Submit checksum job")
    if st.button(label, key=_k(yml_path, "ck_submit"), type="primary", disabled=not ready):
        ok, m = cj.submit_job(yml, checksum_file.strip(), data_dir.strip(), actor,
                              auto_resubmit=auto, retry_failed=retry)
        if ok:
            if not yml.checksum_filename.strip():
                CurationUpdateService().execute(
                    yml, actor, {"checksum_filename": checksum_file.strip()}, db)
            st.success(m)
            st.rerun()
        elif "\n\n" in m:
            head, cmd = m.split("\n\n", 1)
            st.warning(head)
            st.code(cmd, language="bash")
        else:
            st.error(m)


# ── ticket text ───────────────────────────────────────────────────────────────

def render_ticket_panel(yml, yml_path: Path, actor: str, db) -> None:
    ready = yml.checksum_verified_cpup.verified or yml.status != Status.PREPARING
    with st.expander("DM ticket text", expanded=ready and yml.status == Status.PREPARING):
        if not ready:
            st.caption("The suggested ticket text appears once the CPUP checksum is verified.")
            return

        ks, kb, kr = (_k(yml_path, "tk_subject"), _k(yml_path, "tk_body"),
                      _k(yml_path, "tk_reset"))
        draft = db.get_ticket_draft(str(yml_path))
        # Values are set before the widgets exist (Streamlit forbids changing a
        # widget's state after it is drawn): template on reset, else the saved
        # draft the first time this artifact is shown, else the template.
        if st.session_state.pop(kr, False):
            st.session_state[ks], st.session_state[kb] = default_ticket(yml)
        elif ks not in st.session_state:
            if draft is not None:
                st.session_state[ks], st.session_state[kb] = draft["subject"], draft["body"]
            else:
                st.session_state[ks], st.session_state[kb] = default_ticket(yml)

        subject = st.text_input("Subject", key=ks)
        body = st.text_area("Body", key=kb, height=230)
        st.code(f"Subject: {subject}\n\n{body}", language=None)

        c = st.columns([1, 1, 3])
        if c[0].button("Save text", key=_k(yml_path, "tk_save")):
            db.save_ticket_draft(str(yml_path), subject, body, actor)
            st.success("Ticket text saved for this artifact.")
        if c[1].button("Reset to template", key=_k(yml_path, "tk_tmpl")):
            st.session_state[kr] = True
            st.rerun()
        note = "Kept in the portal for this artifact; not written to the YML."
        if draft is not None:
            note += f" Last saved by {draft['updated_by']} at {str(draft['updated_at'])[:16]}."
        c[2].caption(note)


# ── mark staged ───────────────────────────────────────────────────────────────

# Required-to-stage fields that can be filled in right in the panel, with
# where they otherwise live in the detail view.
_STAGE_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("dm_ticket_number", "DM ticket number (RT) *", "text"),
    ("tracking_ticket",  "Tracking ticket (6####) *", "text"),
    ("size_gb",          "Size (GB) *", "number"),
    ("project_id",       "KAUST project ID (k####) *", "text"),
    ("pi_username",      "PI username *", "text"),
    ("destination_path", "Destination path *", "text"),
    ("checksum_filename", "Checksum filename *", "text"),
    ("source_path",      "Source path *", "text"),
)


def _missing(yml, field: str) -> bool:
    v = getattr(yml, field)
    return (v or 0) <= 0 if field == "size_gb" else not str(v or "").strip()


def render_mark_staged(yml, yml_path: Path, actor: str, role: Role, db) -> None:
    """
    PREPARING → STAGED. Shows an input for the DM ticket and for every other
    required field that is still empty in the saved record, so everything
    can be completed here; the checklist reflects what is typed, and
    "Mark as staged" saves those values and stages in one step.
    """
    if yml.status != Status.PREPARING:
        return
    import copy
    from iolib.projects import get_project_profile

    with st.expander("Mark as staged", expanded=True):
        profile = get_project_profile(yml.aimcr_reference, db)
        updates: dict = {}
        shown = [f for f in _STAGE_FIELDS if f[0] == "dm_ticket_number" or _missing(yml, f[0])]
        cols = st.columns(2)
        for i, (field, label, kind) in enumerate(shown):
            with cols[i % 2]:
                if kind == "number":
                    val = st.number_input(label, min_value=0.0, step=0.1, format="%.2f",
                                          value=float(getattr(yml, field) or 0.0),
                                          key=_k(yml_path, f"ms_{field}"))
                else:
                    default = getattr(yml, field) or profile.get(field, "")
                    val = st.text_input(label, value=default, key=_k(yml_path, f"ms_{field}"))
            updates[field] = val

        # checklist against what is typed, not only what is saved
        preview = copy.deepcopy(yml)
        for f, v in updates.items():
            setattr(preview, f, float(v) if f == "size_gb" else str(v).strip())
        if not preview.checksum_filename.strip():
            try:
                job = cj.load_job(cj.job_dir_for(yml))
                if job and (job.get("original_checksum_file") or job.get("checksum_file")):
                    preview.checksum_filename = job.get("original_checksum_file") or job["checksum_file"]
            except Exception:
                pass
        missing = preview.validate_curation_fields()
        if missing:
            st.warning("Still needed before staging:  \n- " + "  \n- ".join(missing))
        else:
            st.success("Everything required is filled in.")
        if not yml.checksum_verified_cpup.verified:
            st.caption("The CPUP checksum is not verified yet.")

        if st.button("Mark as staged", key=_k(yml_path, "ms_apply"), type="primary",
                     disabled=bool(missing)):
            clean = {f: (float(v) if f == "size_gb" else str(v).strip()) for f, v in updates.items()}
            res = MarkStagedService().execute(yml, actor, role, db, updates=clean)
            if res.success:
                st.success(res.message)
                st.rerun()
            for e in res.errors:
                st.error(e)
