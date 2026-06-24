"""
config.py
---------
ingle source of truth for all constants, enums, and path helpers.
Nothing in this file does I/O — pure data and pure functions.
To add a new enum value, add it here and nowhere else.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path


# ── runtime paths ─────────────────────────────────────────────────────────────
#
# Filesystem layout on sh3:
#
#   PROJECTS_BASE/
#   ├── 612345/                  ← named after aimcr_reference (6####)
#   │   ├── manifest.yml
#   │   ├── datasets/
#   │   │   ├── my_dataset.yml
#   │   │   └── my_dataset.modlog.yml
#   │   ├── models/
#   │   └── software/
#   ├── 612346/
#   │   └── ...
#   └── Metadata_Dashboard/
#       └── tracker.db

STAGING_BASE:  Path = Path("/scratch/project/k03/support_team/")
PROJECTS_BASE: Path = Path("/scratch/project/k03/support_team/")
DB_PATH:       Path = Path(
    "/scratch/project/k03/artifact_db/tracker.db"
)

# Resolved at import time so all modules share the same object
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ── LDAP / system group names ─────────────────────────────────────────────────

# Swap these for the real sh3 group names before deployment
LDAP_GROUP_CURATOR: str = "k03"
LDAP_GROUP_MOVER: str   = "k02"


# ── datetime ──────────────────────────────────────────────────────────────────

DATETIME_FMT: str = "%Y-%m-%dT%H:%M:%S%z"
DATE_FMT: str     = "%Y-%m-%d"


# ── file naming ───────────────────────────────────────────────────────────────

YML_SUFFIX:    str = ".yml"
MODLOG_SUFFIX: str = ".modlog.yml"
MANIFEST_NAME: str = "manifest.yml"


# ── enums ─────────────────────────────────────────────────────────────────────

class Role(str, Enum):
    """App roles resolved from LDAP group membership."""
    CURATOR  = "curator"
    MOVER    = "mover"
    READONLY = "readonly"


class ArtifactType(str, Enum):
    """
    Artifact classification.
    Each value maps to a subdirectory on disk (see ARTIFACT_TYPE_DIR below).
    """
    DATASET  = "dataset"
    MODEL    = "model"
    SOFTWARE = "software"


# Maps each ArtifactType to its on-disk subdirectory name.
# Update here if a directory is ever renamed — nowhere else needs to change.
ARTIFACT_TYPE_DIR: dict[ArtifactType, str] = {
    ArtifactType.DATASET:  "datasets",
    ArtifactType.MODEL:    "models",
    ArtifactType.SOFTWARE: "software",
}


class Partition(str, Enum):
    """HPC partition the artifact currently resides on."""
    CPU = "cpu"
    GPU = "gpu"


class Status(str, Enum):
    """
    Artifact lifecycle status.
    Transitions enforced by the app — never written freehand.
    """
    STAGED   = "staged"
    MOVED    = "moved"
    BLOCKED  = "blocked"
    REJECTED = "rejected"


class Action(str, Enum):
    """
    Mod log action types.
    BLOCKED / REJECTED carry a reason propagated from the relevant
    status_reason field at write time.
    When status advances from BLOCKED → STAGED or MOVED the latest
    status_reason entry is propagated — no explicit CLEARED action.
    """
    STAGED   = "staged"
    MOVED    = "moved"
    BLOCKED  = "blocked"
    REJECTED = "rejected"
    UPDATED  = "updated"


class ChecksumType(str, Enum):
    SHA256 = "sha256"
    MD5    = "md5"


class ChecksumSource(str, Enum):
    SELF_GENERATED = "self-generated"
    FROM_SOURCE    = "from-source"


# ── allowed status transitions ────────────────────────────────────────────────
# Maps (current_status, role) → set of statuses the role may transition to.
# Enforced by the app layer — never bypassed.

ALLOWED_TRANSITIONS: dict[tuple[Status, Role], set[Status]] = {
    (Status.STAGED,   Role.CURATOR): {Status.BLOCKED,  Status.REJECTED},
    (Status.BLOCKED,  Role.CURATOR): {Status.STAGED,   Status.REJECTED},
    (Status.REJECTED, Role.CURATOR): {Status.STAGED},
    (Status.STAGED,   Role.MOVER):   {Status.MOVED,    Status.BLOCKED,  Status.REJECTED},
    (Status.BLOCKED,  Role.MOVER):   {Status.MOVED,    Status.REJECTED},
    (Status.REJECTED, Role.MOVER):   {Status.STAGED},
}


# ── path helpers ──────────────────────────────────────────────────────────────
# All paths are keyed by aimcr_reference (6####) — the on-disk directory name.
# artifact_type determines which subdirectory (datasets/ models/ software/).

def proposal_dir(aimcr_reference: str) -> Path:
    """Absolute path to a proposal directory: PROJECTS_BASE / 6####."""
    return PROJECTS_BASE / aimcr_reference


def manifest_path(aimcr_reference: str) -> Path:
    """Absolute path to a proposal's manifest.yml."""
    return proposal_dir(aimcr_reference) / MANIFEST_NAME


def artifact_dir(aimcr_reference: str, artifact_type: ArtifactType) -> Path:
    """
    Absolute path to the type-bucketed subdirectory:
      PROJECTS_BASE / 6#### / datasets|models|software /
    """
    subdir = ARTIFACT_TYPE_DIR[artifact_type]
    return proposal_dir(aimcr_reference) / subdir


def artifact_yml_path(
    aimcr_reference: str,
    artifact_name: str,
    artifact_type: ArtifactType,
) -> Path:
    """Absolute path to an artifact's YML snapshot file."""
    return artifact_dir(aimcr_reference, artifact_type) / f"{artifact_name}{YML_SUFFIX}"


def modlog_path(
    aimcr_reference: str,
    artifact_name: str,
    artifact_type: ArtifactType,
) -> Path:
    """Absolute path to an artifact's sidecar modlog file."""
    return artifact_dir(aimcr_reference, artifact_type) / f"{artifact_name}{MODLOG_SUFFIX}"


# ── backwards-compat shim ─────────────────────────────────────────────────────
# project_dir kept so any external script importing it still works.
# Internally everything uses proposal_dir.
project_dir = proposal_dir
