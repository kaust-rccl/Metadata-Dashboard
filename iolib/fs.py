"""
io/fs.py
--------
Filesystem helpers.

All functions are pure I/O — no business logic.
Any function that shells out uses subprocess with a timeout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def path_exists(path: str | Path) -> bool:
    """Return True if path exists and is accessible."""
    try:
        return Path(path).exists()
    except (PermissionError, OSError):
        return False


def is_dir(path: str | Path) -> bool:
    return Path(path).is_dir()


def get_size_gb(path: str | Path) -> float:
    """
    Return the disk usage of path in GB using du -sb.
    Returns 0.0 on any error.
    """
    try:
        result = subprocess.run(
            ["du", "-sb", str(path)],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            bytes_used = int(result.stdout.split()[0])
            return round(bytes_used / (1024 ** 3), 3)
    except (subprocess.TimeoutExpired, ValueError, IndexError, OSError):
        pass
    return 0.0


def checksum_file_exists(staging_dir: str | Path, filename: str) -> bool:
    """Return True if checksum file exists inside staging_dir."""
    return (Path(staging_dir) / filename).exists()


def run_checksum_verify(
    checksum_file: str | Path,
    working_dir: str | Path,
) -> tuple[bool, str]:
    """
    Run sha256sum -c <checksum_file> in working_dir.

    Returns:
        (passed: bool, output: str)
        output contains stdout+stderr for display in the UI.
    """
    try:
        result = subprocess.run(
            ["sha256sum", "-c", str(checksum_file)],
            capture_output=True,
            text=True,
            cwd=str(working_dir),
            timeout=120,
        )
        passed = result.returncode == 0
        output = result.stdout + result.stderr
        return passed, output.strip()
    except subprocess.TimeoutExpired:
        return False, "Checksum verification timed out after 120s."
    except OSError as exc:
        return False, f"Failed to run sha256sum: {exc}"


def list_staged_artifacts(projects_base: Path) -> list[Path]:
    """
    Return candidate artifact .yml files under projects_base.

    Only the fixed layout is visited — three levels, never deeper:

        projects_base / <dir> / datasets|models|software / <name>.yml

    This replaces a full rglob(), which on Shaheen3 descended into every
    staged dataset under support_team/ (PROJECTS_BASE == STAGING_BASE) and
    picked up any .yml it found there.

    Skips .modlog.yml sidecars, manifest.yml, dotfiles/temp files, and
    Metadata_Dashboard/. Symlinks ARE returned so the classifier can report
    them as ignored rather than have them vanish silently.

    This is a structural filter only. Whether a returned file actually holds
    one of our records is decided by iolib.yaml_io.classify_artifact_yml().
    """
    from config import ARTIFACT_TYPE_DIR, MANIFEST_NAME, MODLOG_SUFFIX, YML_SUFFIX

    if not projects_base.is_dir():
        return []

    found: list[Path] = []
    try:
        top_level = list(projects_base.iterdir())
    except OSError:
        return []

    for proposal in top_level:
        if proposal.name == "Metadata_Dashboard" or proposal.name.startswith("."):
            continue
        try:
            if not proposal.is_dir() or proposal.is_symlink():
                continue
        except OSError:
            continue
        for subdir in ARTIFACT_TYPE_DIR.values():
            bucket = proposal / subdir
            try:
                if not bucket.is_dir() or bucket.is_symlink():
                    continue
                entries = list(bucket.iterdir())
            except OSError:
                continue
            for p in entries:
                name = p.name
                if (
                    not name.endswith(YML_SUFFIX)
                    or name.endswith(MODLOG_SUFFIX)
                    or name == MANIFEST_NAME
                    or name.startswith(".")
                ):
                    continue
                found.append(p)

    return sorted(found)


def ensure_artifact_dir(aimcr_reference: str, artifact_type, projects_base: Path) -> Path:
    """
    Create and return the type-bucketed artifact directory.
    e.g. PROJECTS_BASE / 612345 / datasets/
    """
    from config import ARTIFACT_TYPE_DIR
    subdir = ARTIFACT_TYPE_DIR[artifact_type]
    from iolib.perms import make_dirs
    return make_dirs(projects_base / aimcr_reference / subdir)   # group k03 rwx + setgid

def extract_artifact_info(source_path: str) -> dict:
    """
    Extract artifact metadata from a source path.
    Returns a dict of field_name → value for any field we can infer.
    On error returns {"_error": "message"}.
    """
    import re
    from pathlib import Path
    from config import ArtifactType, ChecksumType, ChecksumSource, ARTIFACT_TYPE_DIR

    p = Path(source_path)
    if not p.exists():
        return {"_error": f"Path not found: {source_path}"}

    result = {}

    # artifact_name from directory name
    result["artifact_name"] = p.name

    # artifact_type from parent directory name
    type_map = {v: k for k, v in ARTIFACT_TYPE_DIR.items()}
    if p.parent.name in type_map:
        result["artifact_type"] = type_map[p.parent.name].value

    # aimcr_reference from grandparent if it matches 6####
    grandparent = p.parent.parent.name
    if re.match(r"^6\d{4,}$", grandparent):
        result["aimcr_reference"] = grandparent

    # size_gb
    size = get_size_gb(source_path)
    if size > 0:
        result["size_gb"] = size

    # checksum file
    for fname, ctype in [
        ("checksum.sha256", ChecksumType.SHA256),
        ("checksum.md5",    ChecksumType.MD5),
        ("sha256sum.txt",   ChecksumType.SHA256),
        ("md5sum.txt",      ChecksumType.MD5),
    ]:
        if (p / fname).exists():
            result["checksum_filename"] = fname
            result["checksum_type"]     = ctype.value
            result["checksum_source"]   = ChecksumSource.FROM_SOURCE.value
            break

    return result