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
- Schema migrations are additive only — ALTER TABLE ADD COLUMN / CREATE INDEX.
  Never DROP or RENAME columns without a version bump.

Schema drift on the shared DB:
  The live artifacts table may have been created by the sysadmin schema,
  which has no UNIQUE constraint on yml_path (CREATE TABLE IF NOT EXISTS
  never adds one to an existing table). Two consequences this class handles:
    - ON CONFLICT(yml_path) upserts fail outright, so writes here use
      explicit select → update / insert keyed by rowid instead.
    - modlog_entries.yml_path REFERENCES artifacts(yml_path); with a
      non-unique parent, SQLite rejects every UPDATE of yml_path and every
      DELETE on artifacts ("foreign key mismatch") while foreign_keys=ON.
      Structural operations therefore run in _maintenance_tx() with FK
      enforcement off and handle modlog_entries explicitly.
  ensure_unique_yml_path() adds the missing unique index as soon as no
  duplicate yml_path rows remain (sync removes them), which makes the FK
  valid again and stops new duplicates at the source.
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


class DuplicateRowsError(RuntimeError):
    """Several artifact rows share one yml_path — resolved by iolib.sync."""


class Database:
    """
    Wraps a single SQLite connection for the app session.

    Journal mode is DELETE, not WAL: the DB lives on Lustre, where WAL's
    shared-memory index is not supported. busy_timeout absorbs short lock
    contention between sessions, the CLI, and the sysadmin tooling.
    """

    # Columns added 2026-06 during schema unification with the sysadmin
    # tracker. Applied as additive ALTER TABLE ADD COLUMN, never DROP/RENAME.
    # "duplicate column name" is swallowed for DBs where they already exist.
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

    # Project profile — fields shared by every artifact of a proposal, kept
    # so they can be auto-filled (see iolib.projects).
    _PROJECT_COLUMNS_2026_10 = [
        ("kaust_project_id", "TEXT"),
    ]

    _UNIQUE_YML_INDEX = "ux_artifacts_yml_path"

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._path = db_path
        self._conn: sqlite3.Connection = self._connect()
        self._init_schema()
        self._migrate_schema()
        self.ensure_unique_yml_path()

    @property
    def path(self) -> Path:
        return self._path

    # ── connection / schema ───────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        # timeout= sets the busy handler before anything else touches the file,
        # so every statement below waits up to 5 s for a lock instead of
        # failing immediately with "database is locked".
        conn = sqlite3.connect(str(self._path), timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        self._ensure_delete_journal(conn)
        return conn

    def _ensure_delete_journal(self, conn: sqlite3.Connection) -> None:
        """
        Move the DB to DELETE journal mode if it is not there already.
        Persistent in the file, so it only has to succeed once. Leaving WAL
        needs exclusive access; while another connection has the file open
        the switch fails — not fatal, retried on the next connect.
        """
        mode = "unknown"
        try:
            mode = str(conn.execute("PRAGMA journal_mode;").fetchone()[0]).lower()
            if mode == "delete":
                return
            new = str(conn.execute("PRAGMA journal_mode=DELETE;").fetchone()[0]).lower()
        except sqlite3.OperationalError as exc:
            logger.warning(
                "%s is in %s journal mode and could not be switched to DELETE "
                "(%s) — another connection has it open. Continuing in %s mode; "
                "the switch is retried on the next start.",
                self._path, mode, exc, mode,
            )
            return
        if new != "delete":
            logger.warning("%s journal mode is still %s after requesting DELETE.", self._path, new)
        else:
            logger.info("%s switched from %s to DELETE journal mode.", self._path, mode)

    def _init_schema(self) -> None:
        self._conn.executescript(_DDL)
        self._conn.commit()

    def _add_columns(self, table: str, columns: list[tuple[str, str]]) -> None:
        for col, sql_type in columns:
            try:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {sql_type}")
                self._conn.commit()
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

    def _migrate_schema(self) -> None:
        """Additive column migrations not covered by CREATE TABLE IF NOT EXISTS."""
        self._add_columns("artifacts", self._ARTIFACT_COLUMNS_2026_06)
        self._add_columns("projects", self._PROJECT_COLUMNS_2026_10)

    def has_unique_yml_path(self) -> bool:
        """True if artifacts.yml_path is covered by a UNIQUE constraint or index."""
        for idx in self._conn.execute("PRAGMA index_list(artifacts)").fetchall():
            if not idx["unique"]:
                continue
            cols = [r["name"] for r in self._conn.execute(f"PRAGMA index_info('{idx['name']}')")]
            if cols == ["yml_path"]:
                return True
        return False

    def ensure_unique_yml_path(self) -> tuple[bool, str]:
        """
        Add the UNIQUE index on artifacts.yml_path if it is missing and no
        duplicates remain. Returns (has_unique_now, message). Never raises.
        """
        try:
            if self.has_unique_yml_path():
                return True, ""
            dupes = self._conn.execute(
                "SELECT COUNT(*) FROM (SELECT yml_path FROM artifacts "
                "GROUP BY yml_path HAVING COUNT(*) > 1)"
            ).fetchone()[0]
            if dupes:
                msg = (f"{dupes} yml_path value(s) are shared by several rows; unique "
                       "index not added yet (sync resolves the duplicates first).")
                logger.warning("%s: %s", self._path, msg)
                return False, msg
            self._conn.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {self._UNIQUE_YML_INDEX} "
                "ON artifacts(yml_path)"
            )
            self._conn.commit()
            logger.info("%s: added unique index on artifacts.yml_path.", self._path)
            return True, "unique index on artifacts.yml_path added"
        except sqlite3.Error as exc:
            logger.warning("%s: could not add unique index on yml_path: %s", self._path, exc)
            return False, str(exc)

    @contextmanager
    def _tx(self):
        """Context manager for a single transaction."""
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    @contextmanager
    def _maintenance_tx(self):
        """
        Transaction with foreign-key enforcement off, for structural changes
        (relocate / delete rows). Needed because a non-unique yml_path makes
        SQLite reject these with "foreign key mismatch". Callers keep
        modlog_entries consistent themselves.
        """
        if self._conn.in_transaction:
            self._conn.commit()
        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            self._conn.execute("PRAGMA foreign_keys=ON")

    # ── project operations ────────────────────────────────────────────────────

    def upsert_project(self, manifest) -> None:
        """
        Insert or update a project row from a ManifestYML instance.

        Empty incoming values never blank out stored ones — the row doubles
        as the project profile used for auto-fill. Explicit select/update/
        insert so it works whether or not project_id carries a constraint.
        """
        from config import manifest_path as mp
        pid = manifest.project_id
        vals = {
            "proposal_title":   manifest.proposal_title or "",
            "pi_username":      manifest.pi_username or "",
            "kaust_project_id": getattr(manifest, "kaust_project_id", "") or "",
            "last_modified":    manifest.last_modified.isoformat() if manifest.last_modified else "",
            "last_modified_by": manifest.last_modified_by or "",
        }
        with self._tx():
            row = self._conn.execute(
                "SELECT rowid AS _rowid FROM projects WHERE project_id = ? LIMIT 1", (pid,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO projects
                       (project_id, proposal_title, pi_username, kaust_project_id,
                        manifest_path, created_at, last_modified, last_modified_by)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (pid, vals["proposal_title"], vals["pi_username"], vals["kaust_project_id"],
                     str(mp(pid)),
                     manifest.manifest_created.isoformat() if manifest.manifest_created else "",
                     vals["last_modified"], vals["last_modified_by"]),
                )
            else:
                self._conn.execute(
                    """UPDATE projects SET
                         proposal_title   = COALESCE(NULLIF(?, ''), proposal_title),
                         pi_username      = COALESCE(NULLIF(?, ''), pi_username),
                         kaust_project_id = COALESCE(NULLIF(?, ''), kaust_project_id),
                         last_modified    = COALESCE(NULLIF(?, ''), last_modified),
                         last_modified_by = COALESCE(NULLIF(?, ''), last_modified_by)
                       WHERE rowid = ?""",
                    (vals["proposal_title"], vals["pi_username"], vals["kaust_project_id"],
                     vals["last_modified"], vals["last_modified_by"], row["_rowid"]),
                )

    def get_project(self, project_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()

    def list_projects(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM projects ORDER BY project_id"
        ).fetchall()

    # ── artifact operations ───────────────────────────────────────────────────

    _MIRROR_COLUMNS = (
        "artifact_name", "artifact_type", "artifact_version", "project_id",
        "status", "partition", "curated_by", "moved_by", "date_staged", "date_moved",
        "checksum_verified_cpup", "checksum_verified_gpup", "reference",
        "source_path", "destination_path", "modlog_path",
        "last_modified", "last_modified_by", "tracking_ticket", "dm_ticket_number",
        "aimcr_reference", "checksum_type", "checksum_source", "checksum_value",
        "size_gb", "reference_group",
    )

    @staticmethod
    def _mirror_values(yml, modlog_path: Path) -> dict:
        """checksum_value (sysadmin schema) holds yml.checksum_filename."""
        return {
            "artifact_name":          yml.artifact_name,
            "artifact_type":          yml.artifact_type.value,
            "artifact_version":       yml.artifact_version,
            "project_id":             yml.aimcr_reference,
            "status":                 yml.status.value,
            "partition":              yml.partition.value,
            "curated_by":             yml.curated_by,
            "moved_by":               yml.moved_by,
            "date_staged":            yml.date_staged.isoformat() if yml.date_staged else "",
            "date_moved":             yml.date_moved.isoformat() if yml.date_moved else "",
            "checksum_verified_cpup": int(yml.checksum_verified_cpup.verified),
            "checksum_verified_gpup": int(yml.checksum_verified_gpup.verified),
            "reference":              int(yml.reference),
            "source_path":            yml.source_path,
            "destination_path":       yml.destination_path,
            "modlog_path":            str(modlog_path),
            "last_modified":          yml.last_modified.isoformat() if yml.last_modified else "",
            "last_modified_by":       yml.last_modified_by,
            "tracking_ticket":        yml.tracking_ticket,
            "dm_ticket_number":       yml.dm_ticket_number,
            "aimcr_reference":        yml.aimcr_reference,
            "checksum_type":          yml.checksum_type.value,
            "checksum_source":        yml.checksum_source.value,
            "checksum_value":         yml.checksum_filename,
            "size_gb":                yml.size_gb,
            "reference_group":        yml.reference_group,
        }

    def upsert_artifact(
        self, yml, yml_path: Path, modlog_path: Path, row_id: Optional[int] = None,
    ) -> int:
        """
        Insert or update the artifact row for `yml_path` from an ArtifactYML.
        Every mirrored column is refreshed from the YML (source of truth).

        row_id: update exactly that row (sync uses this). Otherwise the row
        is found by yml_path; several rows sharing it raise DuplicateRowsError
        (the YML is still correct on disk; the next sync resolves the rows).
        yml_path itself is never written by an UPDATE here — moving a row to
        another path is relocate_artifact()'s job.
        Returns the rowid written.
        """
        vals = self._mirror_values(yml, modlog_path)
        # Without the unique index SQLite rejects any UPDATE on artifacts
        # ("foreign key mismatch"), so writes go through the FK-relaxed path
        # until sync has removed the duplicates and added the index.
        tx = self._tx if self.has_unique_yml_path() else self._maintenance_tx
        with tx():
            if row_id is None:
                ids = [r[0] for r in self._conn.execute(
                    "SELECT rowid AS _rowid FROM artifacts WHERE yml_path = ?", (str(yml_path),)
                ).fetchall()]
                if len(ids) > 1:
                    raise DuplicateRowsError(
                        f"{len(ids)} DB rows share yml_path {yml_path} (rowids {ids})"
                    )
                row_id = ids[0] if ids else None

            if row_id is None:
                cols = ("yml_path",) + self._MIRROR_COLUMNS
                cur = self._conn.execute(
                    f"INSERT INTO artifacts ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    [str(yml_path)] + [vals[c] for c in self._MIRROR_COLUMNS],
                )
                return cur.lastrowid

            self._conn.execute(
                f"UPDATE artifacts SET {', '.join(f'{c} = ?' for c in self._MIRROR_COLUMNS)} "
                "WHERE rowid = ?",
                [vals[c] for c in self._MIRROR_COLUMNS] + [row_id],
            )
            return row_id

    def get_artifact(self, artifact_name: str, aimcr_reference: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT rowid AS _rowid, * FROM artifacts WHERE artifact_name=? AND project_id=?",
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
        Every row carries `_rowid` — the only identifier guaranteed unique
        on the shared DB (yml_path may repeat; see module docstring).
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
        sql = f"SELECT rowid AS _rowid, * FROM artifacts {where} ORDER BY last_modified DESC"
        return self._conn.execute(sql, params).fetchall()

    def find_duplicate_groups(self) -> list[dict]:
        """
        Groups of artifact rows that are duplicates of each other — sharing
        a yml_path, or the same (aimcr_reference, type, name). For UI alerts.
        Each group: {"reason": str, "label": str, "rowids": [int, ...]}.
        """
        groups: list[dict] = []
        for r in self._conn.execute(
            "SELECT yml_path, GROUP_CONCAT(rowid) AS ids FROM artifacts "
            "GROUP BY yml_path HAVING COUNT(*) > 1"
        ):
            groups.append({"reason": "same YML path", "label": r["yml_path"],
                           "rowids": [int(x) for x in r["ids"].split(",")]})
        for r in self._conn.execute(
            "SELECT COALESCE(NULLIF(CAST(aimcr_reference AS TEXT), ''), project_id) AS ref, "
            "artifact_type, artifact_name, GROUP_CONCAT(rowid) AS ids, "
            "COUNT(DISTINCT yml_path) AS paths FROM artifacts "
            "GROUP BY ref, artifact_type, artifact_name HAVING COUNT(*) > 1 AND paths > 1"
        ):
            groups.append({"reason": "same AIMCR reference, type and name",
                           "label": f"{r['ref']}/{r['artifact_type']}/{r['artifact_name']}",
                           "rowids": [int(x) for x in r["ids"].split(",")]})
        return groups

    def relocate_artifact(
        self, row_id: int, artifact_name: str, yml_path: Path, modlog_path: Path,
    ) -> None:
        """
        Point one row (by rowid) at a new name / yml_path in one transaction.
        modlog_entries follow only if no other row still uses the old path.
        Raises sqlite3.IntegrityError if the unique index exists and the
        new path is taken.
        """
        new_s = str(yml_path)
        with self._maintenance_tx():
            old = self._conn.execute(
                "SELECT yml_path FROM artifacts WHERE rowid = ?", (row_id,)
            ).fetchone()
            if old is None:
                raise KeyError(f"artifact rowid {row_id} not found")
            old_s = old["yml_path"]
            self._conn.execute(
                "UPDATE artifacts SET artifact_name = ?, yml_path = ?, modlog_path = ? "
                "WHERE rowid = ?",
                (artifact_name, new_s, str(modlog_path), row_id),
            )
            others = self._conn.execute(
                "SELECT COUNT(*) FROM artifacts WHERE yml_path = ?", (old_s,)
            ).fetchone()[0]
            if not others and old_s != new_s:
                self._conn.execute(
                    "UPDATE modlog_entries SET yml_path = ?, artifact_name = ? WHERE yml_path = ?",
                    (new_s, artifact_name, old_s),
                )

    def repoint_artifact(
        self, old_yml_path: str, new_yml_path: Path, new_modlog_path: Path
    ) -> None:
        """Move the single row at old_yml_path to new_yml_path (name unchanged)."""
        rows = self._conn.execute(
            "SELECT rowid AS _rowid, artifact_name FROM artifacts WHERE yml_path = ?", (old_yml_path,)
        ).fetchall()
        if len(rows) != 1:
            raise DuplicateRowsError(
                f"expected one row at {old_yml_path}, found {len(rows)}"
            )
        self.relocate_artifact(rows[0]["_rowid"], rows[0]["artifact_name"],
                               new_yml_path, new_modlog_path)

    def delete_artifact_rows(self, yml_path: Optional[str] = None,
                             row_id: Optional[int] = None) -> int:
        """
        Delete artifact rows by yml_path (all rows sharing it) or by rowid,
        plus their modlog_entries when no row is left on that path.
        Returns the number of artifact rows deleted.
        """
        if (yml_path is None) == (row_id is None):
            raise ValueError("pass exactly one of yml_path or row_id")
        with self._maintenance_tx():
            if row_id is not None:
                r = self._conn.execute(
                    "SELECT yml_path FROM artifacts WHERE rowid = ?", (row_id,)
                ).fetchone()
                if r is None:
                    return 0
                yml_path = r["yml_path"]
                n = self._conn.execute("DELETE FROM artifacts WHERE rowid = ?", (row_id,)).rowcount
            else:
                n = self._conn.execute(
                    "DELETE FROM artifacts WHERE yml_path = ?", (yml_path,)
                ).rowcount
            left = self._conn.execute(
                "SELECT COUNT(*) FROM artifacts WHERE yml_path = ?", (yml_path,)
            ).fetchone()[0]
            if not left:
                self._conn.execute("DELETE FROM modlog_entries WHERE yml_path = ?", (yml_path,))
            return n

    # ── modlog operations ─────────────────────────────────────────────────────

    def insert_modlog_entry(self, entry, yml_path: str) -> None:
        """Insert a single ModLogEntry row. Best effort only."""
        sql = """
            INSERT INTO modlog_entries
                (artifact_name, yml_path, actor, role, action, timestamp, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        try:
            with self._tx():
                self._conn.execute(sql, (
                    entry.actor,
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

    def close(self) -> None:
        self._conn.close()
