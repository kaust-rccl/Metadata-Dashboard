"""
iolib/ticket.py
---------------
Suggested DM-ticket subject and body for an artifact. The text is a
suggestion: the curator edits it in the portal, and the edited version is
kept per artifact in the DB (ticket_drafts) — never in the YML.
"""

from __future__ import annotations

from config import ArtifactType

_TYPE_PLURAL = {
    ArtifactType.MODEL:    "Models",
    ArtifactType.DATASET:  "Datasets",
    ArtifactType.SOFTWARE: "Software",
}


def default_ticket(yml) -> tuple[str, str]:
    """(subject, body) from the template, filled from the record."""
    kind = _TYPE_PLURAL.get(yml.artifact_type, str(yml.artifact_type.value).title())
    ref = yml.aimcr_reference or "6####"

    source = yml.source_path.strip() or f"/scratch/project/k03/support_team/{ref}/<path_to_artifact>"
    if not source.endswith("/"):
        source += "/"

    if yml.destination_path.strip():
        dest = yml.destination_path.strip()
    elif yml.project_id.strip():
        dest = f"/scratch/project/{yml.project_id.strip()}"
    else:
        dest = "/scratch/project/k#####"
    if not dest.endswith("/"):
        dest += "/"

    subject = f"Sh3 Data Movement Request: {kind} - {yml.artifact_name}"
    body = (
        f"Source: {source}\n"
        f"\n"
        f"Destination: {dest}\n"
        f"\n"
        f"Group: K03\n"
        f"\n"
        f"AIMCR reference: {ref}\n"
        f"\n"
        f"Context: Models/Datasets movement for GC project proposal ID: {ref}"
    )
    return subject, body
