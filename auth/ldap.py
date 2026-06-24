"""
auth/ldap.py
------------
Role resolution from Unix system group membership.

Uses `id -Gn <username>` — no LDAP client library required.
Works on any POSIX system where groups are populated via LDAP, NIS,
or local /etc/group (all three are transparent to the `id` command).

To add a new role:
  1. Add the Role value to config.py
  2. Add the group name constant to config.py
  3. Add the mapping here in resolve_role()
"""

from __future__ import annotations

import getpass
import logging
import subprocess

from config import LDAP_GROUP_CURATOR, LDAP_GROUP_MOVER, Role

logger = logging.getLogger(__name__)


def get_current_username() -> str:
    """
    Return the Unix username of the process owner.
    Uses getpass.getuser() which reads $USER / $LOGNAME / pwd.
    """
    return getpass.getuser()


def get_user_groups(username: str) -> list[str]:
    """
    Return the list of group names the user belongs to.
    Falls back to an empty list on any error so the app
    degrades to read-only rather than crashing.
    """
    try:
        result = subprocess.run(
            ["id", "-Gn", username],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip().split()
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Failed to resolve groups for %s: %s", username, exc)
    return []


def resolve_role(username: str) -> Role:
    """
    Map a Unix username to an app Role via group membership.

    Priority: curator > mover > readonly
    If a user belongs to both curator and mover groups (shouldn't
    happen in practice) they get curator.
    """
    groups = get_user_groups(username)

    if LDAP_GROUP_CURATOR in groups:
        return Role.CURATOR
    if LDAP_GROUP_MOVER in groups:
        return Role.MOVER
    return Role.READONLY
