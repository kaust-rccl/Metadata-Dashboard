"""
iolib/projects.py
-----------------
Project profile — the fields every artifact of one proposal shares, so they
are typed once and auto-filled afterwards.

Keyed by AIMCR reference (6####). Fields, named as on ArtifactYML:
    proposal_title, project_id (KAUST k####), pi_username

Where a profile is stored (written on every artifact save, see
ManifestYML.absorb_project_fields and iolib.sync.mirror_to_db):
    1. manifest.yml of the proposal          (YML — source of truth)
    2. projects table: proposal_title, pi_username, kaust_project_id

Lookup order, first non-empty value wins per field:
    1. manifest.yml
    2. projects table
    3. the most recently modified artifact YML in the proposal directory
       (so proposals staged before profiles existed still auto-fill)

Only blanks are ever filled — a value the user typed is never replaced.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from config import AIMCR_REFERENCE_PATTERN, ARTIFACT_TYPE_DIR, manifest_path, proposal_dir

logger = logging.getLogger(__name__)

PROFILE_FIELDS: tuple[str, ...] = ("proposal_title", "project_id", "pi_username")

_REF_RE = re.compile(AIMCR_REFERENCE_PATTERN)


def get_project_profile(aimcr_reference: str, db=None) -> dict[str, str]:
    """
    Return {field: value} for the profile fields known for this proposal.
    Missing fields are absent from the dict. Never raises.
    """
    ref = str(aimcr_reference or "").strip()
    if not _REF_RE.fullmatch(ref):
        return {}

    profile: dict[str, str] = {}

    def take(field: str, value) -> None:
        v = str(value or "").strip()
        if v and field not in profile:
            profile[field] = v

    # 1. manifest.yml
    try:
        from iolib.yaml_io import load_manifest
        m = load_manifest(manifest_path(ref))
        if m is not None:
            take("proposal_title", m.proposal_title)
            take("pi_username", m.pi_username)
            take("project_id", m.kaust_project_id)
    except Exception as exc:
        logger.debug("profile: manifest for %s unreadable: %s", ref, exc)

    # 2. projects table
    if db is not None and len(profile) < len(PROFILE_FIELDS):
        try:
            row = db.get_project(ref)
            if row is not None:
                keys = row.keys()
                take("proposal_title", row["proposal_title"] if "proposal_title" in keys else "")
                take("pi_username", row["pi_username"] if "pi_username" in keys else "")
                take("project_id", row["kaust_project_id"] if "kaust_project_id" in keys else "")
        except Exception as exc:
            logger.debug("profile: projects row for %s unreadable: %s", ref, exc)

    # 3. newest artifact YML of the proposal
    if len(profile) < len(PROFILE_FIELDS):
        yml = _newest_artifact(ref)
        if yml is not None:
            for f in PROFILE_FIELDS:
                take(f, getattr(yml, f, ""))

    return profile


def fill_blanks(target: dict, profile: dict[str, str]) -> list[str]:
    """
    Set profile values into `target` (e.g. st.session_state) only where the
    target's value is missing or blank. Returns the fields that were filled.
    """
    filled = []
    for f, v in profile.items():
        if not str(target.get(f) or "").strip():
            target[f] = v
            filled.append(f)
    return filled


def _newest_artifact(ref: str):
    from iolib.yaml_io import load_artifact_yml   # strict: only our records are parsed
    best, best_mtime = None, -1.0
    base: Path = proposal_dir(ref)
    for sub in ARTIFACT_TYPE_DIR.values():
        d = base / sub
        try:
            candidates = [p for p in d.iterdir()
                          if p.suffix == ".yml" and not p.name.endswith(".modlog.yml")]
        except OSError:
            continue
        for p in candidates:
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if mt > best_mtime:
                y = load_artifact_yml(p)
                if y is not None and y.aimcr_reference == ref:
                    best, best_mtime = y, mt
    return best
