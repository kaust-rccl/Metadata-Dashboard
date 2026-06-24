"""
models/modlog.py
----------------
ArtifactModLog — sidecar modification log.

Rules:
- YML is the source of truth. This is a mirror.
- write_best_effort() never raises — a failed write is a sync gap, not a crash.
- hydrate_note() derives the log entry note from the YML's notes fields
  so the log always reflects what was written at the time of action.
- sync_from_yml() rebuilds or patches the log from the YML state,
  called on load to heal any drift.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from config import Action, Role

if TYPE_CHECKING:
    from models.artifact import ArtifactYML

logger = logging.getLogger(__name__)


@dataclass
class ModLogEntry:
    """Single append-only log entry."""
    actor: str
    role: Role
    action: Action
    timestamp: datetime
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "actor": self.actor,
            "role": self.role.value,
            "action": self.action.value,
            "timestamp": self.timestamp.isoformat(),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ModLogEntry:
        return cls(
            actor=d.get("actor", ""),
            role=Role(d.get("role", "readonly")),
            action=Action(d.get("action", "updated")),
            timestamp=datetime.fromisoformat(d["timestamp"]),
            note=d.get("note", ""),
        )


@dataclass
class ArtifactModLog:
    """
    Sidecar modification log for an artifact.

    Lifecycle:
      1. Created alongside the YML when curator first saves (STAGED).
      2. Appended to by curator and mover on every subsequent save.
      3. On load, sync_from_yml() checks for drift and patches if needed.
      4. Written with write_best_effort() — never blocks the main write path.
    """
    artifact_name: str
    artifact_yml: str  # relative or absolute path to the YML file
    entries: list[ModLogEntry] = field(default_factory=list)

    # ── note hydration ────────────────────────────────────────────────────────

    @staticmethod
    def hydrate_note(yml: ArtifactYML, action: Action) -> str:
        """
        Derive the log entry note from the YML's notes fields.

        Hydration rules:
          STAGED / UPDATED by curator → curation_notes
          MOVED by mover              → moving_notes
          BLOCKED / REJECTED          → latest status_reason for the
                                        phase inferred from current status
        When a BLOCKED status advances to STAGED or MOVED, the latest
        status_reason entry is propagated automatically.
        """
        if action in (Action.STAGED, Action.UPDATED):
            return yml.curation_notes

        if action == Action.MOVED:
            note = yml.moving_notes
            # if status was previously BLOCKED, also append the clearing reason
            clearing = yml.latest_status_reason("moving")
            if clearing:
                note = f"{note} | cleared: {clearing}".strip(" |")
            return note

        if action in (Action.BLOCKED, Action.REJECTED):
            # infer phase from which reason list was just appended to
            curation_reason = yml.latest_status_reason("curation")
            moving_reason = yml.latest_status_reason("moving")
            # prefer the one that was most recently updated
            if curation_reason and moving_reason:
                cr = yml.curation_status_reason[-1].timestamp
                mr = yml.moving_status_reason[-1].timestamp
                return curation_reason if cr >= mr else moving_reason
            return curation_reason or moving_reason

        return ""

    # ── append ────────────────────────────────────────────────────────────────

    def append(
            self,
            actor: str,
            role: Role,
            action: Action,
            yml: ArtifactYML,
            note_override: str = "",
    ) -> None:
        note = note_override if note_override else self.hydrate_note(yml, action)
        entry = ModLogEntry(
            actor=actor,
            role=role,
            action=action,
            timestamp=datetime.now(tz=timezone.utc),
            note=note,
        )
        self.entries.append(entry)

    # ── drift detection and recovery ──────────────────────────────────────────

    def sync_from_yml(self, yml: ArtifactYML) -> bool:
        """
        Compare the modlog's latest timestamp against the YML's last_modified.
        If the YML is ahead, append a SYSTEM recovery entry.
        Returns True if a recovery entry was added.
        """
        if not yml.last_modified:
            return False

        latest_log_ts: datetime | None = (
            self.entries[-1].timestamp if self.entries else None
        )

        if latest_log_ts is None or latest_log_ts < yml.last_modified:
            recovery = ModLogEntry(
                actor="SYSTEM",
                role=Role.READONLY,
                action=Action.UPDATED,
                timestamp=yml.last_modified,
                note=(
                    f"recovery: log behind YML — last known status {yml.status.value}"
                ),
            )
            self.entries.append(recovery)
            logger.warning(
                "ModLog for %s was behind YML — recovery entry appended.",
                self.artifact_name,
            )
            return True

        return False

    # ── serialization ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "artifact_name": self.artifact_name,
            "artifact_yml": self.artifact_yml,
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, d: dict) -> ArtifactModLog:
        return cls(
            artifact_name=d.get("artifact_name", ""),
            artifact_yml=d.get("artifact_yml", ""),
            entries=[
                ModLogEntry.from_dict(e) for e in d.get("entries", [])
            ],
        )

    @classmethod
    def new(cls, yml: ArtifactYML, yml_path: Path) -> ArtifactModLog:
        """Create a fresh modlog for a newly staged artifact."""
        return cls(
            artifact_name=yml.artifact_name,
            artifact_yml=str(yml_path),
        )

    # ── best-effort write ─────────────────────────────────────────────────────

    def write_best_effort(self, path: Path, writer_fn) -> None:
        """
        Write the modlog using the provided atomic writer function.
        Silently logs on failure — never raises.
        writer_fn signature: (path: Path, data: dict) -> None
        """
        try:
            writer_fn(path, self.to_dict())
        except Exception as exc:
            logger.warning(
                "ModLog write failed for %s at %s: %s — sync gap recorded.",
                self.artifact_name, path, exc,
            )
