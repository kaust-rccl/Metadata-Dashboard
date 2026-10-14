"""
iolib/sync.py
-------------
Two-way reconciliation between artifact YMLs on disk and tracker.db
(HPC-AI pipeline only — the HPC/ad-hoc pipeline is DB-only by design).

    disk → DB   every recognised artifact YML is mirrored into the DB
                (inserted if missing, refreshed if present)
    DB → disk   every DB artifact row with no recognised YML gets one
                written at its canonical path, plus a modlog entry and a
                manifest entry

A record's identity is (aimcr_reference, artifact_type, artifact_name) —
the same triple that determines its canonical path. DB rows and YMLs are
matched on identity, not on the stored yml_path, so a row whose yml_path
drifted (sysadmin-inserted rows, old Import-page rows) is repointed rather
than duplicated.

Safety rules — sync never guesses and never destroys:
  - Files that are not ours are never parsed (see yaml_io.classify_artifact_yml).
  - A recognised YML that sits somewhere other than where its own content
    says it belongs is a conflict, not synced.
  - Nothing on disk is ever overwritten. If the canonical path for a DB row
    is occupied by anything, that row is reported, not written.
  - DB rows with unknown enum values, unsafe names, or an AIMCR reference
    that does not match AIMCR_REFERENCE_PATTERN are reported, not written.
  - Several DB rows sharing one identity is a conflict; none are touched.

Duplicates (several DB rows sharing a yml_path or an identity) are resolved
before anything else: one row keeps the record, every other row is given
its own identity — its own name if that is free, otherwise <name>_dup2,
_dup3, ... — and gets its own YML. Each is listed in SyncReport.duplicates
so it can be reviewed and, if it was a true duplicate, deleted. Once no
duplicate yml_path remains, the missing unique index is added.

Deleted records: a record whose modlog ends in DELETED (DeleteService) is
never recreated. A DB row left behind by an interrupted delete is removed;
a YML left behind is reported.

Everything that needs a human lands in SyncReport.conflicts, .duplicates
or .errors.

Concurrency: a non-blocking flock next to the DB means only one sync runs
at a time. A session that loses the race skips its sync (report.locked).
"""

from __future__ import annotations

import fcntl
import logging
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from config import (
    AIMCR_REFERENCE_PATTERN, ARTIFACT_TYPE_DIR, DUPLICATE_SUFFIX, MODLOG_SUFFIX, PROJECTS_BASE,
    Action, ArtifactType, ChecksumSource, ChecksumType, Partition, Role, Status,
    artifact_yml_path, manifest_path, modlog_path,
)
from iolib.fs import list_staged_artifacts
from iolib.perms import open_lock_file
from iolib.yaml_io import (
    atomic_create, atomic_write, classify_artifact_yml, file_lock,
    load_manifest, load_modlog,
)
from models.artifact import ArtifactYML, ChecksumVerification
from models.manifest import ManifestEntry, ManifestYML
from models.modlog import ArtifactModLog

logger = logging.getLogger(__name__)

Key = tuple[str, str, str]   # (aimcr_reference, artifact_type, artifact_name)

_AIMCR_REF_RE = re.compile(AIMCR_REFERENCE_PATTERN)

# ArtifactYML fields that have no DB column. A YML rebuilt from a DB row
# leaves these blank; the modlog entry says so.
_NOT_IN_DB: tuple[str, ...] = (
    "project_id (KAUST project, if it differs from the AIMCR reference)",
    "metadata_creation_date", "system", "curation_notes", "moving_notes",
    "curation_status_reason", "moving_status_reason",
    "checksum verified_by / verified_date",
)


# ── report ────────────────────────────────────────────────────────────────────

@dataclass
class SyncReport:
    dry_run: bool = False
    locked: bool = False                                   # another sync held the lock
    db_inserted: list[str] = field(default_factory=list)   # YML → new DB row
    db_refreshed: list[str] = field(default_factory=list)  # YML → existing DB row
    db_repointed: list[str] = field(default_factory=list)  # row's yml_path corrected
    yml_created: list[str] = field(default_factory=list)   # DB row → new YML
    duplicates: list[str] = field(default_factory=list)    # duplicate rows split apart
    deletes_completed: list[str] = field(default_factory=list)  # interrupted deletes finished
    ignored: list[str] = field(default_factory=list)       # not ours — informational
    conflicts: list[str] = field(default_factory=list)     # needs a human
    errors: list[str] = field(default_factory=list)        # unexpected failures

    @property
    def needs_attention(self) -> bool:
        return bool(self.conflicts or self.errors or self.duplicates)

    def summary(self) -> str:
        if self.locked:
            return "Sync skipped — another sync is already running."
        prefix = "[dry run] " if self.dry_run else ""
        return (
            f"{prefix}{len(self.yml_created)} YML(s) created from DB, "
            f"{len(self.db_inserted)} DB row(s) added from YML, "
            f"{len(self.db_repointed)} repointed, "
            f"{len(self.db_refreshed)} refreshed, "
            f"{len(self.duplicates)} duplicate(s) split, "
            f"{len(self.conflicts)} conflict(s), {len(self.errors)} error(s), "
            f"{len(self.ignored)} non-metadata file(s) ignored."
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["needs_attention"] = self.needs_attention
        return d


# ── public entry points ───────────────────────────────────────────────────────

def mirror_to_db(db, yml: ArtifactYML, yml_p: Path, ml_p: Path, row_id: Optional[int] = None) -> int:
    """
    Upsert the project row (FK parent) then the artifact row from a YML.
    Raises on failure — callers decide whether that is fatal.

    project_id is forced to aimcr_reference: manifest.yml's project_id is
    hand-editable and may be blank or stale, while aimcr_reference is the
    key used for paths and for the artifact row's FK.
    """
    manifest = load_manifest(manifest_path(yml.aimcr_reference))
    if manifest is None:
        manifest = ManifestYML.new(
            yml.aimcr_reference, yml.proposal_title, yml.pi_username
        )
    manifest.project_id = yml.aimcr_reference
    manifest.absorb_project_fields(yml)        # project profile → projects row
    db.upsert_project(manifest)
    return db.upsert_artifact(yml, yml_p, ml_p, row_id=row_id)


def sync(
    db,
    projects_base: Path = PROJECTS_BASE,
    actor: str = "SYSTEM",
    dry_run: bool = False,
) -> SyncReport:
    """
    Run a full two-way sync. Never raises for per-record problems — they are
    collected in the report. dry_run computes the same report without
    writing anything to disk or the DB.
    """
    report = SyncReport(dry_run=dry_run)

    with _sync_lock(db.path.with_name("sync"), skip=dry_run) as acquired:
        if not acquired:
            report.locked = True
            logger.info("Sync skipped: lock held by another process.")
            return report

        disk, blocked = _scan_disk(projects_base, report)

        try:
            rows = [dict(r) for r in db.list_artifacts()]
        except sqlite3.Error as exc:
            report.errors.append(f"could not read artifacts from DB: {exc}")
            return report

        _dedupe(db, rows, disk, report, dry_run)

        # Add the missing unique index as soon as the duplicates are gone, so
        # the rest of the run (and every later write) uses a sound schema.
        if not dry_run:
            ok, msg = db.ensure_unique_yml_path()
            if not ok and msg:
                report.conflicts.append(f"DB schema — {msg}")

        claimed = _disk_to_db(db, disk, rows, report, dry_run)
        _db_to_disk(db, rows, claimed, blocked, report, actor, dry_run)

    logger.info("Sync finished: %s", report.summary())
    return report


# ── locking ───────────────────────────────────────────────────────────────────

@contextmanager
def _sync_lock(base: Path, skip: bool) -> Iterator[bool]:
    """Non-blocking exclusive lock on <base>.lock. Yields False if held elsewhere."""
    if skip:
        yield True
        return
    lock_path = base.with_suffix(".lock")
    fd = None
    try:
        fd = open_lock_file(lock_path)   # group k03 rwx, shared across users
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if fd:
            fd.close()
        yield False
        return
    except OSError as exc:
        # Cannot create/lock the sentinel (permissions, fs without flock).
        # Proceed unlocked rather than never syncing; atomic_create still
        # guarantees no file is overwritten if two syncs race.
        logger.warning("Sync lock unavailable at %s (%s) — running unlocked.", lock_path, exc)
        if fd:
            fd.close()
        yield True
        return
    try:
        yield True
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


# ── helpers ───────────────────────────────────────────────────────────────────

def _key_of(yml: ArtifactYML) -> Key:
    return (yml.aimcr_reference, yml.artifact_type.value, yml.artifact_name)


def _label(key: Key) -> str:
    return "/".join(key)


def _row_ref(row: dict) -> str:
    """AIMCR reference for a DB row: the aimcr_reference column if set, else project_id."""
    ref = row.get("aimcr_reference")
    if ref is None or str(ref).strip() == "":
        ref = row.get("project_id")
    return "" if ref is None else str(ref).strip()


def _row_key(row: dict) -> Key:
    return (
        _row_ref(row),
        str(row["artifact_type"] or "").strip().lower(),
        str(row["artifact_name"] or ""),
    )


def _placement_problem(key: Key) -> Optional[str]:
    """Why a YML cannot be created for this identity, or None if it can."""
    ref, atype, name = key
    if not _AIMCR_REF_RE.fullmatch(ref):
        return f"AIMCR reference {ref!r} does not match {AIMCR_REFERENCE_PATTERN}"
    try:
        ArtifactType(atype)
    except ValueError:
        return f"unrecognised artifact_type {atype!r}"
    if (
        not name
        or name != name.strip()
        or name in (".", "..")
        or name.startswith(".")
        or any(c in name for c in "/\\\0")
        or name.endswith(MODLOG_SUFFIX.removesuffix(".yml"))
    ):
        return f"artifact_name {name!r} is not usable as a file name"
    return None


def _deleted_marker(key: Key) -> bool:
    """True if the record's modlog sidecar ends in a DELETED entry."""
    ref, atype, name = key
    try:
        ml = load_modlog(modlog_path(ref, name, ArtifactType(atype)))
    except Exception:
        return False
    return bool(ml and ml.entries and ml.entries[-1].action == Action.DELETED)


# ── pass 1: scan disk ─────────────────────────────────────────────────────────

def _scan_disk(
    projects_base: Path, report: SyncReport
) -> tuple[dict[Key, tuple[Path, ArtifactYML]], set[Key]]:
    """
    Classify every candidate file. Returns:
      disk     — identity → (path, record) for records that are ours AND in place
      blocked  — identities with a misplaced or unreadable record on disk;
                 DB → disk must not create a second copy for these
    """
    disk: dict[Key, tuple[Path, ArtifactYML]] = {}
    blocked: set[Key] = set()

    for p in list_staged_artifacts(projects_base):
        data, reason = classify_artifact_yml(p)
        if data is None:
            report.ignored.append(f"{p} — {reason}")
            continue

        try:
            yml = ArtifactYML.from_dict(data)
        except Exception as exc:
            report.conflicts.append(f"{p} — metadata record has invalid values: {exc}")
            continue

        key = _key_of(yml)
        rel = p.relative_to(projects_base)
        expected = (yml.aimcr_reference, ARTIFACT_TYPE_DIR[yml.artifact_type], yml.artifact_name)
        actual = (rel.parts[0], rel.parts[1], p.name.removesuffix(".yml"))
        if expected != actual:
            report.conflicts.append(
                f"{p} — record says {_label(key)} but file sits at {'/'.join(rel.parts)}; "
                "not synced (move or rename the file to match its content)"
            )
            blocked.add(key)
            continue

        if _deleted_marker(key):
            report.conflicts.append(
                f"{p} — this record was deleted (modlog) but its YML is still on "
                "disk; not synced. Remove the file, or re-stage the artifact."
            )
            blocked.add(key)
            continue

        disk[key] = (p, yml)

    return disk, blocked


# ── pass 1b: split duplicate DB rows ──────────────────────────────────────────

def _dedupe(
    db,
    rows: list[dict],
    disk: dict[Key, tuple[Path, ArtifactYML]],
    report: SyncReport,
    dry_run: bool,
) -> None:
    """
    Rows that share a yml_path or an identity are grouped (transitively).
    In each group one row is kept as-is; every other row is moved to its
    own identity and canonical path, so later passes treat it as a record
    of its own (and write it a YML from its DB columns).

    Keeper preference: the row whose identity AND path match a YML on disk,
    then a row whose identity matches a YML on disk, then the lowest rowid.
    A non-keeper keeps its name if no other record holds that identity,
    otherwise gets <name>_dup2, _dup3, ... (first free). Mutates `rows`
    in place so the later passes see the new identities, also in dry runs.
    """
    parent = {r["_rowid"]: r["_rowid"] for r in rows}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    first_by_key: dict[Key, int] = {}
    first_by_path: dict[str, int] = {}
    for r in rows:
        rid = r["_rowid"]
        for index, k in ((first_by_key, _row_key(r)), (first_by_path, str(r["yml_path"] or ""))):
            if k == "":
                continue
            if k in index:
                parent[find(rid)] = find(index[k])
            else:
                index[k] = rid

    groups: dict[int, list[dict]] = {}
    for r in rows:
        groups.setdefault(find(r["_rowid"]), []).append(r)

    # every identity in use, per (ref, type), so suffixes never collide
    used: dict[tuple[str, str], set[str]] = {}
    for k in list(disk.keys()) + [_row_key(r) for r in rows]:
        used.setdefault((k[0], k[1]), set()).add(k[2])

    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda r: r["_rowid"])

        def score(r: dict) -> tuple:
            k = _row_key(r)
            on_disk = k in disk
            path_match = on_disk and str(disk[k][0]) == str(r["yml_path"])
            return (not path_match, not on_disk, r["_rowid"])

        keeper = min(group, key=score)
        taken: set[Key] = {_row_key(keeper)}
        ids = ", ".join(str(r["_rowid"]) for r in group)
        report.duplicates.append(
            f"DB rows {ids} are duplicates ({_label(_row_key(keeper))}); "
            f"row {keeper['_rowid']} keeps the record"
        )

        for r in group:
            if r is keeper:
                continue
            ref, atype, name = _row_key(r)
            if (ref, atype, name) not in taken and _placement_problem((ref, atype, name)) is None:
                new_name = name                      # its own identity is free
            else:
                n = 2
                while True:
                    new_name = f"{name}{DUPLICATE_SUFFIX}{n}"
                    cand = (ref, atype, new_name)
                    if (new_name not in used.get((ref, atype), set())
                            and _placement_problem(cand) is None
                            and not _path_taken(cand)):
                        break
                    n += 1
            new_key = (ref, atype, new_name)
            problem = _placement_problem(new_key)
            if problem:
                report.conflicts.append(
                    f"DB row {r['_rowid']} ({_label((ref, atype, name))}) — duplicate "
                    f"cannot be split: {problem}; left as is"
                )
                continue

            target = artifact_yml_path(ref, new_name, ArtifactType(atype))
            ml = modlog_path(ref, new_name, ArtifactType(atype))
            what = (f"renamed {name} → {new_name}" if new_name != name
                    else f"yml_path corrected → {target}")
            try:
                if not dry_run:
                    db.relocate_artifact(r["_rowid"], new_name, target, ml)
                r["artifact_name"], r["yml_path"] = new_name, str(target)
                taken.add(new_key)
                used.setdefault((ref, atype), set()).add(new_name)
                report.duplicates.append(
                    f"  row {r['_rowid']}: {what} — review it; delete it if it "
                    "was a true duplicate"
                )
            except Exception as exc:
                report.errors.append(f"DB row {r['_rowid']} — could not split duplicate: {exc}")


def _path_taken(key: Key) -> bool:
    ref, atype, name = key
    p = artifact_yml_path(ref, name, ArtifactType(atype))
    return p.exists() or p.is_symlink()


# ── pass 2: disk → DB ─────────────────────────────────────────────────────────

def _disk_to_db(
    db,
    disk: dict[Key, tuple[Path, ArtifactYML]],
    rows: list[dict],
    report: SyncReport,
    dry_run: bool,
) -> set[int]:
    """Mirror each YML into the DB. Returns rowids of DB rows now accounted for."""
    by_key: dict[Key, list[dict]] = {}
    by_path: dict[str, list[dict]] = {}
    for r in rows:
        by_key.setdefault(_row_key(r), []).append(r)
        by_path.setdefault(str(r["yml_path"]), []).append(r)

    claimed: set[int] = set()

    for key, (yp, yml) in sorted(disk.items()):
        label = _label(key)
        matches = by_key.get(key, [])
        at_path = by_path.get(str(yp), [])
        involved = {r["_rowid"]: r for r in matches + at_path}

        if len(involved) > 1:
            report.conflicts.append(
                f"{label} — DB rows {', '.join(map(str, sorted(involved)))} all claim "
                "this record after de-duplication; none touched"
            )
            claimed.update(involved)
            continue

        row = next(iter(involved.values()), None)
        ml = modlog_path(*_path_args(yml))
        try:
            if row is not None and str(row["yml_path"]) != str(yp):
                if not dry_run:
                    db.relocate_artifact(row["_rowid"], yml.artifact_name, yp, ml)
                report.db_repointed.append(f"{label}: {row['yml_path']} → {yp}")

            if not dry_run:
                mirror_to_db(db, yml, yp, ml, row_id=row["_rowid"] if row else None)

            if row is None:
                report.db_inserted.append(label)
            else:
                report.db_refreshed.append(label)
                claimed.add(row["_rowid"])
        except Exception as exc:
            report.errors.append(f"{label} — DB mirror failed: {exc}")
            claimed.update(involved)

    return claimed


def _path_args(yml: ArtifactYML) -> tuple[str, str, ArtifactType]:
    return yml.aimcr_reference, yml.artifact_name, yml.artifact_type


# ── pass 3: DB → disk ─────────────────────────────────────────────────────────

def _db_to_disk(
    db,
    rows: list[dict],
    claimed: set[int],
    blocked: set[Key],
    report: SyncReport,
    actor: str,
    dry_run: bool,
) -> None:
    """Write a YML for every DB row that has none."""
    groups: dict[Key, list[dict]] = {}
    for r in rows:
        if r["_rowid"] not in claimed:
            groups.setdefault(_row_key(r), []).append(r)

    for key, group in sorted(groups.items()):
        label = _label(key)

        if key in blocked:
            continue   # already reported in the disk scan
        if len(group) > 1:
            report.conflicts.append(
                f"{label} — DB rows {', '.join(str(r['_rowid']) for r in group)} still "
                "share this identity; none written"
            )
            continue

        row = group[0]
        problem = _placement_problem(key)
        if problem:
            report.conflicts.append(f"DB row {row['_rowid']} ({label}) — cannot create YML: {problem}")
            continue

        if _deleted_marker(key):
            # DeleteService wrote the marker but the DB delete did not finish.
            if not dry_run:
                try:
                    db.delete_artifact_rows(row_id=row["_rowid"])
                except Exception as exc:
                    report.errors.append(f"DB row {row['_rowid']} ({label}) — could not finish delete: {exc}")
                    continue
            report.deletes_completed.append(f"{label} (DB row {row['_rowid']})")
            continue

        ref, atype, name = key
        target = artifact_yml_path(ref, name, ArtifactType(atype))
        ml = modlog_path(ref, name, ArtifactType(atype))

        if target.exists() or target.is_symlink():
            _, reason = classify_artifact_yml(target)
            report.conflicts.append(
                f"DB row {row['_rowid']} ({label}) — {target} already exists but is not a "
                f"usable record ({reason or 'see disk conflicts above'}); left untouched"
            )
            continue

        yml, origin, err = _recover(db, row, key, target)
        if yml is None:
            report.conflicts.append(f"DB row {row['_rowid']} ({label}) — cannot rebuild record: {err}")
            continue

        if dry_run:
            report.yml_created.append(f"{target} (from {origin})")
            continue

        try:
            _write_recovered(db, row, yml, target, ml, origin, actor, report)
            report.yml_created.append(f"{target} (from {origin})")
        except FileExistsError:
            report.conflicts.append(
                f"DB row {row['_rowid']} ({label}) — {target} appeared during sync; left untouched"
            )
        except Exception as exc:
            report.errors.append(f"DB row {row['_rowid']} ({label}) — YML write failed: {exc}")


def _recover(
    db, row: dict, key: Key, target: Path
) -> tuple[Optional[ArtifactYML], str, str]:
    """
    Build the record for a DB row with no canonical YML.

    Prefers a lossless copy: if the row's stored yml_path points at an
    existing file elsewhere that passes the same ownership gate and has the
    same identity (typical for rows registered by the Import page, which
    used to leave the original file in place), that file's content is used.
    Otherwise the record is rebuilt from the DB row and its project row.

    Returns (yml, origin_description, error).
    """
    stored = str(row["yml_path"] or "")
    if stored and Path(stored) != target and Path(stored).exists():
        data, _ = classify_artifact_yml(Path(stored))
        if data is not None:
            try:
                yml = ArtifactYML.from_dict(data)
                if _key_of(yml) == key:
                    return yml, f"copy of {stored}", ""
            except Exception:
                pass

    try:
        project = db.get_project(row["project_id"])
    except sqlite3.Error:
        project = None
    yml, err = _yml_from_row(row, project, key)
    return yml, "DB row", err


def _yml_from_row(
    row: dict, project: Optional[sqlite3.Row], key: Key
) -> tuple[Optional[ArtifactYML], str]:
    """
    Rebuild an ArtifactYML from DB columns. Empty enum columns fall back to
    the same defaults ArtifactYML uses for a missing key; a non-empty value
    that is not a valid enum member is an error (never guessed).
    """
    ref, atype, name = key

    def col(name_: str):
        return row.get(name_)

    def s(v) -> str:
        return "" if v is None else str(v)

    def enum(cls, v, default):
        v = s(v).strip().lower()
        if not v:
            return default
        return cls(v)   # ValueError propagates

    def dt(v) -> Optional[datetime]:
        if not v:
            return None
        try:
            return datetime.fromisoformat(str(v))
        except ValueError:
            return None

    try:
        status    = enum(Status, col("status"), Status.STAGED)
        partition = enum(Partition, col("partition"), Partition.CPU)
        ctype     = enum(ChecksumType, col("checksum_type"), ChecksumType.SHA256)
        csource   = enum(ChecksumSource, col("checksum_source"), ChecksumSource.SELF_GENERATED)
    except ValueError as exc:
        return None, f"invalid value in DB row: {exc}"

    db_project_id = s(col("project_id")).strip()
    try:
        size = float(col("size_gb") or 0.0)
    except (TypeError, ValueError):
        size = 0.0

    yml = ArtifactYML(
        artifact_name    = name,
        artifact_type    = ArtifactType(atype),
        artifact_version = s(col("artifact_version")),
        proposal_title   = s(project["proposal_title"]) if project else "",
        # projects.project_id holds the AIMCR reference for our own rows; only
        # a differing value (sysadmin rows) is a real KAUST project id.
        project_id       = db_project_id if db_project_id != ref else "",
        pi_username      = s(project["pi_username"]) if project else "",
        aimcr_reference  = ref,
        tracking_ticket  = s(col("tracking_ticket")),
        dm_ticket_number = s(col("dm_ticket_number")),
        curated_by       = s(col("curated_by")),
        date_staged      = dt(col("date_staged")),
        partition        = partition,
        source_path      = s(col("source_path")),
        destination_path = s(col("destination_path")),
        checksum_type    = ctype,
        checksum_source  = csource,
        checksum_filename = s(col("checksum_value")),
        size_gb          = size,
        checksum_verified_cpup = ChecksumVerification(verified=bool(col("checksum_verified_cpup"))),
        reference        = bool(col("reference")),
        reference_group  = s(col("reference_group")),
        moved_by         = s(col("moved_by")),
        date_moved       = dt(col("date_moved")),
        checksum_verified_gpup = ChecksumVerification(verified=bool(col("checksum_verified_gpup"))),
        status           = status,
        last_modified    = dt(col("last_modified")),
        last_modified_by = s(col("last_modified_by")),
    )
    return yml, ""


def _write_recovered(
    db,
    row: dict,
    yml: ArtifactYML,
    target: Path,
    ml: Path,
    origin: str,
    actor: str,
    report: SyncReport,
) -> None:
    """
    Write order mirrors the services: YML (source of truth) first, then the
    best-effort sidecars, then the DB. A failure after the YML is written is
    reported as an error but the YML stays — the next sync picks it up.
    """
    label = _label(_key_of(yml))
    yml.last_modified    = datetime.now(tz=timezone.utc)
    yml.last_modified_by = actor

    atomic_create(target, yml.to_dict())   # FileExistsError if anything is there

    # modlog sidecar — append to an orphaned one if present
    note = f"recreated by sync from {origin}"
    if origin == "DB row":
        note += "; not stored in the DB, left blank: " + ", ".join(_NOT_IN_DB)
    try:
        modlog = load_modlog(ml) or ArtifactModLog.new(yml, target)
        modlog.artifact_yml = str(target)
        modlog.append(actor=actor, role=Role.READONLY, action=Action.UPDATED,
                      yml=yml, note_override=note)
        modlog.write_best_effort(ml, lambda p, d: atomic_write(p, d))
    except Exception as exc:
        report.errors.append(f"{label} — YML written but modlog sidecar not updated: {exc}")

    # manifest entry
    try:
        mp = manifest_path(yml.aimcr_reference)
        with file_lock(mp):
            manifest = load_manifest(mp)
            if manifest is None:
                manifest = ManifestYML.new(
                    yml.aimcr_reference, yml.proposal_title, yml.pi_username
                )
            manifest.absorb_project_fields(yml)
            entry = ManifestEntry.from_artifact_yml(yml, str(target))
            if manifest.find_by_name(yml.artifact_name):
                manifest.update_artifact(yml.artifact_name, **vars(entry))
            else:
                manifest.append_artifact(entry)
            atomic_write(mp, manifest.to_dict())
    except Exception as exc:
        report.errors.append(f"{label} — YML written but manifest not updated: {exc}")

    # DB: point the row at the new file, then refresh it from the YML
    try:
        if str(row["yml_path"]) != str(target):
            db.relocate_artifact(row["_rowid"], yml.artifact_name, target, ml)
            report.db_repointed.append(f"{label}: {row['yml_path']} → {target}")
        mirror_to_db(db, yml, target, ml, row_id=row["_rowid"])
    except Exception as exc:
        report.errors.append(f"{label} — YML written but DB row not updated: {exc}")
