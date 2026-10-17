"""
iolib/checksum_jobs.py
----------------------
Portal-submitted checksum verification (CPUP) using the resumable
generic_verify.slurm script.

Layout, one directory per artifact (a proposal can hold many):

    PROJECTS_BASE/<6####>/verify_checksum/<datasets|models|software>/<name>/
        generic_verify.slurm   copy of VERIFY_SCRIPT taken at submission
        job.yml                our record: checksum file, data dir, submissions
        logs/<jobname>_<id>.out   written by Slurm (one per job, incl. resubmits)
        verify_state/<manifest>_<hash>/   the script's own resumable state

Everything is group k03 with group rwx: directories are created setgid
(iolib.perms) and sbatch runs under umask 002, which Slurm applies to the
job — so files the job writes, and jobs it resubmits, stay group-writable.

Monitoring reads only files on disk (the script's state + logs), so it
survives app restarts, works inside the container where Slurm commands are
missing, and follows AUTO_RESUBMIT chains whose job IDs the portal never saw.
squeue / sacct, when available, only add the live queued/running state.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import (
    CHECKSUM_CANDIDATE_NAMES, CHECKSUM_SUFFIXES, VERIFY_SCRIPT, VERIFY_UMASK,
    verify_job_dir,
)
from iolib.perms import apply_group_rwx, make_dirs
from iolib.yaml_io import atomic_write, file_lock, load_yaml

logger = logging.getLogger(__name__)

JOB_FILE = "job.yml"
JOB_KIND = "dart-checksum-job"
_SUBMITTED_RE = re.compile(r"Submitted batch job (\d+)")
_LOG_ID_RE = re.compile(r"_(\d+)\.out$")
_LINE_RE = re.compile(r"^[a-fA-F0-9]{32,128} [ *].+")   # same as the script


# ── state ─────────────────────────────────────────────────────────────────────

@dataclass
class JobStatus:
    """What the portal knows about an artifact's checksum job."""
    state: str = "none"          # none | prepared | queued | running | running? |
                                 # ok | failed | incomplete | error
    job_dir: Optional[Path] = None
    checksum_file: str = ""
    data_dir: str = ""
    submitted_by: str = ""
    job_ids: list[str] = field(default_factory=list)
    live: dict[str, str] = field(default_factory=dict)   # job id → squeue state
    total: int = 0
    ok: int = 0
    mismatch: int = 0
    missing: int = 0
    remaining: int = 0
    failed_sample: list[str] = field(default_factory=list)
    result_line: str = ""
    error_lines: list[str] = field(default_factory=list)
    log_path: Optional[Path] = None
    slurm: bool = False

    @property
    def done(self) -> int:
        return self.ok + self.mismatch + self.missing

    @property
    def is_live(self) -> bool:
        return self.state in ("queued", "running", "running?")

    @property
    def label(self) -> str:
        return {
            "none":       "No checksum job yet",
            "prepared":   "Prepared — waiting for the job to be submitted",
            "queued":     "Queued",
            "running":    "Running",
            "running?":   "Probably running (Slurm not visible from here)",
            "ok":         "All files verified OK",
            "failed":     "Finished with failures",
            "incomplete": "Stopped before finishing — resubmit to continue",
            "error":      "Job could not run — see the log",
        }.get(self.state, self.state)


# ── helpers ───────────────────────────────────────────────────────────────────

def job_dir_for(yml) -> Path:
    return verify_job_dir(yml.aimcr_reference, yml.artifact_type, yml.artifact_name)


def slurm_available() -> bool:
    return shutil.which("sbatch") is not None


def _slurm_env(extra: dict[str, str]) -> dict[str, str]:
    """Submission environment: the app's, minus SLURM_*/SBATCH_* (the app may
    itself run inside a Slurm job, whose settings must not leak), plus extra."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SLURM_", "SBATCH_", "SALLOC_"))}
    env.update(extra)
    return env


def _state_dir(job_dir: Path, checksum_file: str, data_dir: str) -> Path:
    """Exactly the directory generic_verify.slurm computes for its state."""
    cf = os.path.realpath(checksum_file)
    cdir = os.path.dirname(cf)
    dd = os.path.realpath(data_dir) if data_dir else cdir
    key = cf if dd == cdir else f"{cf}|{dd}"
    digest = hashlib.md5(key.encode()).hexdigest()[:8]
    return job_dir / "verify_state" / f"{os.path.basename(cf)}_{digest}"


def load_job(job_dir: Path) -> Optional[dict]:
    data = load_yaml(job_dir / JOB_FILE) if (job_dir / JOB_FILE).is_file() else {}
    return data if data.get("kind") == JOB_KIND else None


# ── suggestion ────────────────────────────────────────────────────────────────

def suggest_checksum_files(source_path: str, current: str = "", limit: int = 8,
                           max_entries: int = 3000) -> list[str]:
    """
    Candidate checksum files, best first: the record's current value
    (resolved against source_path if relative), well-known names at the top
    of source_path, then *.sha256 / *.md5 / ... up to two levels down.
    The walk stops after `max_entries` directory entries so a huge dataset
    on Lustre is never crawled.
    """
    out: list[str] = []

    def add(p: Path) -> None:
        s = str(p)
        if s not in out and p.is_file():
            out.append(s)

    base = Path(source_path.strip()) if source_path.strip() else None
    if current.strip():
        c = Path(current.strip())
        add(c if c.is_absolute() or base is None else base / c)
    if base is None or not base.is_dir():
        return out[:limit]

    for name in CHECKSUM_CANDIDATE_NAMES:
        add(base / name)

    seen = 0
    level = [base]
    for _depth in range(3):                   # base, base/*, base/*/*
        nxt: list[Path] = []
        for d in level:
            try:
                entries = sorted(os.scandir(d), key=lambda e: e.name)
            except OSError:
                continue
            for e in entries:
                seen += 1
                if seen > max_entries:
                    return out[:limit]
                try:
                    if e.is_file(follow_symlinks=False) and e.name.endswith(CHECKSUM_SUFFIXES):
                        add(Path(e.path))
                    elif e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                        nxt.append(Path(e.path))
                except OSError:
                    continue
        level = nxt
    return out[:limit]


def check_checksum_file(path: str) -> tuple[bool, str]:
    """Quick sanity check of a manifest before submitting: (ok, message)."""
    p = Path(path)
    if not p.is_file():
        return False, f"{path} does not exist."
    try:
        with p.open("r", errors="replace") as fh:
            lines = [ln.rstrip("\r\n") for _, ln in zip(range(5000), fh)]
    except OSError as exc:
        return False, f"cannot read {path}: {exc}"
    good = sum(1 for ln in lines if _LINE_RE.match(ln))
    nonblank = sum(1 for ln in lines if ln.strip())
    if good == 0:
        return False, (f"{p.name} has no line like '<hex-digest>  <file>' — corrupt, "
                       "empty, or an HTML error page?")
    msg = f"{good} checksum line(s) found"
    if good < nonblank:
        msg += f"; {nonblank - good} malformed line(s) will be skipped"
    return True, msg


# ── prepare / submit / cancel ─────────────────────────────────────────────────

def prepare_job(yml, checksum_file: str, data_dir: str, actor: str) -> tuple[Path, list[str]]:
    """
    Create the job directory (group k03, setgid), copy the verify script,
    write job.yml. Returns (job_dir, argv) where argv is the sbatch command.
    Raises FileNotFoundError if VERIFY_SCRIPT is missing.
    """
    if not VERIFY_SCRIPT.is_file():
        raise FileNotFoundError(f"verify script not found: {VERIFY_SCRIPT}")
    jd = make_dirs(job_dir_for(yml))
    make_dirs(jd / "logs")                       # Slurm opens logs/ before the job runs
    make_dirs(jd / "verify_state")

    script = jd / "generic_verify.slurm"
    tmp = jd / ".generic_verify.slurm.tmp"
    shutil.copyfile(VERIFY_SCRIPT, tmp)
    apply_group_rwx(tmp, directory=False)
    os.replace(tmp, script)

    with file_lock(jd / JOB_FILE):
        job = load_job(jd) or {
            "kind": JOB_KIND, "version": 1,
            "artifact": f"{yml.aimcr_reference}/{yml.artifact_type.value}/{yml.artifact_name}",
            "submissions": [],
        }
        job["checksum_file"] = os.path.realpath(checksum_file)
        job["data_dir"] = os.path.realpath(data_dir) if data_dir.strip() else ""
        job["prepared_by"] = actor
        job["prepared_at"] = datetime.now(tz=timezone.utc).isoformat()
        atomic_write(jd / JOB_FILE, job)

    argv = ["sbatch", str(script), job["checksum_file"]]
    if job["data_dir"]:
        argv.append(job["data_dir"])
    return jd, argv


def manual_command(job_dir: Path, argv: list[str], env: dict[str, str]) -> str:
    """The shell command a user can paste where sbatch is available."""
    import shlex
    envs = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    return f"cd {shlex.quote(str(job_dir))} && umask 002 && {envs} {' '.join(shlex.quote(a) for a in argv)}"


def submit_env(job_dir: Path, auto_resubmit: bool, retry_failed: bool) -> dict[str, str]:
    return {
        "STATE_ROOT": str(job_dir / "verify_state"),
        "AUTO_RESUBMIT": "1" if auto_resubmit else "0",
        "RETRY_FAILED": "1" if retry_failed else "0",
    }


def submit_job(yml, checksum_file: str, data_dir: str, actor: str,
               auto_resubmit: bool = True, retry_failed: bool = False,
               force: bool = False) -> tuple[bool, str]:
    """
    Prepare and submit. Returns (ok, message). Refuses while a job for this
    artifact is still queued/running unless force=True. Where sbatch is not
    available, returns ok=False with the exact command to run by hand
    (job.yml is still written, so the monitor picks the job up from its logs).
    """
    current = job_status(yml)
    if current.is_live and not force:
        return False, (f"A checksum job for this artifact is already {current.state} "
                       f"(job {', '.join(current.live) or ', '.join(current.job_ids[-1:])}).")
    try:
        jd, argv = prepare_job(yml, checksum_file, data_dir, actor)
    except (OSError, FileNotFoundError) as exc:
        return False, f"Could not prepare the job directory: {exc}"

    env_extra = submit_env(jd, auto_resubmit, retry_failed)
    if not slurm_available():
        return False, ("Slurm commands are not available where the dashboard runs "
                       "(for example inside the container). Run this on a login node; "
                       "the dashboard will follow the job from its logs:\n\n"
                       + manual_command(jd, argv, env_extra))

    old_umask = os.umask(VERIFY_UMASK)
    try:
        proc = subprocess.run(argv, cwd=jd, env=_slurm_env(env_extra),
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"sbatch failed: {exc}"
    finally:
        os.umask(old_umask)

    m = _SUBMITTED_RE.search(proc.stdout or "")
    if proc.returncode != 0 or not m:
        return False, f"sbatch failed: {(proc.stderr or proc.stdout).strip()}"
    job_id = m.group(1)

    with file_lock(jd / JOB_FILE):
        job = load_job(jd) or {}
        job.setdefault("submissions", []).append({
            "job_id": job_id,
            "submitted_by": actor,
            "submitted_at": datetime.now(tz=timezone.utc).isoformat(),
            "auto_resubmit": auto_resubmit,
            "retry_failed": retry_failed,
        })
        atomic_write(jd / JOB_FILE, job)
    return True, f"Submitted checksum job {job_id}."


def cancel_job(yml) -> tuple[bool, str]:
    st = job_status(yml)
    ids = [j for j, s in st.live.items() if s in ("PENDING", "RUNNING", "CONFIGURING")]
    if not ids:
        return False, "No queued or running job to cancel."
    if not shutil.which("scancel"):
        return False, f"scancel not available here. Run: scancel {' '.join(ids)}"
    proc = subprocess.run(["scancel", *ids], capture_output=True, text=True,
                          env=_slurm_env({}), timeout=30)
    if proc.returncode != 0:
        return False, f"scancel failed: {proc.stderr.strip()}"
    return True, f"Cancelled job(s) {', '.join(ids)}. Progress so far is kept; resubmit to continue."


# ── monitoring ────────────────────────────────────────────────────────────────

def _read_lines(p: Path) -> list[str]:
    try:
        return p.read_text(errors="replace").splitlines()
    except OSError:
        return []


def _squeue(ids: list[str]) -> dict[str, str]:
    if not ids or not shutil.which("squeue"):
        return {}
    try:
        proc = subprocess.run(["squeue", "-h", "-o", "%i|%T", "--jobs", ",".join(ids)],
                              capture_output=True, text=True, env=_slurm_env({}), timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out = {}
    for line in (proc.stdout or "").splitlines():
        if "|" in line:
            jid, state = line.split("|", 1)
            out[jid.strip()] = state.strip()
    return out


def job_status(yml) -> JobStatus:
    """Full status from disk (+ squeue if available). Never raises."""
    jd = job_dir_for(yml)
    st = JobStatus(job_dir=jd, slurm=slurm_available())
    try:
        job = load_job(jd)
    except Exception as exc:
        st.state, st.error_lines = "error", [f"job.yml unreadable: {exc}"]
        return st
    if job is None:
        return st

    st.checksum_file = job.get("checksum_file", "")
    st.data_dir = job.get("data_dir", "")
    subs = job.get("submissions") or []
    st.submitted_by = subs[-1].get("submitted_by", "") if subs else job.get("prepared_by", "")

    # job ids: what we submitted + whatever appears in logs (auto-resubmits,
    # manual submissions)
    logs = sorted((jd / "logs").glob("*.out"),
                  key=lambda p: int(_LOG_ID_RE.search(p.name).group(1)) if _LOG_ID_RE.search(p.name) else 0)
    ids = [str(s.get("job_id")) for s in subs if s.get("job_id")]
    for p in logs:
        m = _LOG_ID_RE.search(p.name)
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
        # an AUTO_RESUBMIT follow-up is pending before it has a log of its own;
        # its id is in the previous log ("Submitted batch job N")
        for sm in _SUBMITTED_RE.finditer("\n".join(_read_lines(p)[-20:])):
            if sm.group(1) not in ids:
                ids.append(sm.group(1))
    st.job_ids = ids
    st.log_path = logs[-1] if logs else None

    # progress from the script's state
    if st.checksum_file:
        sd = _state_dir(jd, st.checksum_file, st.data_dir)
        valid = _read_lines(sd / "valid.manifest")
        st.total = len(valid)
        last: dict[str, str] = {}
        for line in _read_lines(sd / "results.tsv"):
            if "\t" in line:
                status, name = line.split("\t", 1)
                last[name] = status
        st.ok = sum(1 for v in last.values() if v == "OK")
        st.mismatch = sum(1 for v in last.values() if v == "MISMATCH")
        st.missing = sum(1 for v in last.values() if v == "MISSING")
        st.failed_sample = [f"{v}\t{n}" for n, v in last.items() if v != "OK"][:20]
        names = []
        for line in valid:
            rest = line.split(" ", 1)[1] if " " in line else ""
            names.append(rest[1:] if rest[:1] in (" ", "*") else rest)
        st.remaining = sum(1 for n in names if n not in last)

    # outcome from the newest log
    if st.log_path:
        lines = _read_lines(st.log_path)
        results = [ln for ln in lines if ln.startswith("RESULT:")]
        st.result_line = results[-1] if results else ""
        st.error_lines = [ln for ln in lines if ln.startswith("ERROR:")][-5:]

    st.live = _squeue(ids) if st.slurm else {}
    live_states = set(st.live.values())

    if not ids and not logs:
        st.state = "prepared"
    elif live_states & {"RUNNING", "COMPLETING", "CONFIGURING"}:
        st.state = "running"
    elif "PENDING" in live_states:
        st.state = "queued"
    elif st.total and st.remaining == 0:
        st.state = "ok" if (st.mismatch + st.missing) == 0 else "failed"
    elif st.error_lines and not st.total:
        st.state = "error"
    elif not st.slurm and not st.result_line and st.log_path and \
            (datetime.now().timestamp() - st.log_path.stat().st_mtime) < 900:
        st.state = "running?"
    else:
        st.state = "incomplete"
    return st


# ── auto-apply ────────────────────────────────────────────────────────────────

def auto_apply(yml, db, status: Optional[JobStatus] = None) -> Optional[str]:
    """
    If the job verified every file OK and the record's CPUP checksum box is
    not ticked, tick it — verified_by is whoever submitted the job, and the
    modlog note records the job and counts. Returns a message, or None if
    nothing was done.
    """
    st = status or job_status(yml)
    if st.state != "ok" or yml.checksum_verified_cpup.verified:
        return None
    from iolib.services import CurationUpdateService
    from models.artifact import ChecksumVerification
    who = st.submitted_by or "checksum-job"
    res = CurationUpdateService().execute(
        yml, f"{who} (checksum job {st.job_ids[-1] if st.job_ids else '?'})",
        {"checksum_verified_cpup": ChecksumVerification(
            verified=True, verified_by=who, verified_date=datetime.now(tz=timezone.utc))},
        db,
    )
    if res.success:
        return (f"CPUP checksum marked verified: {st.ok}/{st.total} files OK "
                f"(job {', '.join(st.job_ids[-1:])}).")
    logger.warning("auto-apply failed for %s: %s", yml.artifact_name, res.errors)
    return None


def refresh_pending(db) -> list[str]:
    """
    Startup pass: for every preparing/staged record with a checksum job and
    an unticked CPUP box, apply a completed-OK result. Returns messages.
    """
    from config import Status
    from iolib.yaml_io import load_artifact_yml
    msgs: list[str] = []
    for status in (Status.PREPARING.value, Status.STAGED.value):
        try:
            rows = db.list_artifacts(status=status)
        except Exception:
            continue
        for r in rows:
            try:
                yml = load_artifact_yml(Path(r["yml_path"]))
                if yml is None or yml.checksum_verified_cpup.verified:
                    continue
                if not (job_dir_for(yml) / JOB_FILE).is_file():
                    continue
                m = auto_apply(yml, db)
                if m:
                    msgs.append(f"{yml.artifact_name}: {m}")
            except Exception as exc:
                logger.warning("checksum refresh failed for %s: %s", r["yml_path"], exc)
    return msgs
