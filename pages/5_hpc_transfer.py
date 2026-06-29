"""
pages/5_hpc_transfer.py
-----------------------
HPC pipeline — ticket-driven data transfers.

Thin entry point. All rendering lives in components/hpc_transfer_forms.py.
All DB + YML operations live in iolib/hpc_transfer_service.py.
All Slurm script generation lives in components/hpc_slurm.py.
"""

from __future__ import annotations

import streamlit as st

from auth.session import current_username, init_session, require_role
from config import Role
from iolib.transfers_db import TransfersDatabase
from iolib.hpc_transfer_service import HpcTransferService
from components.hpc_transfer_forms import (
    render_new_transfer,
    render_premove,
    render_postmove,
    render_log,
)


def _svc() -> HpcTransferService:
    if "hpc_transfer_svc" not in st.session_state:
        st.session_state["hpc_transfer_svc"] = HpcTransferService(
            TransfersDatabase()
        )
    return st.session_state["hpc_transfer_svc"]


def main() -> None:
    init_session()

    if not require_role(Role.CURATOR, Role.MOVER):
        st.stop()

    actor = current_username()
    svc   = _svc()

    st.title("HPC Transfer")
    st.caption(
        "Ticket-driven data transfers. "
        "Checksum Slurm scripts are generated and written to the workspace; "
        "submission is your responsibility."
    )

    tab_new, tab_pre, tab_post, tab_log = st.tabs([
        "New transfer",
        "Pre-move",
        "Post-move",
        "Transfer log",
    ])

    with tab_new:
        render_new_transfer(actor, svc)

    with tab_pre:
        render_premove(actor, svc)

    with tab_post:
        render_postmove(actor, svc)

    with tab_log:
        render_log(svc)


main()
