"""
models/artifact.py
------------------
ArtifactYML dataclass — the canonical in-memory representation of an
artifact metadata record.

Rules:
- No I/O here. Reading and writing is handled by io/yaml_io.py.
- No Streamlit imports. UI concerns live in components/.
- Validation returns a list of error strings — empty list means valid.
- Field locking logic lives here so the UI and the write path share it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from config import (
    ArtifactType, ChecksumSource, ChecksumType,
    Partition, Role, Status,
)


# ── value objects ─────────────────────────────────────────────────────────────

@dataclass
class ChecksumVerification:
    """Records who verified the checksum, on which partition, and when."""
    verified: bool = False
    verified_by: str = ""
    verified_date: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "verified": self.verified,
            "verified_by": self.verified_by,
            "verified_date": (
                self.verified_date.isoformat() if self.verified_date else ""
            ),
        }

    @classmethod
    def from_dict(cls, d: dict) -> ChecksumVerification:
        verified_date = None
        if d.get("verified_date"):
            try:
                verified_date = datetime.fromisoformat(d["verified_date"])
            except ValueError:
                pass
        return cls(
            verified=bool(d.get("verified", False)),
            verified_by=d.get("verified_by", ""),
            verified_date=verified_date,
        )


@dataclass
class StatusReasonEntry:
    """
    A single entry in a status_reason list.
    Append-only — never edited after creation.
    When a block is cleared, a new entry is appended with reason='cleared: ...'
    rather than modifying the previous entry.
    """
    actor: str
    timestamp: datetime
    reason: str

    def to_dict(self) -> dict:
        return {
            "actor": self.actor,
            "timestamp": self.timestamp.isoformat(),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> StatusReasonEntry:
        return cls(
            actor=d.get("actor", ""),
            timestamp=datetime.fromisoformat(d["timestamp"]),
            reason=d.get("reason", ""),
        )


# ── main dataclass ────────────────────────────────────────────────────────────

@dataclass
class ArtifactYML:
    """
    Full artifact metadata record.

    Field groups:
      identity   — set at creation, never change
      curation   — set by curator, locked after first save
                   exception: curation_notes always editable by curator
      moving     — set by mover, locked after move confirmed
      status     — updated by app on every lifecycle transition
    """

    # ── identity ──────────────────────────────────────────────────────────────
    artifact_name: str = ""
    artifact_type: ArtifactType = ArtifactType.DATASET
    artifact_version: str = ""
    proposal_title: str = ""
    project_id: str = ""
    pi_username: str = ""
    aimcr_reference: str = ""
    tracking_ticket: str = ""
    metadata_creation_date: Optional[datetime] = None

    # ── curation ──────────────────────────────────────────────────────────────
    dm_ticket_number: str = ""
    curated_by: str = ""
    date_staged: Optional[datetime] = None
    system: str = "shaheen3"
    partition: Partition = Partition.CPU
    source_path: str = ""
    destination_path: str = ""
    checksum_type: ChecksumType = ChecksumType.SHA256
    checksum_source: ChecksumSource = ChecksumSource.SELF_GENERATED
    checksum_filename: str = ""
    size_gb: float = 0.0
    checksum_verified_cpup: ChecksumVerification = field(
        default_factory=ChecksumVerification
    )
    reference: bool = False
    reference_group: str = ""
    curation_notes: str = ""
    curation_status_reason: list[StatusReasonEntry] = field(default_factory=list)

    # ── moving ────────────────────────────────────────────────────────────────
    moved_by: str = ""
    date_moved: Optional[datetime] = None
    checksum_verified_gpup: ChecksumVerification = field(
        default_factory=ChecksumVerification
    )
    moving_notes: str = ""
    moving_status_reason: list[StatusReasonEntry] = field(default_factory=list)

    # ── status ────────────────────────────────────────────────────────────────
    status: Status = Status.STAGED
    last_modified: Optional[datetime] = None
    last_modified_by: str = ""

    # ── locking ───────────────────────────────────────────────────────────────

    def is_curator_locked(self) -> bool:
        """
        Curation fields are locked once status has moved past the initial
        staged write. curation_notes is always editable and is exempt.
        """
        return False

    def is_mover_locked(self) -> bool:
        """Moving fields are locked once the artifact has been moved."""
        return False

    def editable_by(self, role: Role) -> set[str]:
        """
        Returns the set of field names this role may currently edit.
        Used by the UI layer to decide widget vs plain text rendering.
        """
        if role == Role.CURATOR:
            base = {"curation_notes", "curation_status_reason"}
            if not self.is_curator_locked():
                base |= _CURATOR_FIELDS
            return base
        if role == Role.MOVER:
            if not self.is_mover_locked():
                return _MOVER_FIELDS
            return set()
        return set()

    # ── status reason helpers ─────────────────────────────────────────────────

    def append_status_reason(
        self, phase: str, actor: str, reason: str
    ) -> None:
        """
        Append a new StatusReasonEntry to the correct phase list.
        phase must be 'curation' or 'moving'.
        """
        entry = StatusReasonEntry(
            actor=actor,
            timestamp=datetime.now(tz=timezone.utc),
            reason=reason,
        )
        if phase == "curation":
            self.curation_status_reason.append(entry)
        elif phase == "moving":
            self.moving_status_reason.append(entry)
        else:
            raise ValueError(f"Unknown phase: {phase!r}. Must be 'curation' or 'moving'.")

    def latest_status_reason(self, phase: str) -> str:
        """Return the most recent reason string for a phase, or empty string."""
        lst = (
            self.curation_status_reason
            if phase == "curation"
            else self.moving_status_reason
        )
        return lst[-1].reason if lst else ""

    # ── validation ────────────────────────────────────────────────────────────

    def validate_curation_fields(self) -> list[str]:
        """
        Validate all required curation fields before first save.
        Returns a list of human-readable error strings.
        Empty list means all fields are valid.
        """
        errors: list[str] = []

        if not self.artifact_name.strip():
            errors.append("Artifact name is required.")
        if not self.project_id.strip():
            errors.append("Project ID is required.")
        if not self.pi_username.strip():
            errors.append("PI username is required.")
        if not self.aimcr_reference.strip():
            errors.append("AIMCR reference is required.")
        if not self.tracking_ticket.strip():
            errors.append("Tracking ticket is required.")
        if not self.dm_ticket_number.strip():
            errors.append("DM ticket number is required (gate field).")
        if not self.source_path.strip():
            errors.append("Source path is required.")
        if not self.destination_path.strip():
            errors.append("Destination path is required.")
        if not self.checksum_filename.strip():
            errors.append("Checksum filename is required.")
        if self.size_gb <= 0:
            errors.append("Size must be greater than 0 GB.")

        return errors

    def validate_moving_fields(self) -> list[str]:
        """Validate mover fields before move confirmation."""
        errors: list[str] = []

        if not self.moved_by.strip():
            errors.append("Mover username is required.")
        if not self.checksum_verified_gpup.verified:
            errors.append("Checksum must be verified on gpup before confirming move.")
        if not self.checksum_verified_gpup.verified_by.strip():
            errors.append("Checksum verified_by is required on gpup.")

        return errors

    # ── serialization ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """
        Serialize to a plain dict ready for ruamel.yaml.
        Section comments are added by io/yaml_io.py — not here.
        """
        return {
            # identity
            "artifact_name": self.artifact_name,
            "artifact_type": self.artifact_type.value,
            "artifact_version": self.artifact_version,
            "proposal_title": self.proposal_title,
            "project_id": self.project_id,
            "pi_username": self.pi_username,
            "aimcr_reference": self.aimcr_reference,
            "tracking_ticket": self.tracking_ticket,
            "metadata_creation_date": (
                self.metadata_creation_date.isoformat()
                if self.metadata_creation_date else ""
            ),
            # curation
            "dm_ticket_number": self.dm_ticket_number,
            "curated_by": self.curated_by,
            "date_staged": (
                self.date_staged.isoformat() if self.date_staged else ""
            ),
            "system": self.system,
            "partition": self.partition.value,
            "source_path": self.source_path,
            "destination_path": self.destination_path,
            "checksum_type": self.checksum_type.value,
            "checksum_source": self.checksum_source.value,
            "checksum_filename": self.checksum_filename,
            "size_gb": self.size_gb,
            "checksum_verified_cpup": self.checksum_verified_cpup.to_dict(),
            "reference": self.reference,
            "reference_group": self.reference_group,
            "curation_notes": self.curation_notes,
            "curation_status_reason": [
                e.to_dict() for e in self.curation_status_reason
            ],
            # moving
            "moved_by": self.moved_by,
            "date_moved": (
                self.date_moved.isoformat() if self.date_moved else ""
            ),
            "checksum_verified_gpup": self.checksum_verified_gpup.to_dict(),
            "moving_notes": self.moving_notes,
            "moving_status_reason": [
                e.to_dict() for e in self.moving_status_reason
            ],
            # status
            "status": self.status.value,
            "last_modified": (
                self.last_modified.isoformat() if self.last_modified else ""
            ),
            "last_modified_by": self.last_modified_by,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ArtifactYML:
        """Deserialize from a dict loaded by ruamel.yaml."""

        def _dt(val: str) -> Optional[datetime]:
            if not val:
                return None
            try:
                return datetime.fromisoformat(val)
            except (ValueError, TypeError):
                return None

        return cls(
            artifact_name=d.get("artifact_name", ""),
            artifact_type=ArtifactType(d.get("artifact_type", "dataset")),
            artifact_version=d.get("artifact_version", ""),
            proposal_title=d.get("proposal_title", ""),
            project_id=d.get("project_id", ""),
            pi_username=d.get("pi_username", ""),
            aimcr_reference=d.get("aimcr_reference", ""),
            tracking_ticket=d.get("tracking_ticket", ""),
            metadata_creation_date=_dt(d.get("metadata_creation_date", "")),
            dm_ticket_number=d.get("dm_ticket_number", ""),
            curated_by=d.get("curated_by", ""),
            date_staged=_dt(d.get("date_staged", "")),
            system=d.get("system", "shaheen3"),
            partition=Partition(d.get("partition", "cpu")),
            source_path=d.get("source_path", ""),
            destination_path=d.get("destination_path", ""),
            checksum_type=ChecksumType(d.get("checksum_type", "sha256")),
            checksum_source=ChecksumSource(
                d.get("checksum_source", "self-generated")
            ),
            checksum_filename=d.get("checksum_filename", ""),
            size_gb=float(d.get("size_gb", 0.0)),
            checksum_verified_cpup=ChecksumVerification.from_dict(
                d.get("checksum_verified_cpup", {})
            ),
            reference=bool(d.get("reference", False)),
            reference_group=d.get("reference_group", ""),
            curation_notes=d.get("curation_notes", ""),
            curation_status_reason=[
                StatusReasonEntry.from_dict(e)
                for e in d.get("curation_status_reason", [])
            ],
            moved_by=d.get("moved_by", ""),
            date_moved=_dt(d.get("date_moved", "")),
            checksum_verified_gpup=ChecksumVerification.from_dict(
                d.get("checksum_verified_gpup", {})
            ),
            moving_notes=d.get("moving_notes", ""),
            moving_status_reason=[
                StatusReasonEntry.from_dict(e)
                for e in d.get("moving_status_reason", [])
            ],
            status=Status(d.get("status", "staged")),
            last_modified=_dt(d.get("last_modified", "")),
            last_modified_by=d.get("last_modified_by", ""),
        )


# ── field sets used by editable_by() ─────────────────────────────────────────

_CURATOR_FIELDS: frozenset[str] = frozenset({
    "artifact_name", "artifact_type", "artifact_version",
    "proposal_title", "project_id", "pi_username",
    "aimcr_reference", "tracking_ticket",
    "dm_ticket_number", "system", "partition",
    "source_path", "destination_path",
    "checksum_type", "checksum_source", "checksum_filename",
    "size_gb", "checksum_verified_cpup",
    "reference", "reference_group",
    "curation_notes",
})

_MOVER_FIELDS: frozenset[str] = frozenset({
    "moved_by", "date_moved", "partition",
    "checksum_verified_gpup",
    "moving_notes",
})
