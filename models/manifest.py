"""
models/manifest.py
------------------
ManifestYML — per-project index of all artifacts.

Rules:
- One manifest per project, lives at k#####/manifest.yml.
- Entries are bucketed by artifact type (datasets / models / software).
- Stager appends a new entry (status: staged).
- Mover updates the existing entry in place (status: moved).
- All writes go through io/yaml_io.py with a file lock.
- status_reason fields here mirror only the latest entry from the
  artifact YML — full history stays in the artifact YML.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from config import ArtifactType, Status


@dataclass
class ManifestEntry:
    """
    Lightweight artifact summary stored in the manifest.
    Contains only what is needed for dashboard listing and filtering.
    Full detail is always in the artifact YML (yml_path).
    """
    artifact_name: str
    artifact_type: ArtifactType
    artifact_version: str
    status: Status
    yml_path: str
    curation_status_reason: str = ""   # latest entry only — summary
    moving_status_reason: str = ""     # latest entry only — summary
    last_modified: Optional[datetime] = None
    last_modified_by: str = ""

    def to_dict(self) -> dict:
        return {
            "artifact_name": self.artifact_name,
            "artifact_type": self.artifact_type.value,
            "artifact_version": self.artifact_version,
            "status": self.status.value,
            "yml_path": self.yml_path,
            "curation_status_reason": self.curation_status_reason,
            "moving_status_reason": self.moving_status_reason,
            "last_modified": (
                self.last_modified.isoformat() if self.last_modified else ""
            ),
            "last_modified_by": self.last_modified_by,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ManifestEntry:
        last_modified = None
        if d.get("last_modified"):
            try:
                last_modified = datetime.fromisoformat(d["last_modified"])
            except ValueError:
                pass
        return cls(
            artifact_name=d.get("artifact_name", ""),
            artifact_type=ArtifactType(d.get("artifact_type", "dataset")),
            artifact_version=d.get("artifact_version", ""),
            status=Status(d.get("status", "staged")),
            yml_path=d.get("yml_path", ""),
            curation_status_reason=d.get("curation_status_reason", ""),
            moving_status_reason=d.get("moving_status_reason", ""),
            last_modified=last_modified,
            last_modified_by=d.get("last_modified_by", ""),
        )

    @classmethod
    def from_artifact_yml(
        cls, yml, yml_path: str
    ) -> ManifestEntry:
        """
        Build a ManifestEntry from an ArtifactYML instance.
        Mirrors only the summary fields — full detail stays in the YML.
        """
        return cls(
            artifact_name=yml.artifact_name,
            artifact_type=yml.artifact_type,
            artifact_version=yml.artifact_version,
            status=yml.status,
            yml_path=yml_path,
            curation_status_reason=(
                yml.latest_status_reason("curation")
            ),
            moving_status_reason=(
                yml.latest_status_reason("moving")
            ),
            last_modified=yml.last_modified,
            last_modified_by=yml.last_modified_by,
        )


@dataclass
class ManifestYML:
    """
    Project-level manifest — index of all artifacts under a project.
    Bucketed by artifact type for quick visual scanning.
    """
    project_id: str
    proposal_title: str = ""
    pi_username: str = ""
    manifest_created: Optional[datetime] = None
    last_modified: Optional[datetime] = None
    last_modified_by: str = ""

    datasets: list[ManifestEntry] = field(default_factory=list)
    models: list[ManifestEntry] = field(default_factory=list)
    software: list[ManifestEntry] = field(default_factory=list)

    # ── bucket helpers ────────────────────────────────────────────────────────

    def _bucket(self, artifact_type: ArtifactType) -> list[ManifestEntry]:
        """Return the correct bucket list for an artifact type."""
        return {
            ArtifactType.DATASET:  self.datasets,
            ArtifactType.MODEL:    self.models,
            ArtifactType.SOFTWARE: self.software,
        }[artifact_type]

    # ── entry operations ──────────────────────────────────────────────────────

    def find_by_name(self, name: str) -> Optional[ManifestEntry]:
        """Search all buckets for an entry by artifact_name."""
        for bucket in (self.datasets, self.models, self.software):
            for entry in bucket:
                if entry.artifact_name == name:
                    return entry
        return None

    def append_artifact(self, entry: ManifestEntry) -> None:
        """
        Append a new entry to the correct type bucket.
        Called by curator on first save (status: staged).
        Raises ValueError if an entry with the same name already exists.
        """
        if self.find_by_name(entry.artifact_name):
            raise ValueError(
                f"Artifact '{entry.artifact_name}' already exists in manifest."
            )
        self._bucket(entry.artifact_type).append(entry)
        self._touch(entry.last_modified_by)

    def update_artifact(self, name: str, **kwargs) -> None:
        """
        Update fields on an existing entry in place.
        Called by mover on move confirmation.
        Raises KeyError if the artifact is not found.
        kwargs keys must match ManifestEntry field names.
        """
        entry = self.find_by_name(name)
        if entry is None:
            raise KeyError(f"Artifact '{name}' not found in manifest.")
        for key, value in kwargs.items():
            if hasattr(entry, key):
                setattr(entry, key, value)
            else:
                raise AttributeError(
                    f"ManifestEntry has no field '{key}'."
                )
        actor = kwargs.get("last_modified_by", self.last_modified_by)
        self._touch(actor)

    def _touch(self, actor: str) -> None:
        """Update manifest-level last_modified on any write."""
        self.last_modified = datetime.now(tz=timezone.utc)
        self.last_modified_by = actor

    # ── all entries flat ─────────────────────────────────────────────────────

    def all_entries(self) -> list[ManifestEntry]:
        """Return all entries across all buckets — useful for filtering."""
        return self.datasets + self.models + self.software

    def entries_by_status(self, status: Status) -> list[ManifestEntry]:
        """Filter all entries by status."""
        return [e for e in self.all_entries() if e.status == status]

    # ── serialization ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "proposal_title": self.proposal_title,
            "pi_username": self.pi_username,
            "manifest_created": (
                self.manifest_created.isoformat()
                if self.manifest_created else ""
            ),
            "last_modified": (
                self.last_modified.isoformat() if self.last_modified else ""
            ),
            "last_modified_by": self.last_modified_by,
            "datasets": [e.to_dict() for e in self.datasets],
            "models": [e.to_dict() for e in self.models],
            "software": [e.to_dict() for e in self.software],
        }

    @classmethod
    def from_dict(cls, d: dict) -> ManifestYML:
        def _dt(val: str) -> Optional[datetime]:
            if not val:
                return None
            try:
                return datetime.fromisoformat(val)
            except (ValueError, TypeError):
                return None

        return cls(
            project_id=d.get("project_id", ""),
            proposal_title=d.get("proposal_title", ""),
            pi_username=d.get("pi_username", ""),
            manifest_created=_dt(d.get("manifest_created", "")),
            last_modified=_dt(d.get("last_modified", "")),
            last_modified_by=d.get("last_modified_by", ""),
            datasets=[
                ManifestEntry.from_dict(e) for e in d.get("datasets", [])
            ],
            models=[
                ManifestEntry.from_dict(e) for e in d.get("models", [])
            ],
            software=[
                ManifestEntry.from_dict(e) for e in d.get("software", [])
            ],
        )

    @classmethod
    def new(cls, project_id: str, proposal_title: str, pi_username: str) -> ManifestYML:
        """Create a fresh manifest for a new project."""
        return cls(
            project_id=project_id,
            proposal_title=proposal_title,
            pi_username=pi_username,
            manifest_created=datetime.now(tz=timezone.utc),
        )
