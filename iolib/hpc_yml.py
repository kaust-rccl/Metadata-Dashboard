"""
iolib/hpc_yml.py
----------------
Read/write helpers for HPC pipeline workspace YMLs and modlog sidecars.

Files live in the workspace directory:
    workspace/artifact.yml
    workspace/artifact.modlog.yml

Uses fresh ruamel.yaml instances per write to avoid EmitterError.
All functions are best-effort — they never raise, they log warnings.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

YML_NAME    = "artifact.yml"
MODLOG_NAME = "artifact.modlog.yml"


# ── path helpers ──────────────────────────────────────────────────────────────

def yml_path(workspace: Path) -> Path:
    return workspace / YML_NAME


def modlog_path(workspace: Path) -> Path:
    return workspace / MODLOG_NAME


# ── internal yaml factory ─────────────────────────────────────────────────────

def _writer():
    from ruamel.yaml import YAML
    y = YAML()
    y.default_flow_style = False
    y.allow_unicode = True
    y.width = 120
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _reader():
    from ruamel.yaml import YAML
    y = YAML()
    y.allow_unicode = True
    y.allow_duplicate_keys = True
    return y


# ── atomic write ──────────────────────────────────────────────────────────────

def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    y = _writer()
    with tmp.open("w", encoding="utf-8") as fh:
        y.dump(data, fh)
    tmp.replace(path)


# ── YML ───────────────────────────────────────────────────────────────────────

def save_yml(workspace: Path, data: dict) -> None:
    """Write artifact.yml to workspace. Never raises."""
    try:
        _atomic_write(yml_path(workspace), data)
    except Exception as exc:
        logger.warning("HPC YML write failed at %s: %s", workspace, exc)


def load_yml(workspace: Path) -> dict:
    """Load artifact.yml from workspace. Returns {} on any error."""
    try:
        path = yml_path(workspace)
        if not path.exists():
            return {}
        y = _reader()
        with path.open("r", encoding="utf-8") as fh:
            data = y.load(fh)
        return dict(data) if data else {}
    except Exception as exc:
        logger.warning("HPC YML load failed at %s: %s", workspace, exc)
        return {}


def update_yml(workspace: Path, actor: str, **kwargs) -> None:
    """
    Load, apply kwargs, update last_modified, save.
    Never raises.
    """
    data = load_yml(workspace)
    if not data:
        return
    data.update(kwargs)
    data["last_modified"] = datetime.now(tz=timezone.utc).isoformat()
    save_yml(workspace, data)


# ── modlog ────────────────────────────────────────────────────────────────────

def append_modlog(
    workspace: Path,
    actor: str,
    action: str,
    note: str = "",
) -> None:
    """Append one entry to artifact.modlog.yml. Never raises."""
    try:
        path    = modlog_path(workspace)
        entries = []
        if path.exists():
            y = _reader()
            with path.open("r", encoding="utf-8") as fh:
                existing = y.load(fh)
            if isinstance(existing, dict):
                entries = list(existing.get("entries", []))
        entries.append({
            "actor":     actor,
            "action":    action,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "note":      note,
        })
        _atomic_write(path, {"entries": entries})
    except Exception as exc:
        logger.warning("HPC modlog append failed at %s: %s", workspace, exc)


def load_modlog(workspace: Path) -> list[dict]:
    """Load modlog entries. Returns [] on any error."""
    try:
        path = modlog_path(workspace)
        if not path.exists():
            return []
        y = _reader()
        with path.open("r", encoding="utf-8") as fh:
            data = y.load(fh)
        return list((data or {}).get("entries", []))
    except Exception as exc:
        logger.warning("HPC modlog load failed at %s: %s", workspace, exc)
        return []