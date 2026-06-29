"""
components/hpc_slurm.py
-----------------------
Slurm script generators and workspace filesystem helpers
for the HPC pipeline.

No Streamlit imports — pure I/O, fully testable.
"""

from __future__ import annotations

import re
from pathlib import Path

from config import STAGING_BASE


# ── workspace helpers ─────────────────────────────────────────────────────────

def workspace_dir(ticket: str, name: str) -> Path:
    """
    STAGING_BASE/workspace/<ticket>_<name>/
    Sanitised to filesystem-safe characters.
    """
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", f"{ticket}_{name}")
    d = STAGING_BASE / "workspace" / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir(workspace: Path) -> Path:
    d = workspace / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_script(workspace: Path, filename: str, content: str) -> Path:
    """Write a script to the workspace and make it executable."""
    path = workspace / filename
    path.write_text(content, encoding="utf-8")
    path.chmod(0o750)
    return path


def scripts_on_disk(workspace: Path) -> dict[str, bool]:
    """
    Return which scripts already exist in the workspace.
    Used to detect sync warnings when editing a record.
    """
    return {
        "premove":  (workspace / "generate_checksum.slurm").exists(),
        "postmove": (workspace / "verify_checksum.slurm").exists(),
    }


# ── checksum path logic ───────────────────────────────────────────────────────

def checksum_path(data_path: str) -> str:
    """
    If data_path is a directory → checksum lives inside it.
    If data_path is a file      → checksum sits next to it.
    """
    p = Path(data_path.strip())
    if p.suffix:
        return str(p.parent / "checksum.sha256")
    return str(p / "checksum.sha256")


def find_command(data_path: str) -> str:
    """
    Returns the find/sha256sum shell fragment for a file or directory.
    Excludes checksum.sha256 and artifact.yml from the checksum.
    """
    p = Path(data_path.strip())
    if p.suffix:
        return f'sha256sum "{p.name}"'
    return (
        'find . -type f -print0 \\\n'
        '  | sort -z \\\n'
        '  | grep -zv "checksum.sha256" \\\n'
        '  | grep -zv "artifact.yml" \\\n'
        '  | xargs -0 sha256sum'
    )


# ── script generators ─────────────────────────────────────────────────────────

def premove_script(ticket: str, name: str, src: str, workspace: Path) -> str:
    """Generate the pre-move checksum Slurm script."""
    ld  = logs_dir(workspace)
    chk = checksum_path(src)
    src_p   = Path(src.strip())
    cd_dir  = str(src_p.parent) if src_p.suffix else src
    find_cmd = find_command(src)

    return f"""#!/bin/bash
#SBATCH --job-name=checksum-generate
#SBATCH --partition=shared
#SBATCH --time=04:00:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --output={ld}/checksum_generate_%j.log
#SBATCH --dependency=none

# DART — pre-move checksum script
# Ticket  : {ticket}
# Artifact: {name}
# Workspace: {workspace}
# Submission is the operator's responsibility

SOURCE="{src}"
CHECKSUM_FILE="{chk}"

echo "Generating checksum"
echo "Source   : ${{SOURCE}}"
echo "Checksum : ${{CHECKSUM_FILE}}"
echo "Started  : $(date)"
echo ""

cd "{cd_dir}" || {{ echo "ERROR: cannot cd to {cd_dir}"; exit 1; }}

{find_cmd} \\
  > "${{CHECKSUM_FILE}}"

echo ""
echo "Checksum written to : ${{CHECKSUM_FILE}}"
echo "File count          : $(wc -l < "${{CHECKSUM_FILE}}")"
echo "Completed           : $(date)"
"""


def postmove_script(ticket: str, name: str, dst: str, workspace: Path) -> str:
    """Generate the post-move verification Slurm script."""
    ld          = logs_dir(workspace)
    chk         = checksum_path(dst)
    dst_p       = Path(dst.strip())
    cd_dir      = str(dst_p.parent) if dst_p.suffix else dst
    result_file = str(workspace / "verify_result.txt")

    return f"""#!/bin/bash
#SBATCH --job-name=checksum-verify
#SBATCH --partition=shared
#SBATCH --time=04:00:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --output={ld}/checksum_verify_%j.log
#SBATCH --dependency=none

# DART — post-move checksum verification script
# Ticket  : {ticket}
# Artifact: {name}
# Workspace: {workspace}
# Result will be written to: {result_file}
# Submission is the operator's responsibility

DEST="{dst}"
CHECKSUM_FILE="{chk}"
RESULT_FILE="{result_file}"

echo "Verifying checksum"
echo "Destination: ${{DEST}}"
echo "Checksum   : ${{CHECKSUM_FILE}}"
echo "Result     : ${{RESULT_FILE}}"
echo "Started    : $(date)"
echo ""

cd "{cd_dir}" || {{ echo "ERROR: cannot cd to {cd_dir}"; exit 1; }}

sha256sum -c "${{CHECKSUM_FILE}}" > "${{RESULT_FILE}}" 2>&1
EXIT_CODE=$?

TOTAL=$(wc -l < "${{CHECKSUM_FILE}}" | tr -d '[:space:]')
FAILED=$(grep -c "FAILED" "${{RESULT_FILE}}" 2>/dev/null | tr -d '[:space:]' || echo 0)
PASSED=$((TOTAL - FAILED))

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Total files : ${{TOTAL}}"
echo "  Passed      : ${{PASSED}}"
echo "  Failed      : ${{FAILED}}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ "${{FAILED}}" -gt 0 ]; then
    echo "ANOMALIES DETECTED — review ${{RESULT_FILE}}"
    grep "FAILED" "${{RESULT_FILE}}"
    exit 1
fi

echo "All files verified OK"
echo "Completed: $(date)"
"""