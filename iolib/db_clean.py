"""
iolib/db_clean.py
-----------------
Remove artifact rows that earlier versions created from YAML files that are
not artifact records (conda env files, manifests, modlog sidecars, HPC
workspace artifact.yml, legacy hand-authored files, ...), when the startup
scan parsed every .yml it found into a mostly blank row.

Each artifact row is classified:

  foreign      yml_path exists but is not one of our records
               (fails iolib.yaml_io.classify_artifact_yml)      → removed
  no_identity  yml_path does not exist and the row has no artifact_name
               or no AIMCR reference — nothing to rebuild from  → removed
  unplaceable  yml_path does not exist and the identity cannot be placed
               on disk (reference not 6####, unknown type, unsafe name);
               possibly a sysadmin row                         → reported only
  ok           our record, or a valid DB-only row sync will write a YML for

Projects rows with no artifact referencing them and a project_id that is
not an AIMCR reference (e.g. '' from blank rows) are removed too.

Only DB rows change — no file on disk is touched. apply() refuses to run on
a corrupt DB and writes a backup next to it first (SQLite online backup, safe
while the dashboard is running).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from config import AIMCR_REFERENCE_PATTERN
from iolib.yaml_io import classify_artifact_yml

_REF_RE = re.compile(AIMCR_REFERENCE_PATTERN)


@dataclass
class Finding:
    rowid: int
    category: str
    name: str
    ref: str
    yml_path: str
    reason: str


@dataclass
class CleanPlan:
    remove: list[Finding] = field(default_factory=list)
    report: list[Finding] = field(default_factory=list)
    kept: int = 0
    orphan_projects: list = field(default_factory=list)   # raw project_id values


def plan(db) -> CleanPlan:
    from iolib.sync import _placement_problem, _row_key
    out = CleanPlan()
    seen: set[int] = set()
    for r in (dict(x) for x in db.list_artifacts()):
        rid = r["_rowid"]
        if rid in seen:          # a corrupt DB can return a row twice
            continue
        seen.add(rid)
        ref, atype, name = _row_key(r)
        yp = str(r.get("yml_path") or "")
        p = Path(yp) if yp else None

        def f(cat: str, why: str) -> Finding:
            return Finding(rid, cat, name, ref, yp, why)

        if p is not None and (p.exists() or p.is_symlink()):
            data, reason = classify_artifact_yml(p)
            if data is None:
                out.remove.append(f("foreign", reason))
            else:
                out.kept += 1
            continue
        if not name.strip() or not ref.strip():
            out.remove.append(f("no_identity",
                                "file missing and no artifact_name / AIMCR reference"))
            continue
        problem = _placement_problem((ref, atype, name))
        if problem:
            out.report.append(f("unplaceable", f"file missing; {problem}"))
            continue
        out.kept += 1

    removing = {x.rowid for x in out.remove}
    used = {str(r["project_id"]) for r in (dict(x) for x in db.list_artifacts())
            if r["_rowid"] not in removing}
    for pr in db.list_projects():
        raw = pr["project_id"]                      # may be NULL or ''
        if str(raw) not in used and not _REF_RE.fullmatch(str(raw or "")):
            out.orphan_projects.append(raw)
    return out


def backup(db) -> Path:
    """Online backup next to the DB: <db>.backup-YYYYmmdd-HHMMSS."""
    dst = db.path.with_name(f"{db.path.name}.backup-{datetime.now():%Y%m%d-%H%M%S}")
    out = sqlite3.connect(dst)
    try:
        db._conn.backup(out)
    finally:
        out.close()
    try:
        from iolib.perms import apply_group_rwx
        apply_group_rwx(dst, directory=False)
    except Exception:
        pass
    return dst


def apply(db, p: CleanPlan) -> tuple[Path, int, int]:
    """Back up, then delete the planned rows. Returns (backup, artifacts, projects)."""
    problems = db.quick_check()
    if problems:
        raise RuntimeError("the database is corrupt — rebuild it first (docs/db_recovery.md): "
                           + "; ".join(problems[:3]))
    bk = backup(db)
    n_art = sum(db.delete_artifact_rows(row_id=f.rowid) for f in p.remove)
    n_proj = 0
    with db._maintenance_tx():
        for pid in p.orphan_projects:
            n_proj += db._conn.execute(
                "DELETE FROM projects WHERE project_id IS ? AND NOT EXISTS "
                "(SELECT 1 FROM artifacts WHERE artifacts.project_id IS projects.project_id)",
                (pid,)).rowcount
    return bk, n_art, n_proj
