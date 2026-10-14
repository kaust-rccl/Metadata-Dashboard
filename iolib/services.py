"""
iolib/services.py
-----------------
Service layer — one class per lifecycle action.

Rules:
- Pages import services. Pages never call yaml_io or database directly.
- Each service class owns exactly one action's write sequence:
    1. validate
    2. mutate the dataclass
    3. atomic_write(yml)
    4. upsert DB
    5. modlog.append() + write_best_effort()
    6. update manifest (with file lock)
- Adding a new action = adding a new service class here.
- Services return a ServiceResult so the UI can render success/error
  without knowing anything about the write path.

Path resolution:
  All paths are keyed by aimcr_reference + artifact_type.
  yml.aimcr_reference is the on-disk directory name (6####).
  yml.artifact_type determines the subdirectory (datasets/models/software).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from config import (
    Action, Partition, Role, Status,
    artifact_yml_path, manifest_path, modlog_path,
    ALLOWED_TRANSITIONS,
)
from models.artifact import ArtifactYML
from models.manifest import ManifestEntry, ManifestYML
from models.modlog import ArtifactModLog
from iolib.yaml_io import (
    atomic_write, file_lock,
    load_artifact_yml, load_manifest, load_modlog,
    save_artifact_yml,
)

logger = logging.getLogger(__name__)


# ── result type ───────────────────────────────────────────────────────────────

@dataclass
class ServiceResult:
    success: bool
    message: str
    errors: list[str] = field(default_factory=list)

    @classmethod
    def ok(cls, message: str = "Success") -> ServiceResult:
        return cls(success=True, message=message)

    @classmethod
    def fail(cls, errors: list[str]) -> ServiceResult:
        return cls(success=False, message=errors[0], errors=errors)


# ── base ──────────────────────────────────────────────────────────────────────

class _BaseService:
    """Shared helpers for all service classes."""

    def _paths(self, yml: ArtifactYML) -> tuple[Path, Path]:
        """Return (yml_path, modlog_path) for this artifact."""
        yp = artifact_yml_path(yml.aimcr_reference, yml.artifact_name, yml.artifact_type)
        ml = modlog_path(yml.aimcr_reference, yml.artifact_name, yml.artifact_type)
        return yp, ml

    def _touch(self, yml: ArtifactYML, actor: str) -> None:
        yml.last_modified    = datetime.now(tz=timezone.utc)
        yml.last_modified_by = actor

    def _write_modlog(
        self, yml: ArtifactYML, actor: str, role: Role,
        action: Action, ml_path: Path, note: str = "",
    ) -> None:
        modlog = load_modlog(ml_path) or ArtifactModLog.new(yml, ml_path)
        modlog.append(actor=actor, role=role, action=action, yml=yml, note_override=note)
        modlog.write_best_effort(ml_path, lambda p, d: atomic_write(p, d))

    def _diff_note(self, before, after, fields: set) -> str:
        if before is None:
            return "no previous state to diff"
        changes = []
        for f in sorted(fields):
            v_before = getattr(before, f, None)
            v_after  = getattr(after, f, None)
            if hasattr(v_before, "value"):
                v_before = v_before.value
            if hasattr(v_after, "value"):
                v_after = v_after.value
            if str(v_before) != str(v_after):
                changes.append(f"{f}: '{v_before}' → '{v_after}'")
        return "; ".join(changes) if changes else "no field changes detected"

    def _sync_db(self, db, yml: ArtifactYML, yml_p: Path, ml_p: Path) -> str | None:
        """
        Best-effort DB upsert.
        Upserts the proposal (project) row first to satisfy the FK constraint.

        Returns None on success, or the error string on failure. The YML on
        disk remains the source of truth either way — callers surface the
        returned string as a non-fatal warning rather than silently dropping it.
        """
        try:
            from iolib.sync import mirror_to_db
            mirror_to_db(db, yml, yml_p, ml_p)
            return None
        except Exception as exc:
            logger.warning("DB sync failed for %s: %s", yml.artifact_name, exc)
            return str(exc)

    def _ok(self, message: str, db_warning: str | None = None) -> ServiceResult:
        """
        Build a success ServiceResult. If the DB sync failed, append a visible
        (non-fatal) warning so the curator/mover knows the dashboard listing
        is temporarily out of sync, instead of finding out by confusion.
        """
        if db_warning:
            message += (
                " Saved correctly to disk, but the dashboard listing may be "
                f"stale (DB sync failed: {db_warning}). It will be picked up "
                "by the next sync (new session or `dashboard sync`); contact "
                "an admin if this persists."
            )
        return ServiceResult.ok(message)

    def _update_manifest(
        self, yml: ArtifactYML, yml_p: Path, append: bool
    ) -> None:
        """
        Load manifest, add or update the artifact entry, absorb the record's
        project fields into the project profile, write atomically.
        `append` is kept for callers; a missing entry is always added and an
        existing one always updated, so the manifest self-heals.
        """
        mp = manifest_path(yml.aimcr_reference)

        with file_lock(mp):
            manifest = load_manifest(mp)
            if manifest is None:
                manifest = ManifestYML.new(
                    yml.aimcr_reference, yml.proposal_title, yml.pi_username
                )
            manifest.absorb_project_fields(yml)
            entry = ManifestEntry.from_artifact_yml(yml, str(yml_p))
            if manifest.find_by_name(yml.artifact_name):
                manifest.update_artifact(yml.artifact_name, **vars(entry))
            else:
                manifest.append_artifact(entry)
            atomic_write(mp, manifest.to_dict())

    def _remove_from_manifest(self, aimcr_reference: str, name: str, actor: str) -> None:
        mp = manifest_path(aimcr_reference)
        if not mp.exists():
            return
        with file_lock(mp):
            manifest = load_manifest(mp)
            if manifest is not None and manifest.remove_artifact(name, actor):
                atomic_write(mp, manifest.to_dict())


# ── CurationService ───────────────────────────────────────────────────────────

class CurationService(_BaseService):
    """
    Handles first save of a new artifact by the curator.
    Creates the type-bucketed YML, modlog, and appends to the manifest.
    """

    def execute(self, yml: ArtifactYML, actor: str, db) -> ServiceResult:
        errors = yml.validate_curation_fields()
        if errors:
            return ServiceResult.fail(errors)

        now = datetime.now(tz=timezone.utc)
        yml.curated_by             = actor
        yml.date_staged            = now
        yml.metadata_creation_date = now
        yml.status                 = Status.STAGED
        self._touch(yml, actor)

        yml_p, ml_p = self._paths(yml)

        try:
            save_artifact_yml(yml_p, yml)
        except OSError as exc:
            return ServiceResult.fail([f"Failed to write YML: {exc}"])

        db_warning = self._sync_db(db, yml, yml_p, ml_p)
        self._write_modlog(yml, actor, Role.CURATOR, Action.STAGED, ml_p)
        self._update_manifest(yml, yml_p, append=True)

        return self._ok(
            f"Artifact '{yml.artifact_name}' staged successfully.", db_warning
        )


# ── CurationUpdateService ─────────────────────────────────────────────────────

class CurationUpdateService(_BaseService):
    """
    Handles curator edits to any curation field after first save.
    Computes a diff against the current on-disk YML and logs what changed.

    If the edit changes the record's identity (artifact_name, artifact_type
    or aimcr_reference) the record is relocated to its new canonical path:
    new YML written, old YML removed, modlog sidecar moved, DB row repointed,
    manifest entry moved. Previously the new content was written into the
    old file and the manifest update raised, leaving a misplaced record.
    """

    _IDENTITY = ("artifact_name", "artifact_type", "aimcr_reference")

    def execute(
        self,
        yml: ArtifactYML,
        actor: str,
        updated_fields: dict,
        db,
    ) -> ServiceResult:

        old_p, old_ml = self._paths(yml)
        before = load_artifact_yml(old_p)
        old_ident = tuple(getattr(yml, f) for f in self._IDENTITY)

        for field, value in updated_fields.items():
            if not hasattr(yml, field):
                return ServiceResult.fail([f"Unknown field: '{field}'"])
            if isinstance(value, str):
                value = value.strip()
            setattr(yml, field, value)

        self._touch(yml, actor)
        note = self._diff_note(before, yml, set(updated_fields.keys()))

        if tuple(getattr(yml, f) for f in self._IDENTITY) != old_ident:
            return self._relocate(yml, old_ident, old_p, old_ml, actor, note, db)

        try:
            save_artifact_yml(old_p, yml)
        except OSError as exc:
            return ServiceResult.fail([f"Failed to write YML: {exc}"])

        db_warning = self._sync_db(db, yml, old_p, old_ml)
        self._write_modlog(yml, actor, Role.CURATOR, Action.UPDATED, old_ml, note=note)
        self._update_manifest(yml, old_p, append=False)

        return self._ok(f"Artifact updated. Changes: {note}", db_warning)

    def _relocate(self, yml, old_ident, old_p, old_ml, actor, note, db) -> ServiceResult:
        import os
        from iolib.sync import _placement_problem
        from iolib.yaml_io import atomic_create

        problem = _placement_problem((yml.aimcr_reference, yml.artifact_type.value, yml.artifact_name))
        if problem:
            return ServiceResult.fail([f"Cannot rename: {problem}."])
        new_p, new_ml = self._paths(yml)
        if new_p.exists() or new_p.is_symlink():
            return ServiceResult.fail([
                f"Cannot rename: {new_p} already exists. Pick another name, or "
                "delete the other record first."
            ])

        try:
            atomic_create(new_p, yml.to_dict())
        except (OSError, FileExistsError) as exc:
            return ServiceResult.fail([f"Failed to write YML at {new_p}: {exc}"])

        old_label = f"{old_ident[2]}/{getattr(old_ident[1], 'value', old_ident[1])}/{old_ident[0]}"
        rename_note = f"relocated from {old_label}; {note}"

        # DB first, while the old YML still exists: if this fails the rename
        # is rolled back. (Removing the old YML first would let the next sync
        # rebuild the old record from its orphaned DB row.)
        try:
            rows = db._conn.execute(
                "SELECT rowid AS _rowid FROM artifacts WHERE yml_path = ?", (str(old_p),)
            ).fetchall()
            from iolib.sync import mirror_to_db
            if len(rows) > 1:
                raise RuntimeError(
                    f"{len(rows)} DB rows share {old_p}; run sync to split them first"
                )
            if rows:
                db.relocate_artifact(rows[0]["_rowid"], yml.artifact_name, new_p, new_ml)
                mirror_to_db(db, yml, new_p, new_ml, row_id=rows[0]["_rowid"])
            else:
                mirror_to_db(db, yml, new_p, new_ml)
        except Exception as exc:
            try:
                new_p.unlink()
            except OSError:
                pass
            return ServiceResult.fail([f"Rename rolled back — DB could not be updated: {exc}"])

        warnings: list[str] = []
        try:
            old_p.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            warnings.append(f"old YML {old_p} could not be removed ({exc})")

        # move the modlog sidecar so history follows the record
        if old_ml.exists() and not new_ml.exists():
            try:
                os.replace(old_ml, new_ml)
            except OSError as exc:
                warnings.append(f"modlog not moved ({exc})")

        self._write_modlog(yml, actor, Role.CURATOR, Action.UPDATED, new_ml, note=rename_note)
        try:
            self._remove_from_manifest(old_ident[2], old_ident[0], actor)
            self._update_manifest(yml, new_p, append=True)
        except Exception as exc:
            warnings.append(f"manifest not updated ({exc})")

        msg = f"Artifact renamed to '{yml.artifact_name}' and moved to {new_p}. Changes: {note}"
        if warnings:
            msg += " Warnings: " + "; ".join(warnings) + "."
        return ServiceResult.ok(msg)


# ── BlockService ──────────────────────────────────────────────────────────────

class BlockService(_BaseService):
    """Blocks an artifact in curation or moving phase."""

    def execute(
        self, yml: ArtifactYML, actor: str, role: Role,
        phase: str, reason: str, db,
    ) -> ServiceResult:

        if not reason.strip():
            return ServiceResult.fail(["Block reason is required."])

        allowed = ALLOWED_TRANSITIONS.get((yml.status, role), set())
        if Status.BLOCKED not in allowed:
            return ServiceResult.fail([
                f"Cannot block from status '{yml.status.value}' as {role.value}."
            ])

        yml.append_status_reason(phase, actor, reason)
        yml.status = Status.BLOCKED
        self._touch(yml, actor)

        yml_p, ml_p = self._paths(yml)

        try:
            save_artifact_yml(yml_p, yml)
        except OSError as exc:
            return ServiceResult.fail([f"Failed to write YML: {exc}"])

        db_warning = self._sync_db(db, yml, yml_p, ml_p)
        self._write_modlog(yml, actor, role, Action.BLOCKED, ml_p)
        self._update_manifest(yml, yml_p, append=False)

        return self._ok(f"Artifact blocked: {reason}", db_warning)


# ── RejectService ─────────────────────────────────────────────────────────────

class RejectService(_BaseService):
    """Rejects an artifact — soft, can be reinstated."""

    def execute(
        self, yml: ArtifactYML, actor: str, role: Role,
        phase: str, reason: str, db,
    ) -> ServiceResult:

        if not reason.strip():
            return ServiceResult.fail(["Rejection reason is required."])

        allowed = ALLOWED_TRANSITIONS.get((yml.status, role), set())
        if Status.REJECTED not in allowed:
            return ServiceResult.fail([
                f"Cannot reject from status '{yml.status.value}' as {role.value}."
            ])

        yml.append_status_reason(phase, actor, reason)
        yml.status = Status.REJECTED
        self._touch(yml, actor)

        yml_p, ml_p = self._paths(yml)

        try:
            save_artifact_yml(yml_p, yml)
        except OSError as exc:
            return ServiceResult.fail([f"Failed to write YML: {exc}"])

        db_warning = self._sync_db(db, yml, yml_p, ml_p)
        self._write_modlog(yml, actor, role, Action.REJECTED, ml_p)
        self._update_manifest(yml, yml_p, append=False)

        return self._ok(f"Artifact rejected: {reason}", db_warning)


# ── ReinstateService ──────────────────────────────────────────────────────────

class ReinstateService(_BaseService):
    """Reinstates a REJECTED artifact back to STAGED."""

    def execute(
        self, yml: ArtifactYML, actor: str, role: Role,
        phase: str, clearing_reason: str, db,
    ) -> ServiceResult:

        allowed = ALLOWED_TRANSITIONS.get((yml.status, role), set())
        if Status.STAGED not in allowed:
            return ServiceResult.fail([
                f"Cannot reinstate from status '{yml.status.value}' as {role.value}."
            ])

        if clearing_reason.strip():
            yml.append_status_reason(phase, actor, f"reinstated: {clearing_reason}")

        yml.status = Status.STAGED
        self._touch(yml, actor)

        yml_p, ml_p = self._paths(yml)

        try:
            save_artifact_yml(yml_p, yml)
        except OSError as exc:
            return ServiceResult.fail([f"Failed to write YML: {exc}"])

        db_warning = self._sync_db(db, yml, yml_p, ml_p)
        self._write_modlog(yml, actor, role, Action.UPDATED, ml_p)
        self._update_manifest(yml, yml_p, append=False)

        return self._ok("Artifact reinstated to staged.", db_warning)


# ── MoveService ───────────────────────────────────────────────────────────────

class MoveService(_BaseService):
    """
    Confirms an artifact move from cpup to gpup.
    Sets moved_by, date_moved, partition → GPU.
    Propagates any clearing reason from moving_status_reason into modlog.
    """

    def execute(self, yml: ArtifactYML, actor: str, db) -> ServiceResult:
        allowed = ALLOWED_TRANSITIONS.get((yml.status, Role.MOVER), set())
        if Status.MOVED not in allowed:
            return ServiceResult.fail([
                f"Cannot move artifact with status '{yml.status.value}'."
            ])

        # Set actor fields before validation so moved_by check passes
        now            = datetime.now(tz=timezone.utc)
        yml.moved_by   = actor
        yml.date_moved = now

        errors = yml.validate_moving_fields()
        if errors:
            return ServiceResult.fail(errors)

        yml.partition = Partition.GPU
        yml.status    = Status.MOVED
        self._touch(yml, actor)

        yml_p, ml_p = self._paths(yml)

        try:
            save_artifact_yml(yml_p, yml)
        except OSError as exc:
            return ServiceResult.fail([f"Failed to write YML: {exc}"])

        db_warning = self._sync_db(db, yml, yml_p, ml_p)
        self._write_modlog(yml, actor, Role.MOVER, Action.MOVED, ml_p)
        self._update_manifest(yml, yml_p, append=False)

        return self._ok(
            f"Artifact '{yml.artifact_name}' moved successfully.", db_warning
        )


class MovingUpdateService(_BaseService):
    """Handles mover edits to moving fields after confirmation."""

    def execute(
        self,
        yml: ArtifactYML,
        actor: str,
        updated_fields: dict,
        db,
    ) -> ServiceResult:

        yml_p, ml_p = self._paths(yml)
        before = load_artifact_yml(yml_p)

        for field, value in updated_fields.items():
            if hasattr(yml, field):
                setattr(yml, field, value)
            else:
                return ServiceResult.fail([f"Unknown field: '{field}'"])

        self._touch(yml, actor)

        try:
            save_artifact_yml(yml_p, yml)
        except OSError as exc:
            return ServiceResult.fail([f"Failed to write YML: {exc}"])

        note = self._diff_note(before, yml, set(updated_fields.keys()))
        self._sync_db(db, yml, yml_p, ml_p)
        self._write_modlog(yml, actor, Role.MOVER, Action.UPDATED, ml_p, note=note)
        self._update_manifest(yml, yml_p, append=False)

        return ServiceResult.ok(f"Moving fields updated. Changes: {note}")


# ── DeleteService ─────────────────────────────────────────────────────────────

class DeleteService(_BaseService):
    """
    Deletes an artifact RECORD — the YML, its DB row(s) and its manifest
    entry. The artifact data itself (source/destination paths) is never
    touched. The modlog sidecar is kept as the audit trail.

    Order matters, because sync recreates whichever half is missing:
      1. modlog gets a DELETED entry first — sync treats a record whose
         modlog ends in DELETED as retired and never recreates it
      2. DB row(s) removed — on failure the marker is countermanded with an
         UPDATED entry, so nothing is left half-deleted
      3. YML removed
      4. manifest entry removed
    """

    ALLOWED_ROLES = (Role.CURATOR, Role.ADMIN)

    def execute(
        self, yml: ArtifactYML, actor: str, role: Role, reason: str, db,
    ) -> ServiceResult:
        if role not in self.ALLOWED_ROLES:
            return ServiceResult.fail([f"Role '{role.value}' cannot delete records."])
        if not reason.strip():
            return ServiceResult.fail(["A reason is required to delete a record."])

        yml_p, ml_p = self._paths(yml)
        label = f"{yml.aimcr_reference}/{yml.artifact_type.value}/{yml.artifact_name}"

        self._write_modlog(yml, actor, role, Action.DELETED, ml_p,
                           note=f"record deleted: {reason.strip()}")

        try:
            n = db.delete_artifact_rows(yml_path=str(yml_p))
        except Exception as exc:
            self._write_modlog(yml, actor, role, Action.UPDATED, ml_p,
                               note=f"delete aborted — DB error: {exc}")
            return ServiceResult.fail([f"Delete aborted, nothing removed: DB error: {exc}"])

        try:
            yml_p.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            return ServiceResult.fail([
                f"DB row(s) removed but {yml_p} could not be deleted ({exc}). "
                "Remove it by hand; sync will not restore it (deleted marker)."
            ])

        try:
            self._remove_from_manifest(yml.aimcr_reference, yml.artifact_name, actor)
        except Exception as exc:
            return ServiceResult.ok(
                f"Record {label} deleted ({n} DB row(s)); manifest entry could not be removed: {exc}"
            )

        return ServiceResult.ok(
            f"Record {label} deleted ({n} DB row(s)). Data at the source and "
            "destination paths was not touched; the modlog is kept for audit."
        )
