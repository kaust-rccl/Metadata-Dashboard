"""
iolib/hpc_transfer_service.py
------------------------------
Service layer for HPC pipeline transfers.

Wraps TransfersDatabase with higher-level operations that also keep
the workspace YML in sync with every DB write.

All methods return (success: bool, message: str).
"""

from __future__ import annotations

import logging
from pathlib import Path

from config import TransferStatus
from iolib.transfers_db import TransfersDatabase
from iolib.hpc_yml import append_modlog, save_yml, update_yml, load_yml
from models.hpc_transfer import HpcTransfer

logger = logging.getLogger(__name__)


class HpcTransferService:
    """
    Facade over TransfersDatabase + workspace YML.
    One instance per Streamlit session — stored in session_state.
    """

    def __init__(self, db: TransfersDatabase) -> None:
        self._db = db

    # ── create ────────────────────────────────────────────────────────────────

    def create(
        self,
        ticket_number: str,
        requester: str,
        artifact_name: str,
        artifact_type: str,
        source_path: str,
        destination_path: str,
        operator: str,
        workspace: Path,
        size_gb: float | None = None,
        notes: str = "",
    ) -> tuple[bool, str, int | None]:
        """
        Create a DB record and write the initial YML.
        Returns (success, message, transfer_id).
        """
        try:
            tid = self._db.create_transfer(
                ticket_number    = ticket_number,
                requester        = requester,
                artifact_name    = artifact_name,
                artifact_type    = artifact_type,
                source_path      = source_path,
                destination_path = destination_path,
                operator         = operator,
                size_gb          = size_gb,
                notes            = notes,
            )
            row = self._db.get_transfer(tid)
            if row:
                t = HpcTransfer.from_row(row)
                save_yml(workspace, t.to_yml_dict())
                append_modlog(workspace, operator, "created", f"Transfer record created. DB ID: {tid}")
            return True, f"Transfer record created (ID: **{tid}**).", tid
        except Exception as exc:
            logger.error("HpcTransferService.create failed: %s", exc)
            return False, str(exc), None

    # ── set premove ───────────────────────────────────────────────────────────

    def set_premove(
        self, transfer_id: int, actor: str, workspace: Path
    ) -> tuple[bool, str]:
        try:
            self._db.update_status(
                transfer_id, TransferStatus.PREMOVE, actor,
                "Pre-move checksum script generated."
            )
            update_yml(workspace, actor, status="pre-move")
            append_modlog(workspace, actor, "pre-move", "Checksum script generated.")
            return True, "Status → pre-move."
        except Exception as exc:
            return False, str(exc)

    # ── set verifying ─────────────────────────────────────────────────────────

    def set_verifying(
        self, transfer_id: int, actor: str, workspace: Path
    ) -> tuple[bool, str]:
        try:
            self._db.update_status(
                transfer_id, TransferStatus.VERIFYING, actor,
                "Post-move verification script generated."
            )
            update_yml(workspace, actor, status="verifying")
            append_modlog(workspace, actor, "verifying", "Verify script generated.")
            return True, "Status → verifying."
        except Exception as exc:
            return False, str(exc)

    # ── record verification result ────────────────────────────────────────────

    def record_verification(
        self,
        transfer_id: int,
        total: int,
        passed: int,
        failed: int,
        actor: str,
        workspace: Path,
    ) -> tuple[bool, str]:
        try:
            self._db.record_verification(transfer_id, total, passed, failed, actor)
            final = "completed" if failed == 0 else "anomaly"
            update_yml(
                workspace, actor,
                status=final,
                checksum_verified_src=True,
            )
            append_modlog(
                workspace, actor, final,
                f"Verified: {passed}/{total} OK, {failed} failed."
            )
            if failed == 0:
                return True, f"✓ {passed}/{total} files OK — marked **completed**."
            else:
                return False, f"⚠️ {failed}/{total} files FAILED — marked **anomaly**."
        except Exception as exc:
            return False, str(exc)

    # ── update fields ─────────────────────────────────────────────────────────

    def update_fields(
        self,
        transfer_id: int,
        actor: str,
        workspace: Path,
        row,               # current DB row for diff note
        **changed,
    ) -> tuple[bool, str]:
        try:
            self._db.update_fields(transfer_id, actor, **changed)
            note = "; ".join(
                f"{k}: '{row[k]}' → '{v}'"
                for k, v in changed.items()
            )
            # mirror changes into YML
            yml_data = load_yml(workspace)
            if yml_data:
                yml_data.update(changed)
                save_yml(workspace, yml_data)
            append_modlog(workspace, actor, "updated", note)
            return True, note
        except Exception as exc:
            return False, str(exc)

    # ── update status ─────────────────────────────────────────────────────────

    def update_status(
        self,
        transfer_id: int,
        status: TransferStatus,
        actor: str,
        note: str = "",
    ) -> tuple[bool, str]:
        try:
            self._db.update_status(transfer_id, status, actor, note)
            return True, f"Status → {status.value}."
        except Exception as exc:
            return False, str(exc)

    # ── delete ────────────────────────────────────────────────────────────────

    def delete(self, transfer_id: int) -> tuple[bool, str]:
        try:
            self._db._conn.execute(
                "DELETE FROM transfer_log WHERE transfer_id = ?", (transfer_id,)
            )
            self._db._conn.execute(
                "DELETE FROM transfers WHERE id = ?", (transfer_id,)
            )
            self._db._conn.commit()
            return True, "Record deleted."
        except Exception as exc:
            return False, str(exc)

    # ── read ──────────────────────────────────────────────────────────────────

    def list_transfers(self, status=None, ticket=None):
        return self._db.list_transfers(status=status, ticket=ticket)

    def get_transfer(self, transfer_id: int):
        return self._db.get_transfer(transfer_id)

    def get_log(self, transfer_id: int):
        return self._db.get_log(transfer_id)