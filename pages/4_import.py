"""
pages/4_import.py
-----------------
Import an existing hand-authored YML into the tracker.

Flow:
  1. User pastes a path to an existing YML on sh3
  2. App loads it tolerantly (duplicate keys, missing fields handled)
  3. Prefilled form appears — editable fields depend on role:
       Curator  → can fill curation fields, moving fields locked
       Mover    → can fill moving fields, curation fields locked
  4. User fills any gaps, confirms
  5. App writes to DB, creates modlog sidecar, updates/creates manifest
  6. Original YML is left untouched — registered as-is

Startup reconciliation has been removed. All imports are manual and
explicit — no surprises on startup.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from auth.session import (
    current_db, current_role, current_username,
    init_session, require_role,
)
from components.fields import (
    render_checksum_verification, render_field,
    render_text_area, section_header, status_badge,
)
from config import (
    ArtifactType, ChecksumSource, ChecksumType,
    Partition, Role, Status,
    artifact_yml_path, modlog_path, manifest_path,
    ARTIFACT_TYPE_DIR,
)
from iolib.database import Database
from iolib.services import ServiceResult
from iolib.yaml_io import load_yaml, load_modlog, atomic_write, file_lock, load_manifest
from models.artifact import ArtifactYML, ChecksumVerification, StatusReasonEntry
from models.manifest import ManifestEntry, ManifestYML
from models.modlog import ArtifactModLog, ModLogEntry
from config import Action
from datetime import datetime, timezone


def main() -> None:
    db: Database = current_db()
    init_session(db)

    if not require_role(Role.CURATOR, Role.MOVER):
        st.stop()

    role   = current_role()
    actor  = current_username()

    st.title("Import existing YML")
    st.caption(
        "Load a hand-authored YML from disk, fill any missing fields, "
        "and register it in the tracker. The original file is not modified."
    )

    # ── step 1: path input ────────────────────────────────────────────────
    st.subheader("Step 1 — point to the YML file")

    path_input = st.text_input(
        "Absolute path to YML file",
        placeholder="/scratch/project/k03/support_team/datasets/65562/oMol25/omol25_v1.yml",
        key="import_path",
    )

    load_btn = st.button("Load file", type="primary", key="import_load")

    if load_btn and path_input.strip():
        p = Path(path_input.strip())
        if not p.exists():
            st.error(f"File not found: `{p}`")
            st.stop()
        if not p.suffix == ".yml" or p.name.endswith(".modlog.yml"):
            st.error("Please point to an artifact `.yml` file, not a modlog or manifest.")
            st.stop()

        raw, warnings = _load_with_warnings(p)
        st.session_state["import_raw"]      = raw
        st.session_state["import_src_path"] = str(p)
        st.session_state["import_warnings"] = warnings

    # ── step 2: form ──────────────────────────────────────────────────────
    if "import_raw" not in st.session_state:
        st.stop()

    raw      = st.session_state["import_raw"]
    src_path = st.session_state["import_src_path"]
    warnings = st.session_state["import_warnings"]

    st.divider()
    st.subheader("Step 2 — review and complete fields")
    st.caption(f"Loaded from: `{src_path}`")

    if warnings:
        with st.expander(f"{len(warnings)} issue(s) found in source file", expanded=True):
            for w in warnings:
                st.warning(w)

    yml = _raw_to_yml(raw)

    # ── identity ──────────────────────────────────────────────────────────
    section_header("Identity")
    c1, c2, c3 = st.columns(3)
    with c1:
        yml.artifact_name = st.text_input(
            "Artifact name *", value=yml.artifact_name, key="i_name"
        )
    with c2:
        yml.artifact_version = st.text_input(
            "Version *", value=yml.artifact_version or "1", key="i_ver"
        )
    with c3:
        yml.artifact_type = ArtifactType(st.selectbox(
            "Type *", [t.value for t in ArtifactType],
            index=_enum_idx(ArtifactType, yml.artifact_type),
            key="i_type",
        ))

    c1, c2 = st.columns(2)
    with c1:
        yml.proposal_title = st.text_input(
            "Proposal title *", value=yml.proposal_title, key="i_ptitle"
        )
    with c2:
        yml.pi_username = st.text_input(
            "PI username *", value=yml.pi_username, key="i_pi"
        )

    c1, c2, c3 = st.columns(3)
    with c1:
        yml.project_id = st.text_input(
            "Project ID (k#####) *", value=yml.project_id, key="i_pid"
        )
    with c2:
        yml.aimcr_reference = st.text_input(
            "AIMCR reference (6####) *", value=yml.aimcr_reference, key="i_aimcr"
        )
    with c3:
        yml.tracking_ticket = st.text_input(
            "Tracking ticket (6####) *", value=yml.tracking_ticket, key="i_tt"
        )

    # ── curation ──────────────────────────────────────────────────────────
    section_header("Curation", color="#1D9E75")
    curator_locked = (role == Role.MOVER)

    yml.dm_ticket_number = render_field(
        "DM ticket number (RT) *",
        yml.dm_ticket_number, not curator_locked, "i_dm",
        help="Required. Enter manually if missing from source file.",
    )

    c1, c2 = st.columns(2)
    with c1:
        yml.source_path = render_field(
            "Source path (cpup) *", yml.source_path,
            not curator_locked, "i_src",
        )
    with c2:
        yml.destination_path = render_field(
            "Destination path (gpup) *", yml.destination_path,
            not curator_locked, "i_dst",
        )

    c1, c2, c3 = st.columns(3)
    with c1:
        yml.checksum_type = ChecksumType(render_field(
            "Checksum type", yml.checksum_type.value,
            not curator_locked, "i_ctype",
            options=[t.value for t in ChecksumType],
        ))
    with c2:
        yml.checksum_source = ChecksumSource(render_field(
            "Checksum source", yml.checksum_source.value,
            not curator_locked, "i_csrc",
            options=[s.value for s in ChecksumSource],
        ))
    with c3:
        yml.checksum_filename = render_field(
            "Checksum filename *", yml.checksum_filename,
            not curator_locked, "i_cfn",
        )

    c1, c2 = st.columns(2)
    with c1:
        yml.size_gb = render_field(
            "Size (GB) *", yml.size_gb,
            not curator_locked, "i_size",
        )
    with c2:
        yml.reference = render_field(
            "Reference candidate", yml.reference,
            not curator_locked, "i_ref",
        )

    cv_cpup = render_checksum_verification(
        "Checksum verification — cpup",
        yml.checksum_verified_cpup,
        editable=not curator_locked,
        field_key="i_cv_cpup",
    )
    if not curator_locked:
        yml.checksum_verified_cpup = ChecksumVerification(
            verified=cv_cpup["verified"],
            verified_by=cv_cpup["verified_by"],
            verified_date=cv_cpup["verified_date"],
        )

    yml.curation_notes = render_text_area(
        "Curation notes", yml.curation_notes,
        editable=not curator_locked, field_key="i_cnotes",
    )

    # ── moving ────────────────────────────────────────────────────────────
    section_header("Moving", color="#534AB7")
    mover_locked = (role == Role.CURATOR)

    c1, c2 = st.columns(2)
    with c1:
        yml.moved_by = render_field(
            "Moved by", yml.moved_by,
            not mover_locked, "i_mby",
        )
    with c2:
        _date_moved_str = render_field(
            "Date moved (ISO)", str(yml.date_moved or ""),
            not mover_locked, "i_dmoved",
        )
        if not mover_locked and _date_moved_str:
            try:
                yml.date_moved = datetime.fromisoformat(_date_moved_str)
            except ValueError:
                st.caption("Invalid date format — leave empty if unknown.")

    yml.partition = Partition(render_field(
        "Partition", yml.partition.value,
        not mover_locked, "i_part",
        options=[p.value for p in Partition],
    ))

    cv_gpup = render_checksum_verification(
        "Checksum verification — gpup",
        yml.checksum_verified_gpup,
        editable=not mover_locked,
        field_key="i_cv_gpup",
    )
    if not mover_locked:
        yml.checksum_verified_gpup = ChecksumVerification(
            verified=cv_gpup["verified"],
            verified_by=cv_gpup["verified_by"],
            verified_date=cv_gpup["verified_date"],
        )

    yml.moving_notes = render_text_area(
        "Moving notes", yml.moving_notes,
        editable=not mover_locked, field_key="i_mnotes",
    )

    # ── status override ───────────────────────────────────────────────────
    section_header("Status")
    yml.status = Status(st.selectbox(
        "Status *",
        [s.value for s in Status],
        index=_enum_idx(Status, yml.status),
        key="i_status",
        help="Set to the current known status of this artifact.",
    ))

    # ── confirm ───────────────────────────────────────────────────────────
    st.divider()
    st.subheader("Step 3 — confirm import")

    errors = _validate_import(yml, role)
    if errors:
        for e in errors:
            st.error(e)

    confirm_disabled = bool(errors)
    if st.button(
        "Import artifact", type="primary",
        key="import_confirm", disabled=confirm_disabled,
    ):
        result = _do_import(yml, actor, role, db)
        if result.success:
            st.success(result.message)
            st.balloons()
            # Clear import state so the form resets
            for k in ["import_raw", "import_src_path", "import_warnings"]:
                st.session_state.pop(k, None)
            st.rerun()
        else:
            for e in result.errors:
                st.error(e)


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_with_warnings(path: Path) -> tuple[dict, list[str]]:
    """
    Load a YML file tolerantly, returning (data, human-readable warnings).
    Detects duplicate keys, missing required fields, and unknown fields.
    """
    from iolib.yaml_io import load_yaml

    warnings: list[str] = []

    # Detect duplicate keys in raw text before loading
    try:
        text = path.read_text(encoding="utf-8")
        keys_seen: set[str] = set()
        for line in text.splitlines():
            stripped = line.strip()
            if (
                ":" in stripped
                and not stripped.startswith("#")
                and not stripped.startswith("-")
            ):
                key = stripped.split(":")[0].strip()
                if key and key in keys_seen:
                    warnings.append(
                        f"Duplicate key **`{key}`** found — last value will be used. "
                        f"Consider cleaning the source file."
                    )
                if key:
                    keys_seen.add(key)
    except OSError as exc:
        warnings.append(f"Could not read file for pre-scan: {exc}")

    data = load_yaml(path)

    # Flag missing required fields
    required = [
        "artifact_name", "artifact_type", "aimcr_reference",
        "project_id", "pi_username", "source_path", "destination_path",
        "checksum_filename",
    ]
    for field in required:
        if not data.get(field):
            warnings.append(
                f"Missing required field **`{field}`** — please fill it in the form."
            )

    return data, warnings


def _raw_to_yml(raw: dict) -> ArtifactYML:
    """
    Convert a raw dict (from any hand-authored YML) to an ArtifactYML.
    Maps legacy field names to current schema where known.
    Fills defaults for anything missing.
    """
    # Legacy field name aliases
    aliases = {
        "name":           "artifact_name",
        "type":           "artifact_type",
        "version":        "artifact_version",
        "owner":          "curated_by",
        "pi":             "pi_username",
        "project":        "project_id",
        "aimcr":          "aimcr_reference",
        "ticket":         "tracking_ticket",
        "dm_ticket":      "dm_ticket_number",
        "checksum":       "checksum_filename",
        "checksum_value": "checksum_filename",
        "size(gb)":       "size_gb",
        "size_gb":        "size_gb",
        "notes":          "curation_notes",
        "staged_by":      "curated_by",
    }

    normalised = {}
    for k, v in raw.items():
        canonical = aliases.get(k.lower(), k)
        normalised[canonical] = v

    # Safe enum coercion
    def _type(val) -> ArtifactType:
        try:
            return ArtifactType(str(val).lower())
        except ValueError:
            return ArtifactType.DATASET

    def _status(val) -> Status:
        try:
            return Status(str(val).lower())
        except ValueError:
            return Status.STAGED

    def _partition(val) -> Partition:
        try:
            return Partition(str(val).lower())
        except ValueError:
            return Partition.CPU

    def _checksum_type(val) -> ChecksumType:
        try:
            return ChecksumType(str(val).lower())
        except ValueError:
            return ChecksumType.SHA256

    def _checksum_src(val) -> ChecksumSource:
        try:
            return ChecksumSource(str(val).lower())
        except ValueError:
            return ChecksumSource.SELF_GENERATED

    def _dt(val) -> datetime | None:
        if not val:
            return None
        try:
            return datetime.fromisoformat(str(val))
        except (ValueError, TypeError):
            return None

    def _float(val) -> float:
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0

    def _checksum_block(val) -> ChecksumVerification:
        if isinstance(val, dict):
            return ChecksumVerification(
                verified=bool(val.get("verified", False)),
                verified_by=str(val.get("verified_by", "")),
                verified_date=_dt(val.get("verified_date")),
            )
        return ChecksumVerification()

    return ArtifactYML(
        artifact_name    = str(normalised.get("artifact_name", "")),
        artifact_type    = _type(normalised.get("artifact_type", "dataset")),
        artifact_version = str(normalised.get("artifact_version", "1")),
        proposal_title   = str(normalised.get("proposal_title", "")),
        project_id       = str(normalised.get("project_id", "")),
        pi_username      = str(normalised.get("pi_username", "")),
        aimcr_reference  = str(normalised.get("aimcr_reference", "")),
        tracking_ticket  = str(normalised.get("tracking_ticket", "")),
        dm_ticket_number = str(normalised.get("dm_ticket_number", "")),
        curated_by       = str(normalised.get("curated_by", "")),
        date_staged      = _dt(normalised.get("date_staged") or normalised.get("creation_date")),
        metadata_creation_date = _dt(normalised.get("metadata_creation_date") or normalised.get("creation_date")),
        system           = str(normalised.get("system", "shaheen3")),
        partition        = _partition(normalised.get("partition", "cpu")),
        source_path      = str(normalised.get("source_path", "")),
        destination_path = str(normalised.get("destination_path", "")),
        checksum_type    = _checksum_type(normalised.get("checksum_type", "sha256")),
        checksum_source  = _checksum_src(normalised.get("checksum_source", "self-generated")),
        checksum_filename = str(normalised.get("checksum_filename", "")),
        size_gb          = _float(normalised.get("size_gb", 0.0)),
        checksum_verified_cpup = _checksum_block(normalised.get("checksum_verified_cpup", {})),
        reference        = bool(normalised.get("reference", False)),
        reference_group  = str(normalised.get("reference_group", "")),
        curation_notes   = str(normalised.get("curation_notes", "") or normalised.get("notes", "")),
        moved_by         = str(normalised.get("moved_by", "")),
        date_moved       = _dt(normalised.get("date_moved")),
        checksum_verified_gpup = _checksum_block(normalised.get("checksum_verified_gpup", {})),
        moving_notes     = str(normalised.get("moving_notes", "")),
        status           = _status(normalised.get("status", "staged")),
        last_modified    = _dt(normalised.get("last_modified")),
        last_modified_by = str(normalised.get("last_modified_by", "")),
    )


def _validate_import(yml: ArtifactYML, role: Role) -> list[str]:
    """
    Validate the import form — role-aware.
    Returns list of error strings. Empty = valid.
    """
    errors: list[str] = []

    # Identity — always required regardless of role
    if not yml.artifact_name.strip():
        errors.append("Artifact name is required.")
    if not yml.project_id.strip():
        errors.append("Project ID is required.")
    if not yml.pi_username.strip():
        errors.append("PI username is required.")
    if not yml.aimcr_reference.strip():
        errors.append("AIMCR reference is required.")
    if not yml.tracking_ticket.strip():
        errors.append("Tracking ticket is required.")

    # Curation fields — required when curator imports
    if role == Role.CURATOR:
        if not yml.dm_ticket_number.strip():
            errors.append("DM ticket number is required.")
        if not yml.source_path.strip():
            errors.append("Source path is required.")
        if not yml.checksum_filename.strip():
            errors.append("Checksum filename is required.")

    return errors


def _do_import(yml: ArtifactYML, actor: str, role: Role, db: Database) -> ServiceResult:
    """
    Register the imported artifact:
      1. Set last_modified metadata
      2. Write to DB
      3. Create modlog sidecar (IMPORTED action)
      4. Append/update manifest entry
    Original YML is not touched.
    """
    from models.manifest import ManifestYML, ManifestEntry

    now = datetime.now(tz=timezone.utc)
    yml.last_modified    = now
    yml.last_modified_by = actor

    if not yml.metadata_creation_date:
        yml.metadata_creation_date = now
    if not yml.date_staged and yml.status != Status.MOVED:
        yml.date_staged = now
    if not yml.curated_by and role == Role.CURATOR:
        yml.curated_by = actor
    if not yml.moved_by and role == Role.MOVER:
        yml.moved_by = actor

    # Derive YML path — for import we use the canonical path based on
    # aimcr_reference and artifact_type so the DB record points correctly.
    # The original file stays where it is.
    yml_p = artifact_yml_path(yml.aimcr_reference, yml.artifact_name, yml.artifact_type)
    ml_p  = modlog_path(yml.aimcr_reference, yml.artifact_name, yml.artifact_type)

    # DB upsert — upsert project first for FK constraint
    try:
        mp = manifest_path(yml.aimcr_reference)
        manifest = load_manifest(mp)
        if manifest is None:
            manifest = ManifestYML.new(
                yml.aimcr_reference, yml.proposal_title, yml.pi_username
            )
        # aimcr_reference is the authoritative key (see services._sync_db) —
        # don't trust a possibly-blank/stale project_id from manifest.yml.
        manifest.project_id = yml.aimcr_reference
        db.upsert_project(manifest)
        db.upsert_artifact(yml, yml_p, ml_p)
    except Exception as exc:
        return ServiceResult.fail([f"DB registration failed: {exc}"])

    # Modlog sidecar — create with IMPORTED action
    modlog = load_modlog(ml_p) or ArtifactModLog.new(yml, ml_p)
    entry = ModLogEntry(
        actor     = actor,
        role      = role,
        action    = Action.UPDATED,
        timestamp = now,
        note      = f"imported via dashboard by {actor} ({role.value})",
    )
    modlog.entries.append(entry)
    modlog.write_best_effort(ml_p, lambda p, d: atomic_write(p, d))

    # Manifest — append or update
    try:
        mp = manifest_path(yml.aimcr_reference)
        with file_lock(mp):
            manifest = load_manifest(mp)
            if manifest is None:
                manifest = ManifestYML.new(
                    yml.aimcr_reference, yml.proposal_title, yml.pi_username
                )
            entry_m = ManifestEntry.from_artifact_yml(yml, str(yml_p))
            try:
                manifest.append_artifact(entry_m)
            except ValueError:
                manifest.update_artifact(yml.artifact_name, **vars(entry_m))
            atomic_write(mp, manifest.to_dict())
    except Exception as exc:
        return ServiceResult.fail([f"Manifest update failed: {exc}"])

    return ServiceResult.ok(
        f"Artifact '{yml.artifact_name}' imported and registered successfully."
    )


def _enum_idx(enum_cls, value) -> int:
    """Return the index of value in enum_cls, defaulting to 0."""
    values = [e.value for e in enum_cls]
    try:
        return values.index(value.value if hasattr(value, "value") else value)
    except ValueError:
        return 0


main()