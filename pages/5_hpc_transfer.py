"""
pages/5_hpc_transfer.py
-----------------------
HPC pipeline — ticket-driven data transfers.

No YAML files. Database is the sole source of truth.

Three tabs:
  1. New transfer  — fill details, generate pre-move checksum Slurm script
  2. Post-move     — generate verify Slurm script, import result, log counts
  3. Transfer log  — view all transfers with status and audit trail
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from auth.session import current_username, init_session, require_role
from config import Role, TransferStatus, TransferType
from iolib.transfers_db import TransfersDatabase


# ── session helpers ───────────────────────────────────────────────────────────

def _tdb() -> TransfersDatabase:
    if "transfers_db" not in st.session_state:
        st.session_state["transfers_db"] = TransfersDatabase()
    return st.session_state["transfers_db"]


def _actor() -> str:
    return current_username()


# ── slurm script generators ───────────────────────────────────────────────────

def _premove_script(ticket: str, name: str, src: str) -> str:
    return f"""#!/bin/bash
#SBATCH --job-name=checksum-generate
#SBATCH --partition=shared
#SBATCH --time=04:00:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --output=checksum_generate_%j.log

# DART — pre-move checksum script
# Ticket : {ticket}
# Artifact: {name}
# Submission is the operator's responsibility

SOURCE_DIR="{src}"
CHECKSUM_FILE="${{SOURCE_DIR}}/checksum.sha256"

echo "Generating checksum for: ${{SOURCE_DIR}}"
echo "Started: $(date)"
echo ""

# relative paths — checksum works wherever the data is moved to
cd "${{SOURCE_DIR}}" || exit 1
find . -type f -print0 \\
  | sort -z \\
  | xargs -0 sha256sum \\
  > "${{CHECKSUM_FILE}}"

echo ""
echo "Checksum written to: ${{CHECKSUM_FILE}}"
echo "File count: $(wc -l < "${{CHECKSUM_FILE}}")"
echo "Completed: $(date)"
"""


def _postmove_script(ticket: str, name: str, dst: str) -> str:
    return f"""#!/bin/bash
#SBATCH --job-name=checksum-verify
#SBATCH --partition=shared
#SBATCH --time=04:00:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --output=checksum_verify_%j.log

# DART — post-move checksum verification script
# Ticket : {ticket}
# Artifact: {name}
# Submission is the operator's responsibility

DEST_DIR="{dst}"
CHECKSUM_FILE="${{DEST_DIR}}/checksum.sha256"
RESULT_FILE="${{DEST_DIR}}/verify_result.txt"

echo "Verifying checksum at: ${{DEST_DIR}}"
echo "Started: $(date)"
echo ""

cd "${{DEST_DIR}}" || exit 1
sha256sum -c "${{CHECKSUM_FILE}}" > "${{RESULT_FILE}}" 2>&1
EXIT_CODE=$?

TOTAL=$(wc -l < "${{CHECKSUM_FILE}}")
FAILED=$(grep -c "FAILED" "${{RESULT_FILE}}" || echo 0)
PASSED=$((TOTAL - FAILED))

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Total files : ${{TOTAL}}"
echo "  Passed      : ${{PASSED}}"
echo "  Failed      : ${{FAILED}}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ "${{FAILED}}" -gt 0 ]; then
    echo "ANOMALIES DETECTED — review ${{RESULT_FILE}}"
    grep "FAILED" "${{RESULT_FILE}}"
    exit 1
fi

echo "All files verified OK"
echo "Completed: $(date)"
"""


# ── verify_result.txt parser ──────────────────────────────────────────────────

def _parse_result_file(path_str: str) -> tuple[int, int, int] | None:
    """
    Parse a verify_result.txt produced by the post-move Slurm script.
    Returns (total, passed, failed) or None if the file cannot be read.
    Lines look like:
        ./path/to/file: OK
        ./path/to/file: FAILED
    """
    try:
        text = Path(path_str.strip()).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None

    lines  = [l.strip() for l in text.splitlines() if l.strip()]
    passed = sum(1 for l in lines if l.endswith(": OK"))
    failed = sum(1 for l in lines if "FAILED" in l)
    total  = passed + failed
    if total == 0:
        return None
    return total, passed, failed


# ── status badge ──────────────────────────────────────────────────────────────

_STATUS_COLORS = {
    TransferStatus.PENDING:     ("#7a82a0", "rgba(122,130,160,0.1)"),
    TransferStatus.PREMOVE:     ("#6c8fff", "rgba(108,143,255,0.12)"),
    TransferStatus.IN_PROGRESS: ("#e89440", "rgba(232,148,64,0.12)"),
    TransferStatus.VERIFYING:   ("#6c8fff", "rgba(108,143,255,0.12)"),
    TransferStatus.COMPLETED:   ("#1dbf8a", "rgba(29,191,138,0.12)"),
    TransferStatus.ANOMALY:     ("#d95555", "rgba(217,85,85,0.1)"),
    TransferStatus.BLOCKED:     ("#e89440", "rgba(232,148,64,0.12)"),
}


def _badge(status: str) -> str:
    try:
        s = TransferStatus(status)
        fg, bg = _STATUS_COLORS[s]
    except (ValueError, KeyError):
        fg, bg = "#7a82a0", "rgba(122,130,160,0.1)"
    return (
        f"<span style='background:{bg};color:{fg};"
        f"padding:2px 9px;border-radius:3px;"
        f"font-size:0.78em;font-weight:600;font-family:monospace;"
        f"letter-spacing:0.06em'>{status.upper()}</span>"
    )


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    init_session()

    if not require_role(Role.CURATOR, Role.MOVER):
        st.stop()

    actor = _actor()
    tdb   = _tdb()

    st.title("HPC Transfer")
    st.caption(
        "Ticket-driven data transfers — no YAML, DB-only record. "
        "Checksum Slurm scripts are generated here; submission is your responsibility."
    )

    tab_new, tab_post, tab_log = st.tabs([
        "New transfer",
        "Post-move verification",
        "Transfer log",
    ])

    # ── tab 1: new transfer ───────────────────────────────────────────────────
    with tab_new:
        _render_new_transfer(actor, tdb)

    # ── tab 2: post-move ──────────────────────────────────────────────────────
    with tab_post:
        _render_postmove(actor, tdb)

    # ── tab 3: log ────────────────────────────────────────────────────────────
    with tab_log:
        _render_log(tdb)


# ── new transfer ──────────────────────────────────────────────────────────────

def _render_new_transfer(actor: str, tdb: TransfersDatabase) -> None:
    st.subheader("New transfer")
    st.caption(
        "Fill the transfer details, then generate the pre-move checksum script. "
        "Submit the script at the source path — this dashboard does not submit jobs."
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
            size_gb = st.number_input(
                "Size (GB)", min_value=0.0, step=0.1, format="%.3f"
            )
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
            try:
                tid = tdb.create_transfer(
                    ticket_number    = ticket.strip(),
                    requester        = requester.strip(),
                    artifact_name    = art_name.strip(),
                    artifact_type    = art_type,
                    source_path      = src.strip(),
                    destination_path = dst.strip(),
                    operator         = actor,
                    size_gb          = float(size_gb) if size_gb else None,
                    notes            = notes.strip(),
                )
                st.success(f"Transfer record created (ID: {tid}).")
                st.session_state["new_transfer_id"] = tid
            except Exception as exc:
                st.error(f"Failed to create record: {exc}")

    # ── pre-move slurm script ──────────────────────────────────────────────────
    st.divider()
    st.subheader("Pre-move — generate checksum script")
    st.caption(
        "Uses `find | sort | xargs sha256sum` with relative paths so the checksum "
        "file travels with the data and verifies correctly at the destination."
    )

    with st.form("premove_script_form"):
        c1, c2 = st.columns(2)
        with c1:
            ps_ticket = st.text_input(
                "Ticket number",
                value=st.session_state.get("new_transfer_id", ""),
                placeholder="RT-12345",
                key="ps_ticket",
            )
        with c2:
            ps_name = st.text_input(
                "Artifact name",
                placeholder="my_dataset",
                key="ps_name",
            )
        ps_src = st.text_input(
            "Source path",
            placeholder="/scratch/project/k03/support_team/65616/datasets/my_dataset",
            key="ps_src",
        )
        generate = st.form_submit_button("Generate pre-move script")

    if generate and ps_src.strip():
        script = _premove_script(
            ps_ticket.strip() or "RT-XXXX",
            ps_name.strip()   or "artifact",
            ps_src.strip(),
        )
        st.warning(
            "⚠️ Submission is your responsibility. "
            "Copy this script and submit it at the source path on Shaheen3.",
            icon="⚠️",
        )
        st.code(script, language="bash")

        # mark record as pre-move if we have a transfer ID
        tid_str = ps_ticket.strip()
        if tid_str.isdigit():
            try:
                tdb.update_status(
                    int(tid_str), TransferStatus.PREMOVE, actor,
                    "Pre-move checksum script generated."
                )
                st.caption(f"Transfer ID {tid_str} status → pre-move.")
            except Exception:
                pass


# ── post-move ─────────────────────────────────────────────────────────────────

def _render_postmove(actor: str, tdb: TransfersDatabase) -> None:
    st.subheader("Post-move verification")
    st.caption(
        "Data has been moved. Generate the verification script, submit it at the "
        "destination, then import the result file here to complete the record."
    )

    # ── generate verify script ────────────────────────────────────────────────
    st.markdown("**Step 1 — generate verification script**")

    with st.form("postmove_script_form"):
        c1, c2 = st.columns(2)
        with c1:
            pv_ticket = st.text_input("Ticket / Transfer ID", placeholder="RT-12345 or DB ID")
            pv_name   = st.text_input("Artifact name",         placeholder="my_dataset")
        with c2:
            pv_dst = st.text_input(
                "Destination path",
                placeholder="/scratch/project/k#####/datasets/my_dataset",
            )
        gen_verify = st.form_submit_button("Generate verify script")

    if gen_verify and pv_dst.strip():
        script = _postmove_script(
            pv_ticket.strip() or "RT-XXXX",
            pv_name.strip()   or "artifact",
            pv_dst.strip(),
        )
        st.warning(
            "⚠️ Submit this at the **destination path**. "
            "Result is written to `verify_result.txt` in that directory.",
            icon="⚠️",
        )
        st.code(script, language="bash")

    st.divider()

    # ── import result ─────────────────────────────────────────────────────────
    st.markdown("**Step 2 — import verification result**")
    st.caption(
        "After the verification job completes, import `verify_result.txt` here. "
        "The pass/fail counts are parsed and stored in the transfer record."
    )

    with st.form("import_result_form"):
        c1, c2 = st.columns(2)
        with c1:
            ir_tid = st.text_input(
                "Transfer ID (from transfer log) *",
                placeholder="42",
            )
        with c2:
            ir_path = st.text_input(
                "Path to verify_result.txt *",
                placeholder="/scratch/project/k#####/datasets/my_dataset/verify_result.txt",
            )
        import_result = st.form_submit_button("Import result & complete record", type="primary")

    if import_result:
        if not ir_tid.strip() or not ir_path.strip():
            st.error("Transfer ID and result file path are both required.")
        elif not ir_tid.strip().isdigit():
            st.error("Transfer ID must be a number — check the Transfer log tab.")
        else:
            counts = _parse_result_file(ir_path.strip())
            if counts is None:
                st.error(
                    f"Could not parse `{ir_path.strip()}`. "
                    "Make sure the verification job has completed and the file exists."
                )
            else:
                total, passed, failed = counts
                try:
                    tdb.record_verification(
                        int(ir_tid.strip()), total, passed, failed, actor
                    )
                    if failed == 0:
                        st.success(
                            f"✓ Verification complete — {passed}/{total} files OK. "
                            "Transfer marked **completed**."
                        )
                    else:
                        st.error(
                            f"⚠️ Anomalies detected — {failed}/{total} files FAILED. "
                            "Transfer marked **anomaly**. Review the result file."
                        )
                except Exception as exc:
                    st.error(f"Failed to record verification: {exc}")


# ── transfer log ──────────────────────────────────────────────────────────────

def _render_log(tdb: TransfersDatabase) -> None:
    st.subheader("Transfer log")

    # filters
    c1, c2 = st.columns([2, 1])
    with c1:
        search_ticket = st.text_input(
            "Search by ticket", placeholder="RT-...",
            key="log_search", label_visibility="collapsed"
        )
    with c2:
        status_opts = ["All"] + [s.value for s in TransferStatus]
        status_filter = st.selectbox(
            "Status", status_opts, key="log_status",
            label_visibility="collapsed"
        )

    rows = tdb.list_transfers(
        status = None if status_filter == "All" else status_filter,
        ticket = search_ticket.strip() or None,
    )

    if not rows:
        st.info("No transfers found.")
        return

    # table header
    hc = st.columns([1, 2, 2, 1, 1, 2, 1])
    for col, hdr in zip(hc, ["ID", "Ticket", "Artifact", "Type", "Status", "Operator", ""]):
        col.markdown(f"**{hdr}**")
    st.divider()

    for row in rows:
        rc = st.columns([1, 2, 2, 1, 1, 2, 1])
        rc[0].caption(str(row["id"]))
        rc[1].caption(row["ticket_number"])
        rc[2].markdown(f"`{row['artifact_name']}`")
        rc[3].caption(row["artifact_type"])
        rc[4].markdown(_badge(row["status"]), unsafe_allow_html=True)
        rc[5].caption(row["operator"] or "—")

        if rc[6].button("Detail", key=f"log_detail_{row['id']}"):
            st.session_state["log_selected_id"] = row["id"]

    # detail view
    if "log_selected_id" in st.session_state:
        st.divider()
        _render_transfer_detail(tdb, st.session_state["log_selected_id"])


def _render_transfer_detail(tdb: TransfersDatabase, transfer_id: int) -> None:
    row = tdb.get_transfer(transfer_id)
    if row is None:
        st.error(f"Transfer ID {transfer_id} not found.")
        return

    st.markdown(
        f"#### Transfer #{row['id']} — `{row['artifact_name']}`  "
        f"{_badge(row['status'])}",
        unsafe_allow_html=True,
    )

    c1, c2 = st.columns(2)
    with c1:
        st.text_input("Ticket",     value=row["ticket_number"],    disabled=True, key="d_ticket")
        st.text_input("Requester",  value=row["requester"],        disabled=True, key="d_req")
        st.text_input("Source",     value=row["source_path"],      disabled=True, key="d_src")
        st.text_input("Size (GB)",  value=str(row["size_gb"] or "—"), disabled=True, key="d_size")
    with c2:
        st.text_input("Type",        value=row["artifact_type"],   disabled=True, key="d_type")
        st.text_input("Operator",    value=row["operator"] or "—", disabled=True, key="d_op")
        st.text_input("Destination", value=row["destination_path"],disabled=True, key="d_dst")
        st.text_input("Notes",       value=row["notes"] or "—",    disabled=True, key="d_notes")

    # verification counts
    if row["checksum_total"] is not None:
        st.markdown("**Verification result**")
        vc1, vc2, vc3 = st.columns(3)
        vc1.metric("Total files",  row["checksum_total"])
        vc2.metric("Passed",       row["checksum_passed"])
        vc3.metric("Failed",       row["checksum_failed"])

    # audit log
    with st.expander("Audit log", expanded=False):
        log_rows = tdb.get_log(transfer_id)
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

    # status update
    st.markdown("**Update status**")
    c1, c2 = st.columns([1, 2])
    with c1:
        new_status = st.selectbox(
            "New status",
            [s.value for s in TransferStatus],
            key=f"upd_status_{transfer_id}",
        )
    with c2:
        upd_note = st.text_input("Note", key=f"upd_note_{transfer_id}")

    if st.button("Apply", key=f"upd_apply_{transfer_id}"):
        try:
            from auth.session import current_username
            tdb.update_status(
                transfer_id, TransferStatus(new_status),
                current_username(), upd_note
            )
            st.success("Status updated.")
            st.rerun()
        except Exception as exc:
            st.error(f"Failed: {exc}")


main()
