"""
io/database.py
--------------
SQLite database interface.

The DB is a read-optimised mirror of the YML files.
Source of truth is always the ArtifactYML on disk.

Rules:
- One Database instance per app session, created in app.py.
- All mutating methods are called AFTER a successful YML write.
- If a DB write fails the YML is still correct — log and continue.
- Schema migrations are additive only — ALTER TABLE ADD COLUMN.
  Never DROP or RENAME columns without a version bump.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import DB_PATH

logger = logging.getLogger(__name__)


# ── schema ────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS projects (
    project_id        TEXT PRIMARY KEY,
    proposal_title    TEXT,
    pi_username       TEXT,
    manifest_path     TEXT NOT NULL UNIQUE,
    created_at        TEXT,
    last_modified     TEXT,
    last_modified_by  TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_name            TEXT NOT NULL,
    artifact_type            TEXT NOT NULL,
    artifact_version         TEXT,
    project_id               TEXT NOT NULL REFERENCES projects(project_id),
    status                   TEXT NOT NULL,
    partition                TEXT,
    curated_by               TEXT,
    moved_by                 TEXT,
    date_staged              TEXT,
    date_moved               TEXT,
    checksum_verified_cpup   INTEGER DEFAULT 0,
    checksum_verified_gpup   INTEGER DEFAULT 0,
    reference                INTEGER DEFAULT 0,
    source_path              TEXT,
    destination_path         TEXT,
    yml_path                 TEXT NOT NULL UNIQUE,
    modlog_path              TEXT,
    last_modified            TEXT,
    last_modified_by         TEXT,
    tracking_ticket          INTEGER,
    dm_ticket_number         INTEGER,
    aimcr_reference          INTEGER,
    checksum_type            TEXT,
    checksum_source          TEXT,
    checksum_value           TEXT,
    size_gb                  REAL,
    reference_group          TEXT
);

CREATE TABLE IF NOT EXISTS modlog_entries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_name  TEXT NOT NULL,
    yml_path       TEXT NOT NULL REFERENCES artifacts(yml_path),
    actor          TEXT NOT NULL,
    role           TEXT NOT NULL,
    action         TEXT NOT NULL,
    timestamp      TEXT NOT NULL,
    note           TEXT
);

CREATE INDEX IF NOT EXISTS idx_artifacts_project
    ON artifacts(project_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_status
    ON artifacts(status);
CREATE INDEX IF NOT EXISTS idx_modlog_yml
    ON modlog_entries(yml_path);
CREATE INDEX IF NOT EXISTS idx_modlog_actor
    ON modlog_entries(actor);
"""


class Database:
    """
    Wraps a single SQLite connection for the app session.
    Thread safety: SQLite in WAL mode handles concurrent readers;
    writes are serialised by the GIL since Streamlit runs in one process.
    """

    # Columns added 2026-06 during schema unification with the sysadmin
    # tracker. Applied as additive ALTER TABLE ADD COLUMN, never DROP/RENAME,
    # per the migration rule above. Safe to re-run: "duplicate column name"
    # is swallowed for DBs (like the shared production tracker.db) where the
    # sysadmin team already applied these manually.
    _ARTIFACT_COLUMNS_2026_06 = [
        ("tracking_ticket",  "INTEGER"),
        ("dm_ticket_number", "INTEGER"),
        ("aimcr_reference",  "INTEGER"),
        ("checksum_type",    "TEXT"),
        ("checksum_source",  "TEXT"),
        ("checksum_value",   "TEXT"),
        ("size_gb",          "REAL"),
        ("reference_group",  "TEXT"),
    ]

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._path = db_path
        self._conn: sqlite3.Connection = self._connect()
        self._init_schema()
        self._migrate_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_schema(self) -> None:
        self._conn.executescript(_DDL)
        self._conn.commit()

    def _migrate_schema(self) -> None:
        """Apply additive column migrations not covered by CREATE TABLE IF NOT EXISTS."""
        for col, sql_type in self._ARTIFACT_COLUMNS_2026_06:
            try:
                self._conn.execute(
                    f"ALTER TABLE artifacts ADD COLUMN {col} {sql_type}"
                )
                self._conn.commit()
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

    @contextmanager
    def _tx(self):
        """Context manager for a single transaction."""
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ── project operations ────────────────────────────────────────────────────

    def upsert_project(self, manifest) -> None:
        """
        Insert or replace a project row from a ManifestYML instance.
        Called when a manifest is created or updated.
        """
        from config import manifest_path as mp  # mp(aimcr_reference)
        sql = """
            INSERT INTO projects
                (project_id, proposal_title, pi_username, manifest_path,
                 created_at, last_modified, last_modified_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                proposal_title   = excluded.proposal_title,
                pi_username      = excluded.pi_username,
                last_modified    = excluded.last_modified,
                last_modified_by = excluded.last_modified_by
        """
        with self._tx():
            self._conn.execute(sql, (
                manifest.project_id,
                manifest.proposal_title,
                manifest.pi_username,
                str(mp(manifest.project_id)),
                manifest.manifest_created.isoformat() if manifest.manifest_created else "",
                manifest.last_modified.isoformat() if manifest.last_modified else "",
                manifest.last_modified_by,
            ))

    def get_project(self, project_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()

    def list_projects(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM projects ORDER BY project_id"
        ).fetchall()

    # ── artifact operations ───────────────────────────────────────────────────

    def upsert_artifact(self, yml, yml_path: Path, modlog_path: Path) -> None:
        """
        Insert or update an artifact row from an ArtifactYML instance.
        Called after every successful YML write.

        checksum_value (sysadmin schema) maps to yml.checksum_filename —
        the dashboard stores the checksum file name, not a raw hash value.
        """
        sql = """
            INSERT INTO artifacts
                (artifact_name, artifact_type, artifact_version,
                 project_id, status, partition, curated_by, moved_by,
                 date_staged, date_moved,
                 checksum_verified_cpup, checksum_verified_gpup,
                 reference, source_path, destination_path,
                 yml_path, modlog_path, last_modified, last_modified_by,
                 tracking_ticket, dm_ticket_number, aimcr_reference,
                 checksum_type, checksum_source, checksum_value,
                 size_gb, reference_group)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(yml_path) DO UPDATE SET
                status                 = excluded.status,
                partition              = excluded.partition,
                moved_by               = excluded.moved_by,
                date_moved             = excluded.date_moved,
                checksum_verified_cpup = excluded.checksum_verified_cpup,
                checksum_verified_gpup = excluded.checksum_verified_gpup,
                reference              = excluded.reference,
                last_modified          = excluded.last_modified,
                last_modified_by       = excluded.last_modified_by,
                tracking_ticket        = excluded.tracking_ticket,
                dm_ticket_number       = excluded.dm_ticket_number,
                aimcr_reference        = excluded.aimcr_reference,
                checksum_type          = excluded.checksum_type,
                checksum_source        = excluded.checksum_source,
                checksum_value         = excluded.checksum_value,
                size_gb                = excluded.size_gb,
                reference_group        = excluded.reference_group
        """
        with self._tx():
            self._conn.execute(sql, (
                yml.artifact_name,
                yml.artifact_type.value,
                yml.artifact_version,
                yml.aimcr_reference,
                yml.status.value,
                yml.partition.value,
                yml.curated_by,
                yml.moved_by,
                yml.date_staged.isoformat() if yml.date_staged else "",
                yml.date_moved.isoformat() if yml.date_moved else "",
                int(yml.checksum_verified_cpup.verified),
                int(yml.checksum_verified_gpup.verified),
                int(yml.reference),
                yml.source_path,
                yml.destination_path,
                str(yml_path),
                str(modlog_path),
                yml.last_modified.isoformat() if yml.last_modified else "",
                yml.last_modified_by,
                yml.tracking_ticket,
                yml.dm_ticket_number,
                yml.aimcr_reference,
                yml.checksum_type.value,
                yml.checksum_source.value,
                yml.checksum_filename,
                yml.size_gb,
                yml.reference_group,
            ))

    def get_artifact(self, artifact_name: str, aimcr_reference: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM artifacts WHERE artifact_name=? AND project_id=?",
            (artifact_name, aimcr_reference),
        ).fetchone()

    def list_artifacts(
        self,
        aimcr_reference: Optional[str] = None,
        status: Optional[str] = None,
        artifact_type: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        """
        Flexible artifact listing with optional filters.
        All parameters are optional — omit to list everything.
        """
        clauses, params = [], []
        if aimcr_reference:
            clauses.append("project_id = ?")
            params.append(aimcr_reference)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if artifact_type:
            clauses.append("artifact_type = ?")
            params.append(artifact_type)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM artifacts {where} ORDER BY last_modified DESC"
        return self._conn.execute(sql, params).fetchall()

    # ── modlog operations ─────────────────────────────────────────────────────

    def insert_modlog_entry(self, entry, yml_path: str) -> None:
        """
        Insert a single ModLogEntry row.
        Called after a successful modlog write — best effort only.
        """
        sql = """
            INSERT INTO modlog_entries
                (artifact_name, yml_path, actor, role, action, timestamp, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        try:
            with self._tx():
                self._conn.execute(sql, (
                    entry.actor,    # artifact_name stored separately in entry
                    yml_path,
                    entry.actor,
                    entry.role.value,
                    entry.action.value,
                    entry.timestamp.isoformat(),
                    entry.note,
                ))
        except Exception as exc:
            logger.warning("DB modlog insert failed: %s", exc)

    def list_modlog(
        self,
        yml_path: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        clauses, params = [], []
        if yml_path:
            clauses.append("yml_path = ?")
            params.append(yml_path)
        if actor:
            clauses.append("actor = ?")
            params.append(actor)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._conn.execute(
            f"SELECT * FROM modlog_entries {where} ORDER BY timestamp DESC",
            params,
        ).fetchall()

    # ── reconciliation ────────────────────────────────────────────────────────

    def reconcile_from_ymls(self, yml_paths: list[Path]) -> tuple[int, list[str]]:
        """
        Startup reconciliation pass.
        Loads each YML and upserts the DB row to catch any drift.

        Upserts the project row first (forcing project_id to the artifact's
        own aimcr_reference, not whatever manifest.yml happens to contain) so
        artifacts whose project row never made it into the DB — e.g. ones
        skipped earlier by the same FK ordering bug this fixes — actually
        get healed instead of failing the same FK check again.

        Returns:
            (updated_count, skipped_paths)
            skipped_paths lists any files that could not be parsed,
            so the UI can surface them as warnings rather than crashing.
        """
        from iolib.yaml_io import load_artifact_yml, load_manifest
        from config import manifest_path as mlp_path
        from config import modlog_path as mlp
        from models.manifest import ManifestYML

        updated = 0
        skipped: list[str] = []

        for yp in yml_paths:
            try:
                yml = load_artifact_yml(yp)
                if yml is None:
                    skipped.append(str(yp))
                    continue

                mp = mlp_path(yml.aimcr_reference)
                manifest = load_manifest(mp)
                if manifest is None:
                    manifest = ManifestYML.new(
                        yml.aimcr_reference, yml.proposal_title, yml.pi_username
                    )
                manifest.project_id = yml.aimcr_reference
                self.upsert_project(manifest)

                ml_path = mlp(yml.aimcr_reference, yml.artifact_name, yml.artifact_type)
                self.upsert_artifact(yml, yp, ml_path)
                updated += 1
            except Exception as exc:
                logger.warning("Reconcile skipped %s: %s", yp, exc)
                skipped.append(str(yp))

        return updated, skipped

    def close(self) -> None:
        self._conn.close()