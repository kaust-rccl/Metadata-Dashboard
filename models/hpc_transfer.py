"""
models/hpc_transfer.py
----------------------
Data model for HPC pipeline (ticket-driven) transfers.

Mirrors the transfers DB row as a dataclass.
Also holds UI helpers (badge, status colors) that depend on the model
so they travel with it rather than being scattered across pages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ── status colours ────────────────────────────────────────────────────────────

STATUS_COLORS: dict[str, tuple[str, str]] = {
    "pending":     ("#7a82a0", "rgba(122,130,160,0.1)"),
    "pre-move":    ("#6c8fff", "rgba(108,143,255,0.12)"),
    "in-progress": ("#e89440", "rgba(232,148,64,0.12)"),
    "verifying":   ("#6c8fff", "rgba(108,143,255,0.12)"),
    "completed":   ("#1dbf8a", "rgba(29,191,138,0.12)"),
    "anomaly":     ("#d95555", "rgba(217,85,85,0.1)"),
    "blocked":     ("#e89440", "rgba(232,148,64,0.12)"),
}


def status_badge(status: str) -> str:
    """Return an HTML badge span for the given status string."""
    fg, bg = STATUS_COLORS.get(status, ("#7a82a0", "rgba(122,130,160,0.1)"))
    return (
        f"<span style='background:{bg};color:{fg};"
        f"padding:2px 9px;border-radius:3px;"
        f"font-size:0.78em;font-weight:600;font-family:monospace;"
        f"white-space:nowrap;display:inline-block;margin-top:2px;"
        f"letter-spacing:0.06em'>{status.upper()}</span>"
    )


# ── dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class HpcTransfer:
    """
    Mirrors a single row from the transfers table.
    Constructed from a sqlite3.Row via from_row().
    """
    id:                 int
    ticket_number:      str
    requester:          str
    artifact_name:      str
    artifact_type:      str
    size_gb:            Optional[float]
    source_path:        str
    destination_path:   str
    notes:              str
    status:             str
    operator:           str
    checksum_total:     Optional[int]
    checksum_passed:    Optional[int]
    checksum_failed:    Optional[int]
    created_at:         str
    last_modified:      str
    last_modified_by:   str

    @classmethod
    def from_row(cls, row) -> HpcTransfer:
        return cls(
            id                = row["id"],
            ticket_number     = row["ticket_number"] or "",
            requester         = row["requester"]     or "",
            artifact_name     = row["artifact_name"] or "",
            artifact_type     = row["artifact_type"] or "",
            size_gb           = row["size_gb"],
            source_path       = row["source_path"]       or "",
            destination_path  = row["destination_path"]  or "",
            notes             = row["notes"]             or "",
            status            = row["status"]            or "pending",
            operator          = row["operator"]          or "",
            checksum_total    = row["checksum_total"],
            checksum_passed   = row["checksum_passed"],
            checksum_failed   = row["checksum_failed"],
            created_at        = row["created_at"]        or "",
            last_modified     = row["last_modified"]     or "",
            last_modified_by  = row["last_modified_by"]  or "",
        )

    def to_yml_dict(self) -> dict:
        """
        Build the artifact.yml dict for this transfer.
        Written to workspace/artifact.yml at every stage.
        """
        from datetime import datetime, timezone
        return {
            "artifact_name":         self.artifact_name,
            "artifact_type":         self.artifact_type,
            "ticket_number":         self.ticket_number,
            "requester":             self.requester,
            "operator":              self.operator,
            "source_path":           self.source_path,
            "destination_path":      self.destination_path,
            "size_gb":               self.size_gb or 0.0,
            "notes":                 self.notes,
            "checksum_filename":     "checksum.sha256",
            "checksum_verified_src": False,
            "status":                self.status,
            "last_modified":         datetime.now(tz=timezone.utc).isoformat(),
        }