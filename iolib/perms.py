"""
iolib/perms.py
--------------
Group ownership and permissions for everything the app creates on disk.

Every YML, modlog, manifest and .lock file gets group FILE_GROUP (k03) and
u+rwx,g+rwx. Every directory the app creates gets the same plus setgid, so
files later created inside it — by anyone — inherit the group.

Rules:
- Bits are only ever added, never removed; "other" permissions are untouched.
- Best effort: chgrp needs the process owner to be a member of FILE_GROUP,
  and chmod needs ownership of the file. A failure is logged once per path
  and never interrupts the write it accompanies.
- Apply to the temp file BEFORE the atomic rename/link, so the final file
  appears with the right group and mode in one step.
"""

from __future__ import annotations

import grp
import logging
import os
import stat
from functools import lru_cache
from pathlib import Path

from config import FILE_GROUP

logger = logging.getLogger(__name__)

_FILE_BITS = stat.S_IRWXU | stat.S_IRWXG                  # u+rwx,g+rwx
_DIR_BITS  = stat.S_IRWXU | stat.S_IRWXG | stat.S_ISGID   # + setgid
_warned: set[str] = set()


@lru_cache(maxsize=1)
def _gid() -> int | None:
    try:
        return grp.getgrnam(FILE_GROUP).gr_gid
    except KeyError:
        logger.warning("Group %r not found — files keep their default group.", FILE_GROUP)
        return None


def _warn_once(path: Path, what: str, exc: Exception) -> None:
    key = f"{what}:{path}"
    if key not in _warned:
        _warned.add(key)
        logger.warning("Could not %s on %s: %s", what, path, exc)


def apply_group_rwx(path: Path, directory: bool | None = None) -> None:
    """Give `path` group FILE_GROUP and add u+rwx,g+rwx (plus setgid on dirs)."""
    try:
        st = os.lstat(path)
    except OSError:
        return
    if stat.S_ISLNK(st.st_mode):
        return
    is_dir = stat.S_ISDIR(st.st_mode) if directory is None else directory

    gid = _gid()
    if gid is not None and st.st_gid != gid:
        try:
            os.chown(path, -1, gid)
        except OSError as exc:
            _warn_once(path, f"set group {FILE_GROUP}", exc)

    want = stat.S_IMODE(st.st_mode) | (_DIR_BITS if is_dir else _FILE_BITS)
    if want != stat.S_IMODE(st.st_mode):
        try:
            os.chmod(path, want)
        except OSError as exc:
            _warn_once(path, "set group rwx", exc)


def make_dirs(path: Path) -> Path:
    """
    mkdir -p that applies group rwx + setgid to every directory it CREATES.
    Existing directories are left as they are.
    """
    path = Path(path)
    missing: list[Path] = []
    p = path
    while not p.exists():
        missing.append(p)
        if p.parent == p:
            break
        p = p.parent
    for d in reversed(missing):
        try:
            d.mkdir(exist_ok=True)
        except FileExistsError:
            continue
        apply_group_rwx(d, directory=True)
    return path


def open_lock_file(path: Path):
    """
    Open (creating if needed) a lock sentinel for flock, group-writable so
    another k03 member can take the same lock later. Never truncates — the
    file is only a lock handle.
    """
    make_dirs(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o660)
    apply_group_rwx(path, directory=False)
    return os.fdopen(fd, "r+")
