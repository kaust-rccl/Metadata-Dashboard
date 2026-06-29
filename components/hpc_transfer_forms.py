"""
components/hpc_transfer_forms.py
---------------------------------
All Streamlit render functions for the HPC pipeline page.

Imports:
  - iolib.hpc_transfer_service  for all DB + YML writes
  - components.hpc_slurm        for script generation and workspace helpers
  - models.hpc_transfer         for HpcTransfer, status_badge, STATUS_COLORS
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from auth.session import current_username
from config import TransferStatus, TransferType
from iolib.hpc_transfer_service import HpcTransferService
from iolib.hpc_yml import load_modlog
from models.hpc_transfer import HpcTransfer, STATUS_COLORS, status_badge
from components.hpc_slurm import (
    checksum_path, postmove_script, premove_script,
    scripts_on_disk, workspace_dir, write_script,
)

# fields that affect which scripts — used for sync warnings
_PREMOVE_FIELDS  = {"ticket_number", "artifact_name", "source_path"}
_POSTMOVE_FIELDS = {"ticket_number", "artifact_name", "destination_path"}


def _actor() -> str:
    return current_username()


# ── transfer selector ─────────────────────────────────────────────────────────

def transfer_selector(svc: HpcTransferService, key: str) -> dict | None:
    """
    Selectbox over all existing transfers.
    Returns a hydrated dict or None if no transfers exist.
    """
    rows = svc.list_transfers()
    if not rows:
        st.info("No transfers yet — create one in the New transfer tab.")
        return None
    opts = {
        f"[{r['id']}] {r['ticket_number']} — {r['artifact_name']}": r
        for r in rows
    }
    choice = st.selectbox("Select transfer", list(opts.keys()), key=key)
    row = opts[choice]
    return {
        "id":     row["id"],
        "ticket": row["ticket_number"],
        "name":   row["artifact_name"],
        "src":    row["source_path"],
        "dst":    row["destination_path"],
    }


# ── verify_result.txt parser ──────────────────────────────────────────────────

def parse_result_file(path_str: str) -> tuple[int, int, int] | None:
    """
    Parse a verify_result.txt.
    Returns (total, passed, failed) or None.
    """
    try:
        text = Path(path_str.strip()).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    lines  = [l.strip() for l in text.splitlines() if l.strip()]
    passed = sum(1 for l in lines if l.endswith(": OK"))
    failed = sum(1 for l in lines if "FAILED" in l)
    total  = passed + failed
    return (total, passed, failed) if total > 0 else None


# ── render functions ──────────────────────────────────────────────────────────

def render_new_transfer(actor: str, svc: HpcTransferService) -> None:
    st.subheader("New transfer")
    st.caption(
        "Fill the transfer details to create a record. "
        "Then go to the Pre-move tab to generate the checksum script."
    )

    with st.form("new_transfer_form"):
        c1, c2 = st.columns(2)
        with c1:
            ticket    = st.text_input("RT ticket number *", placeholder="RT-12345")
            art_name  = st.text_input("Artifact name *",    placeholder="my_dataset")
            src       = st.text_input("Source path *",
                placeholder="/scratch/project/k03/support_team/65616/datasets/my_dataset")
        with c2:
            requester = st.text_input("Requester username *", placeholder="pi_username")
            art_type  = st.selectbox("Type", [t.value for t in TransferType])
            dst       = st.text_input("Destination path *",
                placeholder="/scratch/project/k#####/datasets/my_dataset")

        c1, c2 = st.columns(2)
        with c1:
            size_gb = st.number_input("Size (GB)", min_value=0.0, step=0.1, format="%.3f")
        with c2:
            notes = st.text_input("Notes", placeholder="Any context for this transfer")

        submitted = st.form_submit_button("Create transfer record", type="primary")

    if submitted:
        missing = [f for f, v in [
            ("RT ticket number", ticket),
            ("Artifact name",    art_name),
            ("Source path",      src),
            ("Destination path", dst),
            ("Requester",        requester),
        ] if not v.strip()]

        if missing:
            for m in missing:
                st.error(f"{m} is required.")
        else:
            ws = workspace_dir(ticket.strip(), art_name.strip())
            ok, msg, tid = svc.create(
                ticket_number    = ticket.strip(),
                requester        = requester.strip(),
                artifact_name    = art_name.strip(),
                artifact_type    = art_type,
                source_path      = src.strip(),
                destination_path = dst.strip(),
                operator         = actor,
                workspace        = ws,
                size_gb          = float(size_gb) if size_gb else None,
                notes            = notes.strip(),
            )
            if ok:
                st.success(f"{msg}  \nGo to the **Pre-move** tab to generate the checksum script.")
            else:
                st.error(f"Failed to create record: {msg}")


def render_premove(actor: str, svc: HpcTransferService) -> None:
    st.subheader("Pre-move — generate checksum script")
    st.caption(
        "Select a transfer, generate the checksum Slurm script, "
        "and submit it yourself at the source path."
    )

    sel = transfer_selector(svc, key="pre_selector")
    if sel is None:
        return

    st.divider()

    ws = workspace_dir(sel["ticket"], sel["name"])
    c1, c2 = st.columns(2)
    with c1:
        st.text_input("Ticket",      value=sel["ticket"], disabled=True, key="pre_ticket")
        st.text_input("Source path", value=sel["src"],    disabled=True, key="pre_src")
    with c2:
        st.text_input("Artifact",    value=sel["name"],   disabled=True, key="pre_name")

    st.caption(f"Workspace: `{ws}`")
    chk = checksum_path(sel["src"])
    st.info(f"Checksum file will be written to: `{chk}`", icon="ℹ️")

    if st.button("Generate & save pre-move script", type="primary", key="gen_pre"):
        try:
            script = premove_script(sel["ticket"], sel["name"], sel["src"], ws)
            out    = write_script(ws, "generate_checksum.slurm", script)
            ok, msg = svc.set_premove(sel["id"], actor, ws)
            if ok:
                st.success(f"Script written to: `{out}`")
                st.warning(
                    "Submission is your responsibility.  \n"
                    f"```bash\ncd {ws}\nsbatch generate_checksum.slurm\n```",
                    icon="⚠️",
                )
            else:
                st.error(f"Script written but status update failed: {msg}")
        except Exception as exc:
            st.error(f"Failed to write script: {exc}")


def render_postmove(actor: str, svc: HpcTransferService) -> None:
    st.subheader("Post-move verification")
    st.caption(
        "Generate the verification script, submit it at the destination, "
        "then import the result to complete the record."
    )

    sel = transfer_selector(svc, key="post_selector")
    if sel is None:
        return

    st.divider()

    ws          = workspace_dir(sel["ticket"], sel["name"])
    result_file = ws / "verify_result.txt"

    c1, c2 = st.columns(2)
    with c1:
        st.text_input("Ticket",           value=sel["ticket"], disabled=True, key="post_ticket")
        st.text_input("Destination path", value=sel["dst"],    disabled=True, key="post_dst")
    with c2:
        st.text_input("Artifact",         value=sel["name"],   disabled=True, key="post_name")

    st.caption(f"Workspace: `{ws}`")
    chk = checksum_path(sel["dst"])
    st.info(f"Checksum file expected at: `{chk}`", icon="ℹ️")

    # step 1: generate verify script
    st.markdown("**Step 1 — generate verification script**")
    if st.button("Generate & save verify script", type="primary", key="gen_post"):
        try:
            script = postmove_script(sel["ticket"], sel["name"], sel["dst"], ws)
            out    = write_script(ws, "verify_checksum.slurm", script)
            ok, msg = svc.set_verifying(sel["id"], actor, ws)
            if ok:
                st.success(
                    f"Script written to: `{out}`  \n"
                    f"Result file path: `{result_file}`"
                )
                st.warning(
                    "Submission is your responsibility. \n"
                    f"```bash\ncd {ws}\nsbatch verify_checksum.slurm\n```",
                    icon="⚠️",
                )
            else:
                st.error(f"Script written but status update failed: {msg}")
        except Exception as exc:
            st.error(f"Failed to write script: {exc}")

    st.divider()

    # step 2: import result
    st.markdown("**Step 2 — import verification result**")
    ir_path = st.text_input(
        "Path to verify_result.txt",
        value=str(result_file),
        key="ir_path",
    )
    st.text_input("Transfer ID", value=str(sel["id"]), disabled=True, key="ir_tid")

    if st.button("Import result & complete record", type="primary", key="import_result"):
        counts = parse_result_file(ir_path.strip())
        if counts is None:
            st.error(
                f"Could not parse `{ir_path.strip()}`.  \n"
                "Make sure the verification job has completed and the file exists."
            )
        else:
            total, passed, failed = counts
            ok, msg = svc.record_verification(sel["id"], total, passed, failed, actor, ws)
            if failed == 0:
                st.success(msg)
            else:
                st.error(msg)


def render_log(svc: HpcTransferService) -> None:
    st.subheader("Transfer log")

    st.markdown("""
<style>
div[data-testid="stButton"] button { white-space: nowrap; min-width: fit-content; }
</style>
""", unsafe_allow_html=True)

    c1, c2 = st.columns([2, 1])
    with c1:
        search = st.text_input(
            "Search", placeholder="RT-...",
            key="log_search", label_visibility="collapsed"
        )
    with c2:
        status_opts   = ["All"] + [s.value for s in TransferStatus]
        status_filter = st.selectbox(
            "Status", status_opts, key="log_status",
            label_visibility="collapsed"
        )

    rows = svc.list_transfers(
        status = None if status_filter == "All" else status_filter,
        ticket = search.strip() or None,
    )

    if not rows:
        st.info("No transfers found.")
        return

    # header
    st.markdown(
        "<div style='display:grid;grid-template-columns:1fr 2fr 2fr 2fr 2fr 2fr 1.5fr;"
        "gap:0.5rem;padding:6px 0;border-bottom:1px solid rgba(255,255,255,0.1)'>"
        + "".join(
            f"<span style='font-size:1em;font-weight:600;color:#7a82a0'>{h}</span>"
            for h in ["ID", "Ticket", "Artifact", "Type", "Status", "Operator", ""]
        )
        + "</div>",
        unsafe_allow_html=True,
    )

    for row in rows:
        st.write("")
        rc = st.columns([1, 2, 2, 2, 2, 2, 1.5])
        rc[0].caption(str(row["id"]))
        rc[1].caption(row["ticket_number"])
        rc[2].markdown(f"`{row['artifact_name']}`")
        rc[3].caption(row["artifact_type"])
        rc[4].markdown(status_badge(row["status"]), unsafe_allow_html=True)
        rc[5].caption(row["operator"] or "—")
        if rc[6].button("Detail", key=f"log_detail_{row['id']}"):
            st.session_state["log_selected_id"] = row["id"]
        st.divider()

    if "log_selected_id" in st.session_state:
        st.divider()
        render_transfer_detail(svc, st.session_state["log_selected_id"])


def render_transfer_detail(svc: HpcTransferService, transfer_id: int) -> None:
    row = svc.get_transfer(transfer_id)
    if row is None:
        st.error(f"Transfer ID {transfer_id} not found.")
        return

    ws      = workspace_dir(row["ticket_number"], row["artifact_name"])
    scripts = scripts_on_disk(ws)

    st.markdown(
        f"#### Transfer #{row['id']} — `{row['artifact_name']}`  "
        f"{status_badge(row['status'])}",
        unsafe_allow_html=True,
    )
    st.caption(f"Workspace: `{ws}`")

    # ── edit form ─────────────────────────────────────────────────────────────
    with st.expander("Edit record", expanded=True):
        with st.form(f"edit_transfer_{transfer_id}"):
            c1, c2 = st.columns(2)
            with c1:
                new_ticket = st.text_input("RT ticket number", value=row["ticket_number"], key=f"e_ticket_{transfer_id}")
                new_name   = st.text_input("Artifact name",    value=row["artifact_name"], key=f"e_name_{transfer_id}")
                new_src    = st.text_input("Source path",      value=row["source_path"],   key=f"e_src_{transfer_id}")
                new_size   = st.number_input("Size (GB)", value=float(row["size_gb"] or 0.0),
                                             min_value=0.0, step=0.1, format="%.3f", key=f"e_size_{transfer_id}")
            with c2:
                new_req  = st.text_input("Requester",        value=row["requester"],         key=f"e_req_{transfer_id}")
                new_type = st.selectbox("Type", [t.value for t in TransferType],
                                        index=[t.value for t in TransferType].index(row["artifact_type"])
                                        if row["artifact_type"] in [t.value for t in TransferType] else 0,
                                        key=f"e_type_{transfer_id}")
                new_dst  = st.text_input("Destination path", value=row["destination_path"], key=f"e_dst_{transfer_id}")
                new_notes = st.text_input("Notes",           value=row["notes"] or "",      key=f"e_notes_{transfer_id}")

            saved = st.form_submit_button("Save changes", type="primary")

        if saved:
            changed = {
                k: v for k, v in {
                    "ticket_number":    new_ticket.strip(),
                    "artifact_name":    new_name.strip(),
                    "source_path":      new_src.strip(),
                    "destination_path": new_dst.strip(),
                    "requester":        new_req.strip(),
                    "artifact_type":    new_type,
                    "size_gb":          float(new_size),
                    "notes":            new_notes.strip(),
                }.items()
                if str(v) != str(row[k] if k != "size_gb" else float(row["size_gb"] or 0.0))
            }

            if not changed:
                st.info("No changes detected.")
            else:
                premove_affected  = scripts["premove"]  and bool(changed.keys() & _PREMOVE_FIELDS)
                postmove_affected = scripts["postmove"] and bool(changed.keys() & _POSTMOVE_FIELDS)

                if premove_affected or postmove_affected:
                    affected = []
                    if premove_affected:
                        affected.append(
                            f"`generate_checksum.slurm` — "
                            f"{', '.join(f'`{f}`' for f in changed.keys() & _PREMOVE_FIELDS)}"
                        )
                    if postmove_affected:
                        affected.append(
                            f"`verify_checksum.slurm` — "
                            f"{', '.join(f'`{f}`' for f in changed.keys() & _POSTMOVE_FIELDS)}"
                        )
                    st.warning(
                        "⚠️ **Scripts out of sync** — regenerate from Pre-move / Post-move tabs:  \n"
                        + "  \n".join(f"- {s}" for s in affected),
                        icon="⚠️",
                    )

                ok, note = svc.update_fields(transfer_id, _actor(), ws, row, **changed)
                if ok:
                    st.success(f"Record updated.  \nChanges: {note}")
                    if not premove_affected and not postmove_affected:
                        st.rerun()
                else:
                    st.error(f"Failed to save: {note}")

    # ── verification result ───────────────────────────────────────────────────
    if row["checksum_total"] is not None:
        st.markdown("**Verification result**")
        vc1, vc2, vc3 = st.columns(3)
        vc1.metric("Total files", row["checksum_total"])
        vc2.metric("Passed",      row["checksum_passed"])
        vc3.metric("Failed",      row["checksum_failed"])

    # ── modlog viewer ─────────────────────────────────────────────────────────
    with st.expander("Modification log", expanded=False):
        entries = load_modlog(ws)
        if not entries:
            st.caption("No log entries.")
        else:
            for e in reversed(entries):
                ts = str(e.get("timestamp", ""))[:16].replace("T", " ")
                st.markdown(
                    f"<div style='border-left:3px solid #2e3548;"
                    f"padding:4px 10px;margin-bottom:6px;font-size:0.85em'>"
                    f"<b>{e.get('actor','')}</b> · {ts} · "
                    f"<code style='font-size:0.85em'>{e.get('action','')}</code><br>"
                    f"{e.get('note','')}</div>",
                    unsafe_allow_html=True,
                )

    # ── audit log (DB) ────────────────────────────────────────────────────────
    with st.expander("Audit log (DB)", expanded=False):
        log_rows = svc.get_log(transfer_id)
        if not log_rows:
            st.caption("No log entries.")
        else:
            for entry in log_rows:
                ts = entry["timestamp"][:16].replace("T", " ")
                st.markdown(
                    f"<div style='border-left:3px solid #2e3548;"
                    f"padding:4px 10px;margin-bottom:6px;font-size:0.85em'>"
                    f"<b>{entry['actor']}</b> · {ts} · "
                    f"<code style='font-size:0.85em'>{entry['action']}</code><br>"
                    f"{entry['note'] or ''}</div>",
                    unsafe_allow_html=True,
                )

    # ── status update ─────────────────────────────────────────────────────────
    st.markdown("**Update status**")
    c1, c2 = st.columns([1, 2])
    with c1:
        new_status = st.selectbox(
            "New status", [s.value for s in TransferStatus],
            key=f"upd_status_{transfer_id}",
        )
    with c2:
        upd_note = st.text_input("Note", key=f"upd_note_{transfer_id}")

    if st.button("Apply", key=f"upd_apply_{transfer_id}"):
        ok, msg = svc.update_status(transfer_id, TransferStatus(new_status), _actor(), upd_note)
        if ok:
            st.success(msg)
            st.rerun()
        else:
            st.error(f"Failed: {msg}")

    # ── delete ────────────────────────────────────────────────────────────────
    st.divider()
    if st.button("🗑 Delete this record", key=f"del_{transfer_id}", type="secondary"):
        ok, msg = svc.delete(transfer_id)
        if ok:
            del st.session_state["log_selected_id"]
            st.success(msg)
            st.rerun()
        else:
            st.error(f"Failed: {msg}")