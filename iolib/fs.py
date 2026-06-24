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
    Return a list of artifact .yml files found under projects_base.

    Layout expected:
        projects_base / 6#### / datasets|models|software / artifact.yml

    Skips:
      - .modlog.yml sidecars
      - manifest.yml files
      - anything inside Metadata_Dashboard/
    """
    if not projects_base.exists():
        return []
    return sorted(
        p for p in projects_base.rglob("*.yml")
        if not p.name.endswith(".modlog.yml")
        and p.name != "manifest.yml"
        and "Metadata_Dashboard" not in p.parts
    )


def ensure_artifact_dir(aimcr_reference: str, artifact_type, projects_base: Path) -> Path:
    """
    Create and return the type-bucketed artifact directory.
    e.g. PROJECTS_BASE / 612345 / datasets/
    """
    from config import ARTIFACT_TYPE_DIR
    subdir = ARTIFACT_TYPE_DIR[artifact_type]
    artifact_dir = projects_base / aimcr_reference / subdir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir

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