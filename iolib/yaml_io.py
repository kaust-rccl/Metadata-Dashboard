"""
io/yaml_io.py
-------------
All YAML read and write operations.

Features:
- atomic_write() — writes to a temp file then os.replace() into place.
  Safe on shared filesystems; a crash leaves the original intact.
- file_lock() — context manager wrapping fcntl.flock LOCK_EX.
  Serialises concurrent manifest writes from multiple movers.
- load_yaml() — returns a plain dict; section comments in the file
  are not preserved on load (they are re-added on write via _add_comments).
- classify_artifact_yml() — decides whether a file is one of our artifact
  metadata records. Foreign YAML is rejected on size and a plain-text key
  sniff BEFORE it is handed to the YAML parser, so files that are not ours
  are never parsed.
- atomic_create() — like atomic_write() but refuses to replace an existing
  file. Used by sync, which must never overwrite anything on disk.
- ruamel.yaml is used throughout for CommentedMap support.
"""

from __future__ import annotations

import fcntl
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import logging

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

logger = logging.getLogger(__name__)

# Writer — strict, comment-preserving
_yaml = YAML()
_yaml.default_flow_style = False
_yaml.allow_unicode = True
_yaml.width = 120
_yaml.indent(mapping=2, sequence=4, offset=2)

# Reader — tolerant of duplicate keys in hand-authored legacy files.
# On duplicate, ruamel keeps the FIRST value (verified).
_yaml_reader = YAML()
_yaml_reader.allow_unicode = True
_yaml_reader.allow_duplicate_keys = True


# ── public API ────────────────────────────────────────────────────────────────

def _warn_duplicate_keys(text: str, name: str) -> None:
    """
    Warn on duplicate TOP-LEVEL keys only. Indented lines are skipped: the
    same key legitimately repeats under different parents (verified /
    verified_by under both checksum blocks, fields of each manifest entry),
    and flagging those buried real duplicates under false positives.
    """
    keys_seen: set[str] = set()
    for line in text.splitlines():
        if not line or line[0] in " \t#-":
            continue
        stripped = line.strip()
        if ":" in stripped:
            key = stripped.split(":")[0].strip()
            if key and key in keys_seen:
                logger.warning(
                    "Duplicate key '%s' in %s — first value will be used.", key, name
                )
            keys_seen.add(key)


def load_yaml(path: Path) -> dict:
    """
    Load a YAML file and return a plain dict.
    Returns an empty dict if the file does not exist.
    Tolerates duplicate keys in hand-authored files — first value wins.
    Logs a warning when duplicates are detected.

    No ownership check — only call this on files known to be ours by path
    (manifest.yml, modlog sidecars) or on a file a user explicitly pointed
    at (Import page). Anything discovered by walking the tree goes through
    classify_artifact_yml() instead.
    """
    if not path.exists():
        return {}
    try:
        _warn_duplicate_keys(path.read_text(encoding="utf-8"), path.name)
    except OSError:
        pass
    with path.open("r", encoding="utf-8") as fh:
        data = _yaml_reader.load(fh)
    return dict(data) if data else {}


def atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")

    yaml = YAML()
    yaml.default_flow_style = False
    yaml.allow_unicode = True
    yaml.width = 120
    yaml.indent(mapping=2, sequence=4, offset=2)

    commented = _to_commented_map(data)
    with tmp.open("w", encoding="utf-8") as fh:
        yaml.dump(commented, fh)
    os.replace(tmp, path)


def atomic_create(path: Path, data: dict) -> None:
    """
    Write `data` to a NEW file at `path`. Raises FileExistsError if anything
    already exists there (file, symlink, directory) — including something
    created between the caller's own existence check and this call.

    Implemented as write-to-temp then os.link(): link() fails atomically if
    the destination exists, unlike os.replace() which silently overwrites.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.create.tmp")

    yaml = YAML()
    yaml.default_flow_style = False
    yaml.allow_unicode = True
    yaml.width = 120
    yaml.indent(mapping=2, sequence=4, offset=2)

    try:
        with tmp.open("w", encoding="utf-8") as fh:
            yaml.dump(_to_commented_map(data), fh)
        os.link(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def file_lock(path: Path):
    """
    Exclusive file lock for the duration of the context.
    Uses a dedicated .lock sentinel file so the lock fd is separate
    from the file being written — avoids truncation races.

    Usage:
        with file_lock(manifest_path):
            manifest = load_yaml(manifest_path)
            # ... mutate ...
            atomic_write(manifest_path, manifest)
    """
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = lock_path.open("w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


# ── comment injection ─────────────────────────────────────────────────────────

def _to_commented_map(data: dict) -> CommentedMap:
    """
    Convert a plain dict to a ruamel.yaml CommentedMap and inject
    section header comments so the written YML is human-readable.
    """
    cm = CommentedMap(data)

    # Section comments are injected before the first key of each section.
    # ruamel.yaml stores comments as before-key annotations.
    _comment_before(cm, "artifact_name",        "artifact identity")
    _comment_before(cm, "dm_ticket_number",     "curation — filled by curator, locked on first save")
    _comment_before(cm, "moved_by",             "moving — filled by mover, locked on move confirmation")
    _comment_before(cm, "status",               "status")

    # Nested dicts (checksum blocks, status reason lists) stay as plain dicts
    # — ruamel handles them fine without explicit CommentedMap wrapping.
    return cm


def _comment_before(cm: CommentedMap, key: str, comment: str) -> None:
    """Add a section comment above `key` in a CommentedMap if the key exists."""
    if key in cm:
        cm.yaml_set_comment_before_after_key(
            key, before=f"\n{comment}", indent=0
        )


# ── convenience loaders ───────────────────────────────────────────────────────

# ── artifact YML recognition ──────────────────────────────────────────────────
# Top-level keys every one of our artifact records carries. Matched as plain
# text at column 0 (optionally quoted) before any YAML parsing happens.
_SNIFF_KEYS: frozenset[str] = frozenset({"artifact_name", "artifact_type", "aimcr_reference"})
_SNIFF_RE = re.compile(
    r"""^["']?(artifact_name|artifact_type|aimcr_reference)["']?[ \t]*:""",
    re.MULTILINE,
)


def classify_artifact_yml(path: Path) -> tuple[dict | None, str]:
    """
    Decide whether `path` holds one of our artifact metadata records.

    Returns (data, "") if it does, or (None, reason) if it does not.
    `reason` is human-readable and meant for logs / the sync report.

    Gates, in order — a file failing any gate is never parsed as YAML:
      1. regular file, not a symlink (symlinks could point anywhere)
      2. non-empty and <= MAX_ARTIFACT_YML_BYTES
      3. valid UTF-8 text
      4. plain-text sniff: artifact_name, artifact_type and aimcr_reference
         all appear as top-level keys
    Only then:
      5. YAML parse
      6. models.artifact.is_artifact_yml() structural check

    Location (does the file sit where its content says it belongs?) is NOT
    checked here — that is the sync's job, since pages load by known path.
    """
    from config import MAX_ARTIFACT_YML_BYTES
    from models.artifact import is_artifact_yml

    try:
        st = path.lstat()
    except OSError as exc:
        return None, f"cannot stat: {exc}"
    if stat.S_ISLNK(st.st_mode):
        return None, "symlink — not followed"
    if not stat.S_ISREG(st.st_mode):
        return None, "not a regular file"
    if st.st_size == 0:
        return None, "empty file"
    if st.st_size > MAX_ARTIFACT_YML_BYTES:
        return None, f"too large for a metadata record ({st.st_size} bytes)"

    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return None, "not UTF-8 text"
    except OSError as exc:
        return None, f"cannot read: {exc}"

    found = set(_SNIFF_RE.findall(text))
    if not _SNIFF_KEYS <= found:
        missing = ", ".join(sorted(_SNIFF_KEYS - found))
        return None, f"not a metadata record (no top-level {missing})"

    _warn_duplicate_keys(text, path.name)
    try:
        data = _yaml_reader.load(text)
    except Exception as exc:
        return None, f"looks like a metadata record but is not valid YAML: {exc}"
    if not isinstance(data, dict):
        return None, "not a YAML mapping"
    data = dict(data)

    ok, reason = is_artifact_yml(data)
    if not ok:
        return None, reason
    return data, ""


def is_artifact_yml_file(path: Path) -> tuple[bool, str]:
    """(True, "") if `path` holds one of our artifact records, else (False, reason)."""
    data, reason = classify_artifact_yml(path)
    return data is not None, reason


def load_artifact_yml(path: Path, strict: bool = True):
    """
    Load an ArtifactYML from disk. Returns None if the file is missing,
    empty, or (strict, the default) not one of our metadata records.

    strict=False skips the ownership check and parses whatever is there —
    kept only for callers that have already vetted the file themselves.
    """
    from models.artifact import ArtifactYML

    if strict:
        data, reason = classify_artifact_yml(path)
        if data is None:
            if path.exists():
                logger.info("Not loading %s as an artifact: %s", path, reason)
            return None
    else:
        data = load_yaml(path)
        if not data:
            return None
    try:
        return ArtifactYML.from_dict(data)
    except Exception as exc:
        logger.warning("Artifact YML %s could not be deserialised: %s", path, exc)
        return None


def save_artifact_yml(path: Path, yml) -> None:
    """Atomically write an ArtifactYML to disk."""
    atomic_write(path, yml.to_dict())


def load_modlog(path: Path):
    """Load an ArtifactModLog from disk. Returns None if file missing."""
    from models.modlog import ArtifactModLog
    d = load_yaml(path)
    if not d:
        return None
    return ArtifactModLog.from_dict(d)


def load_manifest(path: Path):
    """Load a ManifestYML from disk. Returns None if file missing."""
    from models.manifest import ManifestYML
    d = load_yaml(path)
    if not d:
        return None
    return ManifestYML.from_dict(d)


def save_manifest(path: Path, manifest) -> None:
    """Atomically write a ManifestYML to disk inside a file lock."""
    with file_lock(path):
        atomic_write(path, manifest.to_dict())
