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
- ruamel.yaml is used throughout for CommentedMap support.
"""

from __future__ import annotations

import fcntl
import os
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
# On duplicate, the last value wins (standard YAML 1.1 behaviour).
_yaml_reader = YAML()
_yaml_reader.allow_unicode = True
_yaml_reader.allow_duplicate_keys = True


# ── public API ────────────────────────────────────────────────────────────────

def load_yaml(path: Path) -> dict:
    """
    Load a YAML file and return a plain dict.
    Returns an empty dict if the file does not exist.
    Tolerates duplicate keys in hand-authored files — last value wins.
    Logs a warning when duplicates are detected.
    """
    if not path.exists():
        return {}
    # Check for duplicate keys before loading so we can warn clearly
    try:
        text = path.read_text(encoding="utf-8")
        keys_seen: set[str] = set()
        for line in text.splitlines():
            stripped = line.strip()
            if ":" in stripped and not stripped.startswith("#") and not stripped.startswith("-"):
                key = stripped.split(":")[0].strip()
                if key and key in keys_seen:
                    logger.warning(
                        "Duplicate key '%s' in %s — last value will be used.",
                        key, path.name
                    )
                keys_seen.add(key)
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

def load_artifact_yml(path: Path):
    """Load an ArtifactYML from disk. Returns None if file missing."""
    from models.artifact import ArtifactYML
    d = load_yaml(path)
    if not d:
        return None
    return ArtifactYML.from_dict(d)


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
