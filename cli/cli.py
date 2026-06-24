#!/usr/bin/env python3
"""
cli.py — Metadata Dashboard CLI for movers
───────────────────────────────────────────
Usage:
  python cli.py list                                           list all staged artifacts
  python cli.py list --status moved                           filter by status
  python cli.py list --json                                   machine-readable output

  python cli.py inspect <aimcr> <artifact_name>              show full artifact detail
  python cli.py inspect 65616 Seed-X-PPO-7B
  python cli.py inspect 65616 Seed-X-PPO-7B --json

  python cli.py move <aimcr> <artifact_name>                 confirm move to gpup
  python cli.py move 65616 Seed-X-PPO-7B
  python cli.py move 65616 Seed-X-PPO-7B --verified-by mover1 --notes "ok"

  python cli.py block <aimcr> <artifact_name> <reason>       block with reason
  python cli.py block 65616 Seed-X-PPO-7B "quota exceeded"

  python cli.py reinstate <aimcr> <artifact_name> <reason>   reinstate from blocked/rejected
  python cli.py reinstate 65616 Seed-X-PPO-7B "quota resolved"

  python cli.py edit <aimcr> <artifact_name> field=value ... edit moving fields
  python cli.py edit 65616 Seed-X-PPO-7B moving_notes="re-verified after transfer"
  pythin cli.py edit 65616 Seed-X-PPO-7B destination_path="/scratch/project/k00123/models/Seed-X-PPO-7B"
  pythin cli.py edit 65616 Seed-X-PPO-7B project_id="k00123"
  
All write operations go through the service layer — YML, modlog, manifest,
and DB are all updated in sync. Changes are visible in the UI on next refresh.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

# ── path setup ────────────────────────────────────────────────────────────────
# resolve the app root relative to this script so the CLI works from anywhere
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import (
    ArtifactType, Role, Status,
    artifact_yml_path, modlog_path,
    DB_PATH, PROJECTS_BASE,
)
from iolib.database import Database
from iolib.services import (
    BlockService, MoveService, MovingUpdateService,
    ReinstateService, ServiceResult,
)
from iolib.yaml_io import load_artifact_yml, load_modlog
from models.artifact import ChecksumVerification


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


# ── helpers ───────────────────────────────────────────────────────────────────

def _actor() -> str:
    return getpass.getuser()


def _db() -> Database:
    return Database(DB_PATH)


def _resolve_yml(aimcr: str, artifact_name: str):
    """
    Find the artifact YML by searching all type subdirs under aimcr_reference.
    Returns (ArtifactYML, yml_path) or exits with an error.
    """
    for atype in ArtifactType:
        yp = artifact_yml_path(aimcr, artifact_name, atype)
        if yp.exists():
            yml = load_artifact_yml(yp)
            if yml:
                return yml, yp
    err(f"Artifact '{artifact_name}' not found under AIMCR reference '{aimcr}'.")
    err(f"Searched: {PROJECTS_BASE / aimcr / '*/'}  ")
    sys.exit(1)


def _status_color(status: Status) -> str:
    colors = {
        Status.STAGED:   C.GREEN,
        Status.MOVED:    C.PURPLE,
        Status.BLOCKED:  C.YELLOW,
        Status.REJECTED: C.RED,
    }
    return c(colors.get(status, C.WHITE), status.value.upper())


def _print_result(result: ServiceResult) -> None:
    if result.success:
        ok(result.message)
    else:
        for e in result.errors:
            err(e)
        sys.exit(1)


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_list(args: argparse.Namespace) -> None:
    """List artifacts, optionally filtered by status."""
    db = _db()
    rows = db.list_artifacts(
        aimcr_reference=args.aimcr or None,
        status=args.status or None,
        artifact_type=args.type or None,
    )

    if not rows:
        dim("No artifacts found.")
        return

    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2, default=str))
        return

    # header
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
            f" {c(C.DIM, r['last_modified_by'] or '')} "
            f"{c(C.DIM, modified)}"
        )
    print()
    dim(f"  {len(rows)} artifact(s)")
    print()


def cmd_inspect(args: argparse.Namespace) -> None:
    """Show full artifact detail."""
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
        f"{yml.checksum_verified_cpup.verified} "
        f"by {yml.checksum_verified_cpup.verified_by}")
    if yml.curation_notes:
        row("curation notes",  yml.curation_notes)
    print()

    print(c(C.DIM, "  ── moving ───────────────────────────────────────"))
    row("moved by",         yml.moved_by or c(C.DIM, "—"))
    row("date moved",       str(yml.date_moved or c(C.DIM, "—")))
    row("partition",        yml.partition.value)
    row("gpup verified",
        f"{yml.checksum_verified_gpup.verified} "
        f"by {yml.checksum_verified_gpup.verified_by}"
        if yml.checksum_verified_gpup.verified_by
        else c(C.DIM, "—"))
    if yml.moving_notes:
        row("moving notes",    yml.moving_notes)
    print()

    print(c(C.DIM, "  ── status reasons ───────────────────────────────"))
    if yml.curation_status_reason:
        print(f"  {c(C.DIM, 'curation:')}")
        for e in yml.curation_status_reason:
            print(f"    {c(C.DIM, str(e.timestamp)[:16])} {e.actor}: {e.reason}")
    if yml.moving_status_reason:
        print(f"  {c(C.DIM, 'moving:')}")
        for e in yml.moving_status_reason:
            print(f"    {c(C.DIM, str(e.timestamp)[:16])} {e.actor}: {e.reason}")
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
    """Confirm artifact move to gpup."""
    yml, _ = _resolve_yml(args.aimcr, args.artifact_name)
    actor  = _actor()

    if yml.status == Status.MOVED:
        warn(f"'{args.artifact_name}' is already marked as moved.")
        warn("Use 'edit' to correct moving fields if needed.")
        sys.exit(1)

    if yml.status not in (Status.STAGED, Status.BLOCKED):
        err(f"Cannot move artifact with status '{yml.status.value}'.")
        sys.exit(1)

    # fill moving fields
    verified_by = args.verified_by or actor
    yml.checksum_verified_gpup = ChecksumVerification(
        verified=True,
        verified_by=verified_by,
    )
    if args.notes:
        yml.moving_notes = args.notes

    db     = _db()
    result = MoveService().execute(yml, actor=actor, db=db)
    _print_result(result)

    if result.success:
        dim(f"  verified by: {verified_by}")
        if args.notes:
            dim(f"  notes: {args.notes}")
        dim(f"  changes visible in the UI on next refresh")


def cmd_block(args: argparse.Namespace) -> None:
    """Block an artifact with a reason."""
    yml, _ = _resolve_yml(args.aimcr, args.artifact_name)
    actor  = _actor()
    db     = _db()

    result = BlockService().execute(
        yml, actor=actor, role=Role.MOVER,
        phase="moving", reason=args.reason, db=db,
    )
    _print_result(result)

    if result.success:
        dim(f"  reason: {args.reason}")
        dim(f"  curator will see this reason in the dashboard")


def cmd_reinstate(args: argparse.Namespace) -> None:
    """Reinstate a blocked or rejected artifact back to staged."""
    yml, _ = _resolve_yml(args.aimcr, args.artifact_name)
    actor  = _actor()
    db     = _db()

    result = ReinstateService().execute(
        yml, actor=actor, role=Role.MOVER,
        phase="moving", clearing_reason=args.reason, db=db,
    )
    _print_result(result)

    if result.success:
        dim(f"  clearing reason: {args.reason}")
        dim(f"  status is now: staged")


def cmd_edit(args: argparse.Namespace) -> None:
    """Edit moving fields on an artifact."""
    yml, _ = _resolve_yml(args.aimcr, args.artifact_name)
    actor  = _actor()
    db     = _db()

    # parse field=value pairs
    if not args.fields:
        err("No fields provided. Usage: edit <aimcr> <name> field=value ...")
        sys.exit(1)

    # allowed moving fields only — curators own everything else
    ALLOWED = {
        "moving_notes",
        "checksum_verified_gpup",
        "destination_path",
        "project_id",
    }

    updated: dict = {}
    for kv in args.fields:
        if "=" not in kv:
            err(f"Invalid format: '{kv}'. Use field=value.")
            sys.exit(1)
        key, _, val = kv.partition("=")
        key = key.strip()
        val = val.strip()

        if key not in ALLOWED:
            err(f"Field '{key}' is not editable via CLI.")
            warn(f"Mover-editable fields: {', '.join(sorted(ALLOWED))}")
            sys.exit(1)

        updated[key] = val

    result = MovingUpdateService().execute(
        yml, actor=actor, updated_fields=updated, db=db,
    )
    _print_result(result)

    if result.success:
        for k, v in updated.items():
            dim(f"  {k}: {v}")
        dim(f"  changes visible in the UI on next refresh")


# ── argument parser ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cli.py",
        description="Metadata Dashboard CLI — mover tools",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python cli.py list
  python cli.py list --status staged --json
  python cli.py inspect 65616 Seed-X-PPO-7B
  python cli.py move 65616 Seed-X-PPO-7B --verified-by mover1 --notes "ok"
  python cli.py block 65616 Seed-X-PPO-7B "quota exceeded on gpup"
  python cli.py reinstate 65616 Seed-X-PPO-7B "quota resolved"
  python cli.py edit 65616 Seed-X-PPO-7B moving_notes="re-verified after rerun"
        """
    )

    sub = p.add_subparsers(dest="command", metavar="command")
    sub.required = True

    # list
    p_list = sub.add_parser("list", help="list artifacts")
    p_list.add_argument("--aimcr",   metavar="REF",    help="filter by AIMCR reference")
    p_list.add_argument("--status",  metavar="STATUS", help="filter by status (staged/moved/blocked/rejected)")
    p_list.add_argument("--type",    metavar="TYPE",   help="filter by type (dataset/model/software)")
    p_list.add_argument("--json",    action="store_true", help="output as JSON")

    # inspect
    p_inspect = sub.add_parser("inspect", help="show full artifact detail")
    p_inspect.add_argument("aimcr",         metavar="AIMCR")
    p_inspect.add_argument("artifact_name", metavar="NAME")
    p_inspect.add_argument("--json",        action="store_true")

    # move
    p_move = sub.add_parser("move", help="confirm artifact move to gpup")
    p_move.add_argument("aimcr",            metavar="AIMCR")
    p_move.add_argument("artifact_name",    metavar="NAME")
    p_move.add_argument("--verified-by",    metavar="USER",  help="who verified the gpup checksum (default: current user)")
    p_move.add_argument("--notes",          metavar="TEXT",  help="moving notes")

    # block
    p_block = sub.add_parser("block", help="block an artifact with a reason")
    p_block.add_argument("aimcr",           metavar="AIMCR")
    p_block.add_argument("artifact_name",   metavar="NAME")
    p_block.add_argument("reason",          metavar="REASON")

    # reinstate
    p_reinstate = sub.add_parser("reinstate", help="reinstate a blocked or rejected artifact")
    p_reinstate.add_argument("aimcr",           metavar="AIMCR")
    p_reinstate.add_argument("artifact_name",   metavar="NAME")
    p_reinstate.add_argument("reason",          metavar="REASON")

    # edit
    p_edit = sub.add_parser("edit", help="edit moving fields")
    p_edit.add_argument("aimcr",            metavar="AIMCR")
    p_edit.add_argument("artifact_name",    metavar="NAME")
    p_edit.add_argument("fields",           metavar="field=value", nargs="+")

    return p


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = build_parser()
    args   = parser.parse_args()

    dispatch = {
        "list":      cmd_list,
        "inspect":   cmd_inspect,
        "move":      cmd_move,
        "block":     cmd_block,
        "reinstate": cmd_reinstate,
        "edit":      cmd_edit,
    }

    dispatch[args.command](args)


if __name__ == "__main__":
    main()
