"""Document-related routes.

This module currently hosts *claim-level* document actions (upload/download/delete/open-location).
Report-level document routes live in routes/reports.py.

Keeping this isolated prevents app/routes.py from turning into a 5k-line crime scene.
"""

from __future__ import annotations

import os
import sys
import subprocess
from datetime import datetime
from app.models import now, today
from pathlib import Path

from flask import current_app, flash, redirect, request, send_file, url_for
from werkzeug.utils import secure_filename

from app import db
from app.models import Claim, ClaimDocument, Settings

from . import bp


def _safe_segment(text: str) -> str:
    """Filesystem-safe name chunk."""
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in (text or ""))


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
    """Folder for a specific claim's documents.

    IMPORTANT: The folder name must be stable even if Gina later edits the claimant name.

    Strategy:
    - Prefer an existing folder that starts with `<claim.id>_` if present.
    - Otherwise, create a new folder using `<claim.id>_<safe claimant>`.
    """
    root = _get_documents_root()

    # Prefer an existing folder (claimant name may change over time).
    try:
        matches = sorted(
            [p for p in root.glob(f"{claim.id}_*") if p.is_dir()],
            key=lambda p: p.name,
        )
        if matches:
            return matches[0]
    except Exception:
        pass

    claimant_segment = _safe_segment(getattr(claim, "claimant_name", None) or f"claim_{claim.id}")
    folder = root / f"{claim.id}_{claimant_segment}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@bp.route("/claims/<int:claim_id>/documents/upload", methods=["POST"])
def claim_document_upload(claim_id: int):
    """Upload a claim-level document and create its DB record.

    This is intentionally separate from claim_detail POST handling so uploads
    never collide with billable-item validation.
    """
    claim = Claim.query.get_or_404(claim_id)

    # Support multi-select uploads. Prefer the standard field name "file",
    # but remain backward compatible with older field names.
    uploads = []
    for key in ("file", "document", "upload"):
        try:
            uploads = request.files.getlist(key)
        except Exception:
            uploads = []
        # If this key exists in the form, stop searching (even if empty),
        # so we don't mix keys unexpectedly.
        if key in request.files:
            break

    # Filter empty selections (browser can submit empty parts)
    uploads = [f for f in (uploads or []) if getattr(f, "filename", "")]

    doc_type = (
        (request.form.get("doc_type") or "")
        or (request.form.get("type") or "")
        or (request.form.get("document_type") or "")
    ).strip() or None

    description = (
        (request.form.get("description") or "")
        or (request.form.get("doc_description") or "")
    ).strip() or None

    if not uploads:
        flash("Choose a file to upload.", "danger")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    if not doc_type:
        flash("Document type is required.", "danger")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    # Ensure docs root exists / is configured (mirrors app behavior)
    root = _get_documents_root()
    if not root.exists():
        flash("Documents root folder could not be created.", "danger")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    claim_folder = _get_claim_folder(claim)

    created_count = 0
    saved_paths: list[Path] = []

    # Use a stable base timestamp for this batch; add a per-file suffix to avoid collisions.
    batch_ts = now().strftime("%Y%m%d_%H%M%S")

    for idx, uploaded in enumerate(uploads, start=1):
        original_name = uploaded.filename
        safe_name = secure_filename(original_name) or "upload"
        stored_name = f"{batch_ts}_{idx:02d}_{safe_name}"

        file_path = Path(claim_folder) / stored_name

        try:
            uploaded.save(file_path)
            saved_paths.append(file_path)
        except Exception:
            current_app.logger.exception("Failed to save uploaded document")
            flash("Upload failed while saving the file.", "danger")
            # Cleanup any files saved earlier in this batch.
            for p in saved_paths:
                try:
                    if p.exists():
                        p.unlink()
                except Exception:
                    pass
            return redirect(url_for("main.claim_detail", claim_id=claim.id))

        doc = ClaimDocument(claim_id=claim.id)

        # Defensive field mapping (schema has evolved).
        if hasattr(doc, "doc_type"):
            setattr(doc, "doc_type", doc_type)
        elif hasattr(doc, "type"):
            setattr(doc, "type", doc_type)

        if hasattr(doc, "description"):
            setattr(doc, "description", description)

        if hasattr(doc, "filename_stored"):
            setattr(doc, "filename_stored", stored_name)
        elif hasattr(doc, "stored_filename"):
            setattr(doc, "stored_filename", stored_name)
        elif hasattr(doc, "filename"):
            setattr(doc, "filename", stored_name)

        if hasattr(doc, "original_filename"):
            setattr(doc, "original_filename", original_name)

        if hasattr(doc, "uploaded_at") and getattr(doc, "uploaded_at", None) is None:
            setattr(doc, "uploaded_at", now())

        db.session.add(doc)
        created_count += 1

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        # Avoid orphan files if DB write fails.
        for p in saved_paths:
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass
        flash("Upload failed while saving to the database.", "danger")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    if created_count == 1:
        flash("Document uploaded.", "success")
    else:
        flash(f"{created_count} documents uploaded.", "success")

    return redirect(url_for("main.claim_detail", claim_id=claim.id))


@bp.route("/claims/<int:claim_id>/documents/<int:doc_id>/download")
def claim_document_download(claim_id: int, doc_id: int):
    """Download a stored claim document."""
    claim = Claim.query.get_or_404(claim_id)
    doc = ClaimDocument.query.filter_by(id=doc_id, claim_id=claim.id).first_or_404()

    stored_name = (
        getattr(doc, "filename_stored", None)
        or getattr(doc, "stored_filename", None)
        or getattr(doc, "filename", None)
        or getattr(doc, "stored_name", None)
    )
    if not stored_name:
        flash("Document record is missing a stored filename.", "danger")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    claim_folder = _get_claim_folder(claim)
    file_path = Path(claim_folder) / stored_name

    if not file_path.exists():
        flash("File not found on disk.", "danger")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    # Default behavior: open inline in the browser when possible.
    # If the user explicitly requests a download, force attachment.
    download_flag = (request.args.get("download") or "").strip().lower()
    as_attachment = download_flag in ("1", "true", "yes")

    return send_file(
        file_path,
        as_attachment=as_attachment,
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

    stored_name = (
        getattr(doc, "filename_stored", None)
        or getattr(doc, "stored_filename", None)
        or getattr(doc, "filename", None)
        or getattr(doc, "stored_name", None)
    )
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

    stored_name = (
        getattr(doc, "filename_stored", None)
        or getattr(doc, "stored_filename", None)
        or getattr(doc, "filename", None)
        or getattr(doc, "stored_name", None)
    )
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