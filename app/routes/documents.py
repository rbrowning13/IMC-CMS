

"""Document-related routes.

This module currently hosts *claim-level* document actions (download/delete/open-location).
Report-level document routes live in routes/reports.py.

Keeping this isolated prevents app/routes.py from turning into a 5k-line crime scene.
"""

from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path

from flask import current_app, flash, redirect, request, send_file, url_for

from app import db
from app.models import Claim, ClaimDocument, Settings

from . import bp


def _safe_segment(text: str) -> str:
    """Filesystem-safe name chunk."""
    return "".join(
        ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in (text or "")
    )


def _get_documents_root() -> Path:
    """Resolve the root folder for all documents.

    Mirrors legacy behavior:
    - If Settings.documents_root is ABSOLUTE, use it.
    - If RELATIVE, treat as relative to the project root (current_app.root_path).
    - If empty, default to `<project_root>/documents`.

    Always creates the folder if missing.
    """
    settings = Settings.query.first()
    raw = settings.documents_root if settings and settings.documents_root else ""

    project_root = Path(current_app.root_path).resolve()

    if raw:
        root = Path(raw).expanduser()
        if not root.is_absolute():
            root = project_root / root
    else:
        root = project_root / "documents"

    root.mkdir(parents=True, exist_ok=True)
    return root


def _get_claim_folder(claim: Claim) -> Path:
    """Folder for a specific claim's documents."""
    root = _get_documents_root()
    claimant_segment = _safe_segment(claim.claimant_name or f"claim_{claim.id}")
    folder = root / f"{claim.id}_{claimant_segment}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@bp.route("/claims/<int:claim_id>/documents/<int:doc_id>/download")
def claim_document_download(claim_id: int, doc_id: int):
    """Download a stored claim document."""
    claim = Claim.query.get_or_404(claim_id)
    doc = ClaimDocument.query.filter_by(id=doc_id, claim_id=claim.id).first_or_404()

    stored_name = getattr(doc, "filename_stored", None)
    if not stored_name:
        flash("Document record is missing a stored filename.", "danger")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    claim_folder = _get_claim_folder(claim)
    file_path = Path(claim_folder) / stored_name

    if not file_path.exists():
        flash("File not found on disk.", "danger")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    # Use send_file with an absolute path.
    return send_file(
        file_path,
        as_attachment=True,
        download_name=(getattr(doc, "original_filename", None) or stored_name),
    )


@bp.route(
    "/claims/<int:claim_id>/documents/<int:doc_id>/delete",
    methods=["POST"],
)
def claim_document_delete(claim_id: int, doc_id: int):
    """Delete a claim document record and attempt to delete the file on disk."""
    claim = Claim.query.get_or_404(claim_id)
    doc = ClaimDocument.query.filter_by(id=doc_id, claim_id=claim.id).first_or_404()

    stored_name = getattr(doc, "filename_stored", None)
    try:
        if stored_name:
            claim_folder = _get_claim_folder(claim)
            file_path = Path(claim_folder) / stored_name
            if file_path.exists():
                try:
                    file_path.unlink()
                except Exception:
                    # Don’t block DB delete on filesystem weirdness.
                    pass

        db.session.delete(doc)
        db.session.commit()
        flash("Document deleted.", "success")
    except Exception:
        db.session.rollback()
        flash("Could not delete document.", "danger")

    return redirect(url_for("main.claim_detail", claim_id=claim.id))


@bp.route(
    "/claims/<int:claim_id>/documents/<int:doc_id>/open-location",
    methods=["POST"],
)
def claim_document_open_location(claim_id: int, doc_id: int):
    """Open the claim's documents folder in the OS file browser."""
    claim = Claim.query.get_or_404(claim_id)
    doc = ClaimDocument.query.filter_by(id=doc_id, claim_id=claim.id).first_or_404()

    stored_name = getattr(doc, "filename_stored", None)
    if not stored_name:
        flash("Document record is missing a stored filename.", "danger")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    claim_folder = Path(_get_claim_folder(claim))
    file_path = claim_folder / stored_name

    # Prefer opening the folder (and ideally selecting the file).
    try:
        if os.name == "nt":
            # Windows: open Explorer and select the file
            subprocess.run(["explorer", "/select,", str(file_path)], check=False)
        elif sys.platform == "darwin":
            # macOS: reveal file in Finder
            subprocess.run(["open", "-R", str(file_path)], check=False)
        else:
            # Linux: open folder (selection varies by DE)
            subprocess.run(["xdg-open", str(claim_folder)], check=False)
    except Exception:
        flash("Could not open file location.", "danger")

    return redirect(url_for("main.claim_detail", claim_id=claim.id))