"""
iolib/transfers_db.py
---------------------
SQLite interface for the HPC pipeline (ticket-driven, DB-only transfers).

Completely separate from the AIMCR tracker.db — no FK relationships,
no shared tables, no YML backing.

Schema: one table (transfers) + one table (transfer_log) for audit trail.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import TRANSFERS_DB_PATH, TransferStatus

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS transfers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_number       TEXT NOT NULL,
    requester           TEXT NOT NULL,
    artifact_name       TEXT NOT NULL,
    artifact_type       TEXT NOT NULL,
    size_gb             REAL,
    source_path         TEXT NOT NULL,
    destination_path    TEXT NOT NULL,
    notes               TEXT,
    status              TEXT NOT NULL DEFAULT 'pending',
    operator            TEXT,
    checksum_total      INTEGER,
    checksum_passed     INTEGER,
    checksum_failed     INTEGER,
    created_at          TEXT NOT NULL,
    last_modified       TEXT,
    last_modified_by    TEXT
);

CREATE TABLE IF NOT EXISTS transfer_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id  INTEGER NOT NULL REFERENCES transfers(id),
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    note         TEXT,
    timestamp    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transfers_ticket  ON transfers(ticket_number);
CREATE INDEX IF NOT EXISTS idx_transfers_status  ON transfers(status);
CREATE INDEX IF NOT EXISTS idx_transfer_log_tid  ON transfer_log(transfer_id);
"""


class TransfersDatabase:
    """
    Manages the HPC pipeline transfers database.
    One instance per app session, stored in st.session_state.
    """

    def __init__(self, path: Path = TRANSFERS_DB_PATH) -> None:
        self._path = path
        self._conn = self._connect()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=DELETE;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.executescript(_DDL)
        conn.commit()
        return conn

    @contextmanager
    def _tx(self):
        try:
            yield
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ── create ────────────────────────────────────────────────────────────────

    def create_transfer(
        self,
        ticket_number: str,
        requester: str,
        artifact_name: str,
        artifact_type: str,
        source_path: str,
        destination_path: str,
        operator: str,
        size_gb: Optional[float] = None,
        notes: str = "",
    ) -> int:
        """
        Insert a new transfer record. Returns the new row ID.
        """
        now = datetime.now(tz=timezone.utc).isoformat()
        with self._tx():
            cur = self._conn.execute(
                """
                INSERT INTO transfers
                    (ticket_number, requester, artifact_name, artifact_type,
                     size_gb, source_path, destination_path, notes,
                     status, operator, created_at, last_modified, last_modified_by)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    ticket_number, requester, artifact_name, artifact_type,
                    size_gb, source_path, destination_path, notes,
                    TransferStatus.PENDING.value, operator,
                    now, now, operator,
                ),
            )
            transfer_id = cur.lastrowid
            self._append_log(transfer_id, operator, "created", "Transfer record created.")
        return transfer_id

    # ── update status ─────────────────────────────────────────────────────────

    def update_status(
        self,
        transfer_id: int,
        status: TransferStatus,
        actor: str,
        note: str = "",
    ) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        with self._tx():
            self._conn.execute(
                """
                UPDATE transfers
                SET status=?, last_modified=?, last_modified_by=?
                WHERE id=?
                """,
                (status.value, now, actor, transfer_id),
            )
            self._append_log(transfer_id, actor, status.value, note)

    # ── record verification result ────────────────────────────────────────────

    def record_verification(
        self,
        transfer_id: int,
        total: int,
        passed: int,
        failed: int,
        actor: str,
    ) -> None:
        """
        Store checksum counts and set status to completed or anomaly.
        """
        status = TransferStatus.COMPLETED if failed == 0 else TransferStatus.ANOMALY
        now    = datetime.now(tz=timezone.utc).isoformat()
        note   = f"Verified: {passed}/{total} OK, {failed} failed."

        with self._tx():
            self._conn.execute(
                """
                UPDATE transfers
                SET checksum_total=?, checksum_passed=?, checksum_failed=?,
                    status=?, last_modified=?, last_modified_by=?
                WHERE id=?
                """,
                (total, passed, failed, status.value, now, actor, transfer_id),
            )
            self._append_log(transfer_id, actor, status.value, note)

    # ── update fields ─────────────────────────────────────────────────────────

    def update_fields(
        self,
        transfer_id: int,
        actor: str,
        **kwargs,
    ) -> None:
        """Update arbitrary editable fields. kwargs = {column: value}."""
        EDITABLE = {
            "ticket_number", "requester", "artifact_name", "artifact_type",
            "size_gb", "source_path", "destination_path", "notes",
        }
        valid = {k: v for k, v in kwargs.items() if k in EDITABLE}
        if not valid:
            return
        now = datetime.now(tz=timezone.utc).isoformat()
        sets = ", ".join(f"{k}=?" for k in valid)
        vals = list(valid.values()) + [now, actor, transfer_id]
        changes = "; ".join(f"{k}: {v}" for k, v in valid.items())
        with self._tx():
            self._conn.execute(
                f"UPDATE transfers SET {sets}, last_modified=?, last_modified_by=? WHERE id=?",
                vals,
            )
            self._append_log(transfer_id, actor, "updated", f"Fields updated: {changes}")

    # ── read ──────────────────────────────────────────────────────────────────

    def list_transfers(
        self,
        status: Optional[str] = None,
        ticket: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?"); params.append(status)
        if ticket:
            clauses.append("ticket_number LIKE ?"); params.append(f"%{ticket}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._conn.execute(
            f"SELECT * FROM transfers {where} ORDER BY created_at DESC",
            params,
        ).fetchall()

    def get_transfer(self, transfer_id: int) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM transfers WHERE id=?", (transfer_id,)
        ).fetchone()

    def get_log(self, transfer_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM transfer_log WHERE transfer_id=? ORDER BY timestamp ASC",
            (transfer_id,),
        ).fetchall()

    # ── internal ──────────────────────────────────────────────────────────────

    def _append_log(
        self, transfer_id: int, actor: str, action: str, note: str
    ) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO transfer_log (transfer_id, actor, action, note, timestamp)
            VALUES (?,?,?,?,?)
            """,
            (transfer_id, actor, action, note, now),
        )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
