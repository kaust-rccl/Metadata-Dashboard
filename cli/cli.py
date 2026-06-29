#!/usr/bin/env python3
"""
cli.py — Metadata Dashboard CLI
────────────────────────────────
HPC-AI pipeline commands (operate on AIMCR artifacts):
  dashboard list [--aimcr --status --type --json]
  dashboard inspect <aimcr> <name> [--json]
  dashboard move <aimcr> <name> [--verified-by --notes]
  dashboard block <aimcr> <name> <reason>
  dashboard reinstate <aimcr> <name> <reason>
  dashboard edit <aimcr> <name> field=value ...

HPC pipeline commands (operate on ticket-driven transfers):
  dashboard hpc list [--status --ticket --json]
  dashboard hpc inspect <id> [--json]
  dashboard hpc new --ticket RT-... --name NAME --src PATH --dst PATH --requester USER
                    [--type dataset] [--size GB] [--notes TEXT]
  dashboard hpc premove <id>
  dashboard hpc postmove <id>
  dashboard hpc verify <id> [--result /path/to/verify_result.txt]
  dashboard hpc edit <id> field=value ...
  dashboard hpc block <id> <reason>
  dashboard hpc reinstate <id> <reason>
  dashboard hpc status <id> <status>
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import (
    ArtifactType, Role, Status,
    artifact_yml_path, modlog_path,
    DB_PATH, PROJECTS_BASE,
    TransferStatus, TRANSFERS_DB_PATH,
)
from iolib.database import Database
from iolib.services import (
    BlockService, MoveService, MovingUpdateService,
    ReinstateService, ServiceResult,
)
from iolib.yaml_io import load_artifact_yml, load_modlog
from iolib.transfers_db import TransfersDatabase
from iolib.hpc_transfer_service import HpcTransferService
from models.artifact import ChecksumVerification
from components.hpc_slurm import (
    workspace_dir, premove_script, postmove_script,
    write_script, scripts_on_disk,
)


# ── colours ───────────────────────────────────────────────────────────────────

class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    GREEN  = "\033[32m"
    PURPLE = "\033[35m"
    YELLOW = "\033[33m"
    RED    = "\033[31m"
    CYAN   = "\033[36m"
    WHITE  = "\033[97m"

def _no_color() -> bool:
    import os
    return not sys.stdout.isatty() or os.environ.get("NO_COLOR")

def c(color: str, text: str) -> str:
    if _no_color():
        return text
    return f"{color}{text}{C.RESET}"

def ok(msg: str)   -> None: print(c(C.GREEN,  f"✓ {msg}"))
def err(msg: str)  -> None: print(c(C.RED,    f"✗ {msg}"), file=sys.stderr)
def warn(msg: str) -> None: print(c(C.YELLOW, f"! {msg}"))
def dim(msg: str)  -> None: print(c(C.DIM,    msg))
def bold(msg: str) -> None: print(c(C.BOLD,   msg))


# ── shared helpers ────────────────────────────────────────────────────────────

def _actor() -> str:
    return getpass.getuser()

def _db() -> Database:
    return Database(DB_PATH)

def _tdb() -> TransfersDatabase:
    return TransfersDatabase(TRANSFERS_DB_PATH)

def _svc() -> HpcTransferService:
    return HpcTransferService(_tdb())

def _resolve_yml(aimcr: str, artifact_name: str):
    for atype in ArtifactType:
        yp = artifact_yml_path(aimcr, artifact_name, atype)
        if yp.exists():
            yml = load_artifact_yml(yp)
            if yml:
                return yml, yp
    err(f"Artifact '{artifact_name}' not found under AIMCR reference '{aimcr}'.")
    sys.exit(1)

def _status_color(status: Status) -> str:
    colors = {
        Status.STAGED:   C.GREEN,
        Status.MOVED:    C.PURPLE,
        Status.BLOCKED:  C.YELLOW,
        Status.REJECTED: C.RED,
    }
    return c(colors.get(status, C.WHITE), status.value.upper())

def _hpc_status_color(status: str) -> str:
    colors = {
        "pending":     C.DIM,
        "pre-move":    C.CYAN,
        "in-progress": C.YELLOW,
        "verifying":   C.CYAN,
        "completed":   C.GREEN,
        "anomaly":     C.RED,
        "blocked":     C.YELLOW,
    }
    return c(colors.get(status, C.WHITE), status.upper())

def _print_result(result: ServiceResult) -> None:
    if result.success:
        ok(result.message)
    else:
        for e in result.errors:
            err(e)
        sys.exit(1)

def _resolve_transfer(id_str: str):
    """Resolve a transfer by ID string. Exits on error."""
    if not id_str.isdigit():
        err(f"Transfer ID must be a number, got '{id_str}'.")
        sys.exit(1)
    svc = _svc()
    row = svc.get_transfer(int(id_str))
    if row is None:
        err(f"Transfer ID {id_str} not found.")
        sys.exit(1)
    return svc, row

def _parse_result_file(path_str: str):
    """Parse verify_result.txt. Returns (total, passed, failed) or None."""
    try:
        text = Path(path_str).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        err(f"Cannot read result file: {e}")
        return None
    lines  = [l.strip() for l in text.splitlines() if l.strip()]
    passed = sum(1 for l in lines if l.endswith(": OK"))
    failed = sum(1 for l in lines if "FAILED" in l)
    total  = passed + failed
    if total == 0:
        err(f"No OK/FAILED lines found in {path_str}. Is the verify job complete?")
        return None
    return total, passed, failed


# ── HPC-AI commands ───────────────────────────────────────────────────────────

def cmd_list(args: argparse.Namespace) -> None:
    db   = _db()
    rows = db.list_artifacts(
        aimcr_reference = args.aimcr   or None,
        status          = args.status  or None,
        artifact_type   = args.type    or None,
    )
    if not rows:
        dim("No artifacts found.")
        return
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2, default=str))
        return
    print()
    bold(f"  {'ARTIFACT':<40} {'TYPE':<10} {'STATUS':<10} {'AIMCR':<8} {'LAST MODIFIED BY'}")
    dim(f"  {'─'*40} {'─'*10} {'─'*10} {'─'*8} {'─'*20}")
    for r in rows:
        status_str = _status_color(Status(r["status"]))
        modified   = (r["last_modified"] or "")[:16].replace("T", " ")
        print(
            f"  {c(C.WHITE, r['artifact_name']):<40}"
            f" {c(C.DIM, r['artifact_type']):<10}"
            f" {status_str:<10}"
            f" {c(C.DIM, r['project_id'] or ''):<8}"
            f" {c(C.DIM, r['last_modified_by'] or '')} {c(C.DIM, modified)}"
        )
    print()
    dim(f"  {len(rows)} artifact(s)")
    print()


def cmd_inspect(args: argparse.Namespace) -> None:
    yml, yp = _resolve_yml(args.aimcr, args.artifact_name)
    if args.json:
        print(json.dumps(yml.to_dict(), indent=2, default=str))
        return
    print()
    bold(f"  {yml.artifact_name}")
    print(f"  {_status_color(yml.status)}  {c(C.DIM, yml.artifact_type.value)}")
    print()
    def row(label: str, value: str) -> None:
        if value:
            print(f"  {c(C.DIM, f'{label:<24}')} {value}")
    print(c(C.DIM, "  ── identity ─────────────────────────────────────"))
    row("proposal title",   yml.proposal_title)
    row("project id",       yml.project_id)
    row("aimcr reference",  yml.aimcr_reference)
    row("tracking ticket",  yml.tracking_ticket)
    row("pi username",      yml.pi_username)
    print()
    print(c(C.DIM, "  ── curation ─────────────────────────────────────"))
    row("curated by",       yml.curated_by)
    row("date staged",      str(yml.date_staged or ""))
    row("source path",      yml.source_path)
    row("destination path", yml.destination_path)
    row("checksum file",    yml.checksum_filename)
    row("size (GB)",        str(yml.size_gb))
    row("dm ticket",        yml.dm_ticket_number)
    row("cpup verified",
        f"{yml.checksum_verified_cpup.verified} by {yml.checksum_verified_cpup.verified_by}")
    if yml.curation_notes:
        row("curation notes", yml.curation_notes)
    print()
    print(c(C.DIM, "  ── moving ───────────────────────────────────────"))
    row("moved by",         yml.moved_by or c(C.DIM, "—"))
    row("date moved",       str(yml.date_moved or c(C.DIM, "—")))
    row("partition",        yml.partition.value)
    row("gpup verified",
        f"{yml.checksum_verified_gpup.verified} by {yml.checksum_verified_gpup.verified_by}"
        if yml.checksum_verified_gpup.verified_by else c(C.DIM, "—"))
    if yml.moving_notes:
        row("moving notes", yml.moving_notes)
    print()
    print(c(C.DIM, "  ── modification log ─────────────────────────────"))
    ml = load_modlog(modlog_path(yml.aimcr_reference, yml.artifact_name, yml.artifact_type))
    if ml and ml.entries:
        for entry in reversed(ml.entries[-5:]):
            ts     = str(entry.timestamp)[:16].replace("T", " ")
            action = c(C.CYAN, entry.action.value.upper())
            note   = f"  {c(C.DIM, entry.note)}" if entry.note else ""
            print(f"  {c(C.DIM, ts)}  {action}  {entry.actor}{note}")
        if len(ml.entries) > 5:
            dim(f"  ... and {len(ml.entries) - 5} earlier entries")
    else:
        dim("  no log entries")
    print()
    row("yml path", str(yp))
    print()


def cmd_move(args: argparse.Namespace) -> None:
    yml, _ = _resolve_yml(args.aimcr, args.artifact_name)
    actor  = _actor()
    if yml.status == Status.MOVED:
        warn(f"'{args.artifact_name}' is already marked as moved.")
        sys.exit(1)
    if yml.status not in (Status.STAGED, Status.BLOCKED):
        err(f"Cannot move artifact with status '{yml.status.value}'.")
        sys.exit(1)
    verified_by = args.verified_by or actor
    yml.checksum_verified_gpup = ChecksumVerification(verified=True, verified_by=verified_by)
    if args.notes:
        yml.moving_notes = args.notes
    _print_result(MoveService().execute(yml, actor=actor, db=_db()))
    dim(f"  verified by: {verified_by}")
    if args.notes:
        dim(f"  notes: {args.notes}")
    dim("  changes visible in the UI on next refresh")


def cmd_block(args: argparse.Namespace) -> None:
    yml, _ = _resolve_yml(args.aimcr, args.artifact_name)
    result = BlockService().execute(
        yml, actor=_actor(), role=Role.MOVER,
        phase="moving", reason=args.reason, db=_db(),
    )
    _print_result(result)
    if result.success:
        dim(f"  reason: {args.reason}")


def cmd_reinstate(args: argparse.Namespace) -> None:
    yml, _ = _resolve_yml(args.aimcr, args.artifact_name)
    result = ReinstateService().execute(
        yml, actor=_actor(), role=Role.MOVER,
        phase="moving", clearing_reason=args.reason, db=_db(),
    )
    _print_result(result)
    if result.success:
        dim(f"  status is now: staged")


def cmd_edit(args: argparse.Namespace) -> None:
    yml, _ = _resolve_yml(args.aimcr, args.artifact_name)
    ALLOWED = {"moving_notes", "checksum_verified_gpup", "destination_path", "project_id"}
    updated: dict = {}
    for kv in args.fields:
        if "=" not in kv:
            err(f"Invalid format: '{kv}'. Use field=value.")
            sys.exit(1)
        key, _, val = kv.partition("=")
        key = key.strip()
        if key not in ALLOWED:
            err(f"Field '{key}' is not editable via CLI.")
            warn(f"Editable: {', '.join(sorted(ALLOWED))}")
            sys.exit(1)
        updated[key] = val.strip()
    result = MovingUpdateService().execute(yml, actor=_actor(), updated_fields=updated, db=_db())
    _print_result(result)
    if result.success:
        for k, v in updated.items():
            dim(f"  {k}: {v}")
        dim("  changes visible in the UI on next refresh")


# ── HPC pipeline commands ─────────────────────────────────────────────────────

def cmd_hpc_list(args: argparse.Namespace) -> None:
    svc  = _svc()
    rows = svc.list_transfers(
        status = args.status or None,
        ticket = args.ticket or None,
    )
    if not rows:
        dim("No transfers found.")
        return
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2, default=str))
        return
    print()
    bold(f"  {'ID':<6} {'TICKET':<16} {'ARTIFACT':<30} {'TYPE':<10} {'STATUS':<12} {'OPERATOR'}")
    dim(f"  {'─'*6} {'─'*16} {'─'*30} {'─'*10} {'─'*12} {'─'*16}")
    for r in rows:
        print(
            f"  {c(C.DIM, str(r['id'])):<6}"
            f" {c(C.WHITE, r['ticket_number']):<16}"
            f" {c(C.WHITE, r['artifact_name']):<30}"
            f" {c(C.DIM, r['artifact_type']):<10}"
            f" {_hpc_status_color(r['status']):<12}"
            f" {c(C.DIM, r['operator'] or '—')}"
        )
    print()
    dim(f"  {len(rows)} transfer(s)")
    print()


def cmd_hpc_inspect(args: argparse.Namespace) -> None:
    svc, row = _resolve_transfer(args.id)
    if args.json:
        print(json.dumps(dict(row), indent=2, default=str))
        return

    ws = workspace_dir(row["ticket_number"], row["artifact_name"])

    print()
    bold(f"  [{row['id']}] {row['artifact_name']}")
    print(f"  {_hpc_status_color(row['status'])}  {c(C.DIM, row['artifact_type'])}")
    print()

    def r(label: str, value) -> None:
        if value is not None and str(value).strip():
            print(f"  {c(C.DIM, f'{label:<24}')} {value}")

    print(c(C.DIM, "  ── transfer ─────────────────────────────────────"))
    r("ticket",           row["ticket_number"])
    r("requester",        row["requester"])
    r("operator",         row["operator"])
    r("type",             row["artifact_type"])
    r("size (GB)",        row["size_gb"])
    r("notes",            row["notes"])
    print()
    print(c(C.DIM, "  ── paths ────────────────────────────────────────"))
    r("source",           row["source_path"])
    r("destination",      row["destination_path"])
    r("workspace",        str(ws))
    print()
    if row["checksum_total"] is not None:
        print(c(C.DIM, "  ── verification ─────────────────────────────────"))
        r("total files",      row["checksum_total"])
        r("passed",           row["checksum_passed"])
        r("failed",           row["checksum_failed"])
        print()
    print(c(C.DIM, "  ── audit log ────────────────────────────────────"))
    log = svc.get_log(row["id"])
    if log:
        for entry in log[-5:]:
            ts     = entry["timestamp"][:16].replace("T", " ")
            action = c(C.CYAN, entry["action"].upper())
            note   = f"  {c(C.DIM, entry['note'])}" if entry["note"] else ""
            print(f"  {c(C.DIM, ts)}  {action}  {entry['actor']}{note}")
        if len(log) > 5:
            dim(f"  ... and {len(log) - 5} earlier entries")
    else:
        dim("  no log entries")
    print()

    # YML
    from iolib.hpc_yml import load_modlog as hpc_load_modlog
    modlog = hpc_load_modlog(ws)
    if modlog:
        print(c(C.DIM, "  ── yml modlog ───────────────────────────────────"))
        for entry in modlog[-5:]:
            ts     = str(entry.get("timestamp", ""))[:16].replace("T", " ")
            action = c(C.CYAN, str(entry.get("action", "")).upper())
            note   = f"  {c(C.DIM, entry.get('note',''))}" if entry.get("note") else ""
            print(f"  {c(C.DIM, ts)}  {action}  {entry.get('actor','')}{note}")
        print()


def cmd_hpc_new(args: argparse.Namespace) -> None:
    actor = _actor()
    svc   = _svc()
    ws    = workspace_dir(args.ticket, args.name)
    ok_flag, msg, tid = svc.create(
        ticket_number    = args.ticket,
        requester        = args.requester,
        artifact_name    = args.name,
        artifact_type    = args.type,
        source_path      = args.src,
        destination_path = args.dst,
        operator         = actor,
        workspace        = ws,
        size_gb          = args.size,
        notes            = args.notes or "",
    )
    if ok_flag:
        ok(f"Transfer created (ID: {tid})")
        dim(f"  workspace: {ws}")
        dim(f"  run: dashboard hpc premove {tid}")
    else:
        err(f"Failed: {msg}")
        sys.exit(1)


def cmd_hpc_premove(args: argparse.Namespace) -> None:
    svc, row = _resolve_transfer(args.id)
    actor    = _actor()
    ws       = workspace_dir(row["ticket_number"], row["artifact_name"])

    script = premove_script(row["ticket_number"], row["artifact_name"], row["source_path"], ws)
    out    = write_script(ws, "generate_checksum.slurm", script)

    ok_flag, msg = svc.set_premove(row["id"], actor, ws)
    if ok_flag:
        ok(f"Pre-move script written to: {out}")
        print()
        warn("Submission is your responsibility:")
        dim(f"  sbatch {out}")
        print()
        dim(f"  workspace: {ws}")
    else:
        err(f"Script written but status update failed: {msg}")


def cmd_hpc_postmove(args: argparse.Namespace) -> None:
    svc, row = _resolve_transfer(args.id)
    actor    = _actor()
    ws       = workspace_dir(row["ticket_number"], row["artifact_name"])

    script = postmove_script(row["ticket_number"], row["artifact_name"], row["destination_path"], ws)
    out    = write_script(ws, "verify_checksum.slurm", script)

    ok_flag, msg = svc.set_verifying(row["id"], actor, ws)
    result_file = ws / "verify_result.txt"
    if ok_flag:
        ok(f"Post-move script written to: {out}")
        print()
        warn("Submission is your responsibility:")
        dim(f"  sbatch {out}")
        print()
        dim(f"  result will be written to: {result_file}")
        dim(f"  then run: dashboard hpc verify {row['id']}")
    else:
        err(f"Script written but status update failed: {msg}")


def cmd_hpc_verify(args: argparse.Namespace) -> None:
    svc, row = _resolve_transfer(args.id)
    actor    = _actor()
    ws       = workspace_dir(row["ticket_number"], row["artifact_name"])

    result_path = args.result or str(ws / "verify_result.txt")
    counts = _parse_result_file(result_path)
    if counts is None:
        sys.exit(1)

    total, passed, failed = counts
    ok_flag, msg = svc.record_verification(row["id"], total, passed, failed, actor, ws)

    print()
    dim(f"  total  : {total}")
    dim(f"  passed : {c(C.GREEN, str(passed))}")
    dim(f"  failed : {c(C.RED, str(failed)) if failed else c(C.DIM, '0')}")
    print()

    if failed == 0:
        ok(msg)
    else:
        err(msg)
        dim(f"  review: {result_path}")
        sys.exit(1)


def cmd_hpc_edit(args: argparse.Namespace) -> None:
    svc, row = _resolve_transfer(args.id)
    actor    = _actor()
    ws       = workspace_dir(row["ticket_number"], row["artifact_name"])

    ALLOWED = {
        "ticket_number", "requester", "artifact_name",
        "artifact_type", "size_gb", "source_path",
        "destination_path", "notes",
    }

    changed: dict = {}
    for kv in args.fields:
        if "=" not in kv:
            err(f"Invalid format: '{kv}'. Use field=value.")
            sys.exit(1)
        key, _, val = kv.partition("=")
        key = key.strip()
        if key not in ALLOWED:
            err(f"Field '{key}' is not editable.")
            warn(f"Editable fields: {', '.join(sorted(ALLOWED))}")
            sys.exit(1)
        changed[key] = val.strip()

    # sync warning
    from components.hpc_slurm import scripts_on_disk
    on_disk = scripts_on_disk(ws)
    PREMOVE_FIELDS  = {"ticket_number", "artifact_name", "source_path"}
    POSTMOVE_FIELDS = {"ticket_number", "artifact_name", "destination_path"}
    if on_disk["premove"] and changed.keys() & PREMOVE_FIELDS:
        warn(f"generate_checksum.slurm is out of sync — affected: {changed.keys() & PREMOVE_FIELDS}")
        warn("Regenerate with: dashboard hpc premove " + args.id)
    if on_disk["postmove"] and changed.keys() & POSTMOVE_FIELDS:
        warn(f"verify_checksum.slurm is out of sync — affected: {changed.keys() & POSTMOVE_FIELDS}")
        warn("Regenerate with: dashboard hpc postmove " + args.id)

    ok_flag, note = svc.update_fields(row["id"], actor, ws, row, **changed)
    if ok_flag:
        ok(f"Transfer updated.")
        for k, v in changed.items():
            dim(f"  {k}: {v}")
        dim("  changes visible in the UI on next refresh")
    else:
        err(f"Failed: {note}")
        sys.exit(1)


def cmd_hpc_block(args: argparse.Namespace) -> None:
    svc, row = _resolve_transfer(args.id)
    ok_flag, msg = svc.update_status(
        row["id"], TransferStatus.BLOCKED, _actor(), args.reason
    )
    if ok_flag:
        ok(f"Transfer blocked.")
        dim(f"  reason: {args.reason}")
    else:
        err(f"Failed: {msg}")
        sys.exit(1)


def cmd_hpc_reinstate(args: argparse.Namespace) -> None:
    svc, row = _resolve_transfer(args.id)
    ok_flag, msg = svc.update_status(
        row["id"], TransferStatus.PENDING, _actor(), args.reason
    )
    if ok_flag:
        ok("Transfer reinstated to pending.")
        dim(f"  reason: {args.reason}")
    else:
        err(f"Failed: {msg}")
        sys.exit(1)


def cmd_hpc_status(args: argparse.Namespace) -> None:
    svc, row = _resolve_transfer(args.id)
    try:
        new_status = TransferStatus(args.status)
    except ValueError:
        err(f"Invalid status '{args.status}'.")
        warn(f"Valid values: {', '.join(s.value for s in TransferStatus)}")
        sys.exit(1)
    ok_flag, msg = svc.update_status(row["id"], new_status, _actor())
    if ok_flag:
        ok(f"Status → {new_status.value}")
    else:
        err(f"Failed: {msg}")
        sys.exit(1)


# ── argument parser ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dashboard",
        description="DART — Data Artifact Routing Tracker CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
HPC-AI pipeline:
  dashboard list --status staged
  dashboard inspect 65616 Seed-X-PPO-7B
  dashboard move 65616 Seed-X-PPO-7B --verified-by mover1 --notes "ok"
  dashboard block 65616 Seed-X-PPO-7B "quota exceeded"
  dashboard reinstate 65616 Seed-X-PPO-7B "resolved"
  dashboard edit 65616 Seed-X-PPO-7B destination_path="/scratch/project/k00123/models/Seed-X-PPO-7B"

HPC pipeline:
  dashboard hpc list --status pending
  dashboard hpc new --ticket RT-12345 --name my_dataset --src /cpup/path --dst /gpup/path --requester pi_user
  dashboard hpc premove 42
  dashboard hpc postmove 42
  dashboard hpc verify 42
  dashboard hpc edit 42 destination_path="/scratch/project/k00123/datasets/my_dataset"
  dashboard hpc block 42 "quota exceeded"
  dashboard hpc reinstate 42 "resolved"
  dashboard hpc status 42 completed
        """
    )

    sub = p.add_subparsers(dest="command", metavar="command")
    sub.required = True

    # ── HPC-AI commands ───────────────────────────────────────────────────────

    p_list = sub.add_parser("list", help="list HPC-AI artifacts")
    p_list.add_argument("--aimcr",  metavar="REF")
    p_list.add_argument("--status", metavar="STATUS")
    p_list.add_argument("--type",   metavar="TYPE")
    p_list.add_argument("--json",   action="store_true")

    p_inspect = sub.add_parser("inspect", help="show HPC-AI artifact detail")
    p_inspect.add_argument("aimcr",         metavar="AIMCR")
    p_inspect.add_argument("artifact_name", metavar="NAME")
    p_inspect.add_argument("--json",        action="store_true")

    p_move = sub.add_parser("move", help="confirm HPC-AI artifact move to gpup")
    p_move.add_argument("aimcr",          metavar="AIMCR")
    p_move.add_argument("artifact_name",  metavar="NAME")
    p_move.add_argument("--verified-by",  metavar="USER")
    p_move.add_argument("--notes",        metavar="TEXT")

    p_block = sub.add_parser("block", help="block an HPC-AI artifact")
    p_block.add_argument("aimcr",          metavar="AIMCR")
    p_block.add_argument("artifact_name",  metavar="NAME")
    p_block.add_argument("reason",         metavar="REASON")

    p_reinstate = sub.add_parser("reinstate", help="reinstate an HPC-AI artifact")
    p_reinstate.add_argument("aimcr",          metavar="AIMCR")
    p_reinstate.add_argument("artifact_name",  metavar="NAME")
    p_reinstate.add_argument("reason",         metavar="REASON")

    p_edit = sub.add_parser("edit", help="edit HPC-AI moving fields")
    p_edit.add_argument("aimcr",          metavar="AIMCR")
    p_edit.add_argument("artifact_name",  metavar="NAME")
    p_edit.add_argument("fields",         metavar="field=value", nargs="+")

    # ── HPC subcommand ────────────────────────────────────────────────────────

    p_hpc = sub.add_parser("hpc", help="HPC pipeline (ticket-driven transfer) commands")
    hpc_sub = p_hpc.add_subparsers(dest="hpc_command", metavar="hpc_command")
    hpc_sub.required = True

    h_list = hpc_sub.add_parser("list", help="list HPC transfers")
    h_list.add_argument("--status", metavar="STATUS")
    h_list.add_argument("--ticket", metavar="TICKET")
    h_list.add_argument("--json",   action="store_true")

    h_inspect = hpc_sub.add_parser("inspect", help="show HPC transfer detail")
    h_inspect.add_argument("id",     metavar="ID")
    h_inspect.add_argument("--json", action="store_true")

    h_new = hpc_sub.add_parser("new", help="create a new HPC transfer record")
    h_new.add_argument("--ticket",    required=True, metavar="RT-...")
    h_new.add_argument("--name",      required=True, metavar="ARTIFACT_NAME")
    h_new.add_argument("--src",       required=True, metavar="SOURCE_PATH")
    h_new.add_argument("--dst",       required=True, metavar="DEST_PATH")
    h_new.add_argument("--requester", required=True, metavar="USERNAME")
    h_new.add_argument("--type",      default="dataset",
                       choices=["dataset","model","software","other"])
    h_new.add_argument("--size",      type=float, default=None, metavar="GB")
    h_new.add_argument("--notes",     default="", metavar="TEXT")

    h_pre = hpc_sub.add_parser("premove", help="generate pre-move checksum script")
    h_pre.add_argument("id", metavar="ID")

    h_post = hpc_sub.add_parser("postmove", help="generate post-move verify script")
    h_post.add_argument("id", metavar="ID")

    h_verify = hpc_sub.add_parser("verify", help="parse verify_result.txt and complete record")
    h_verify.add_argument("id",       metavar="ID")
    h_verify.add_argument("--result", metavar="PATH",
                          help="path to verify_result.txt (default: workspace/verify_result.txt)")

    h_edit = hpc_sub.add_parser("edit", help="edit HPC transfer fields")
    h_edit.add_argument("id",     metavar="ID")
    h_edit.add_argument("fields", metavar="field=value", nargs="+")

    h_block = hpc_sub.add_parser("block", help="block an HPC transfer")
    h_block.add_argument("id",     metavar="ID")
    h_block.add_argument("reason", metavar="REASON")

    h_reinstate = hpc_sub.add_parser("reinstate", help="reinstate a blocked HPC transfer")
    h_reinstate.add_argument("id",     metavar="ID")
    h_reinstate.add_argument("reason", metavar="REASON")

    h_status = hpc_sub.add_parser("status", help="manually set HPC transfer status")
    h_status.add_argument("id",     metavar="ID")
    h_status.add_argument("status", metavar="STATUS")

    return p


# ── entry point ───────────────────────────────────────────────────────────────

_HPC_AI_DISPATCH = {
    "list":      cmd_list,
    "inspect":   cmd_inspect,
    "move":      cmd_move,
    "block":     cmd_block,
    "reinstate": cmd_reinstate,
    "edit":      cmd_edit,
}

_HPC_DISPATCH = {
    "list":      cmd_hpc_list,
    "inspect":   cmd_hpc_inspect,
    "new":       cmd_hpc_new,
    "premove":   cmd_hpc_premove,
    "postmove":  cmd_hpc_postmove,
    "verify":    cmd_hpc_verify,
    "edit":      cmd_hpc_edit,
    "block":     cmd_hpc_block,
    "reinstate": cmd_hpc_reinstate,
    "status":    cmd_hpc_status,
}


def main() -> None:
    parser = build_parser()
    args   = parser.parse_args()

    if args.command == "hpc":
        _HPC_DISPATCH[args.hpc_command](args)
    else:
        _HPC_AI_DISPATCH[args.command](args)


if __name__ == "__main__":
    main()