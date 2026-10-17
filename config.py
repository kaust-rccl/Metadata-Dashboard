"""
config.py
---------
Single source of truth for all constants, enums, and path helpers.
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
    "/scratch/project/k03/support_team/mock_db/tracker.db"
)

# Resolved at import time so all modules share the same object
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ── LDAP / system group names ─────────────────────────────────────────────────

# Swap these for the real sh3 group names before deployment
LDAP_GROUP_CURATOR: str = "k03"
LDAP_GROUP_MOVER: str   = "k02"


# ── shared file ownership ─────────────────────────────────────────────────────
# Every YML, modlog, manifest and lock file the app creates is given this
# group with rwx, and every directory it creates gets group rwx + setgid so
# anything created inside later inherits the group (see iolib/perms.py).
FILE_GROUP: str = "k03"


# ── checksum verification jobs (portal-submitted) ─────────────────────────────
# Canonical resumable verify script; copied into each job directory at
# submission so a run keeps the exact script it started with.
VERIFY_SCRIPT: Path = Path("/scratch/project/k03/support_team/checksum-verify/generic_verify.slurm")
VERIFY_DIR_NAME: str = "verify_checksum"   # PROJECTS_BASE/<6####>/verify_checksum/
# umask for sbatch; Slurm applies the submitting umask to the job, so every
# file the job writes is group read-write (group k03 comes from setgid dirs).
VERIFY_UMASK: int = 0o002
# Checksum file names tried first when suggesting one inside a source path.
CHECKSUM_CANDIDATE_NAMES: tuple[str, ...] = (
    "checksum.sha256", "checksums.sha256", "SHA256SUMS", "sha256sums.txt",
    "checksum.md5", "md5sum.txt", "MD5SUMS", "checksum.sha512",
)
CHECKSUM_SUFFIXES: tuple[str, ...] = (".sha256", ".sha512", ".sha1", ".md5")


def verify_job_dir(aimcr_reference: str, artifact_type: "ArtifactType", artifact_name: str) -> Path:
    """PROJECTS_BASE/<6####>/verify_checksum/<datasets|models|software>/<name>/"""
    return (proposal_dir(aimcr_reference) / VERIFY_DIR_NAME
            / ARTIFACT_TYPE_DIR[artifact_type] / artifact_name)


# ── superusers ────────────────────────────────────────────────────────────────
# Usernames listed here resolve to Role.ADMIN regardless of group membership.
# ADMIN bypasses every require_role() guard and holds the union of the
# curator and mover permission sets.
SUPERUSERS: frozenset[str] = frozenset({
    "x_mohameta",
})


# ── datetime ──────────────────────────────────────────────────────────────────

DATETIME_FMT: str = "%Y-%m-%dT%H:%M:%S%z"
DATE_FMT: str     = "%Y-%m-%d"


# ── file naming ───────────────────────────────────────────────────────────────

YML_SUFFIX:    str = ".yml"
MODLOG_SUFFIX: str = ".modlog.yml"
MANIFEST_NAME: str = "manifest.yml"

# ── artifact YML recognition / sync ───────────────────────────────────────────

# Proposal directories under PROJECTS_BASE are named after the AIMCR
# reference (6####). Sync refuses to create a directory for any reference
# that does not match — so a malformed DB row can never mkdir an arbitrary
# name inside the shared support_team tree.
AIMCR_REFERENCE_PATTERN: str = r"6\d{4,}"

# Suffix sync gives to a DB row whose (aimcr_reference, type, name) collides
# with another record: <name>_dup2, <name>_dup3, ...
DUPLICATE_SUFFIX: str = "_dup"

# Our artifact YMLs are a few KB. Anything larger is not read at all.
MAX_ARTIFACT_YML_BYTES: int = 256 * 1024


# ── enums ─────────────────────────────────────────────────────────────────────

class Role(str, Enum):
    """App roles resolved from LDAP group membership (or SUPERUSERS)."""
    ADMIN    = "admin"
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
    PREPARING = "preparing"   # record exists, staging in progress (checksum, ticket)
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
    DELETED  = "deleted"   # record removed via DeleteService; sync honours it
    PREPARED = "prepared"  # record created in PREPARING status


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
    # PREPARING → STAGED only through MarkStagedService (needs a DM ticket)
    (Status.PREPARING, Role.CURATOR): {Status.STAGED, Status.REJECTED},
    (Status.PREPARING, Role.ADMIN):   {Status.STAGED, Status.REJECTED},
    (Status.STAGED,   Role.CURATOR): {Status.BLOCKED,  Status.REJECTED},
    (Status.BLOCKED,  Role.CURATOR): {Status.STAGED,   Status.REJECTED},
    (Status.REJECTED, Role.CURATOR): {Status.STAGED},
    (Status.STAGED,   Role.MOVER):   {Status.MOVED,    Status.BLOCKED,  Status.REJECTED},
    (Status.BLOCKED,  Role.MOVER):   {Status.MOVED,    Status.REJECTED},
    (Status.REJECTED, Role.MOVER):   {Status.STAGED},
    # ADMIN — union of curator and mover
    (Status.STAGED,   Role.ADMIN):   {Status.MOVED,  Status.BLOCKED, Status.REJECTED},
    (Status.BLOCKED,  Role.ADMIN):   {Status.MOVED,  Status.STAGED,  Status.REJECTED},
    (Status.REJECTED, Role.ADMIN):   {Status.STAGED},
    (Status.MOVED,    Role.ADMIN):   {Status.BLOCKED, Status.REJECTED},
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


# ── HPC pipeline (ticket-driven, DB-only) ────────────────────────────────────

TRANSFERS_DB_PATH: Path = Path(
    "/scratch/project/k03/support_team/mock_db/transfers.db"
)
TRANSFERS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)


class TransferStatus(str, Enum):
    """
    Lifecycle status for HPC pipeline (ticket-driven) transfers.
    No YAML backing — DB is the sole source of truth.
    """
    PENDING     = "pending"      # record created, pre-move not yet done
    PREMOVE     = "pre-move"     # checksum generated, awaiting data movement
    IN_PROGRESS = "in-progress"  # data being moved
    VERIFYING   = "verifying"    # post-move checksum running
    COMPLETED   = "completed"    # verified, all files OK
    ANOMALY     = "anomaly"      # verification found failures
    BLOCKED     = "blocked"      # on hold


class TransferType(str, Enum):
    """Artifact type for HPC pipeline transfers."""
    DATASET  = "dataset"
    MODEL    = "model"
    SOFTWARE = "software"
    OTHER    = "other"
