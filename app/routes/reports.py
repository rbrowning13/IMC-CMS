

"""Report-related routes.

This module holds all report-specific endpoints after splitting the legacy
monolithic routes.py.

IMPORTANT:
- Endpoints/URLs must remain identical to legacy routes.py so templates/links do
  not change.
- During the transition, a few helpers are duplicated here. We can later move
  them into a shared helpers module.
"""

from __future__ import annotations

import io
import json
import os
import sys
import subprocess
from pathlib import Path
from datetime import date, datetime, timedelta
from collections import defaultdict

from flask import (
    current_app,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.utils import secure_filename

# Optional WeasyPrint import for PDF generation.
# We catch any Exception here and fall back to HTML = None.
try:
    from weasyprint import HTML
except Exception:
    HTML = None

from ..extensions import db
from ..models import (
    BillableItem,
    Claim,
    Provider,
    Report,
    ReportApprovedProvider,
    ReportDocument,
    Settings,
    BarrierOption,
)

from . import bp


# ---- helpers (temporary duplicates; will move to routes/helpers.py) ----

def _allowed_file(filename: str) -> bool:
    allowed = {
        "pdf",
        "doc",
        "docx",
        "rtf",
        "txt",
        "jpg",
        "jpeg",
        "png",
        "mp4",
        "mov",
        "avi",
    }
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in allowed


def _safe_segment(text: str) -> str:
    """Filesystem-safe name chunk."""
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in (text or ""))



def _open_in_file_manager(path: Path) -> None:
    """Open the given folder in the host OS file manager."""
    folder = Path(path).resolve()
    if not folder.exists():
        return
    try:
        if sys.platform.startswith("darwin"):
            subprocess.Popen(["open", str(folder)])
        elif os.name == "nt":
            os.startfile(str(folder))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except Exception:
        pass


# Reveal a specific file in the host OS file manager when supported.
def _reveal_in_file_manager(file_path: Path) -> None:
    """Reveal a specific file in the host OS file manager when supported.

    Falls back to opening the containing folder.
    """
    p = Path(file_path).resolve()
    if p.exists() and p.is_file():
        try:
            if sys.platform.startswith("darwin"):
                # Reveal in Finder
                subprocess.Popen(["open", "-R", str(p)])
                return
            elif os.name == "nt":
                # Reveal in Explorer
                subprocess.Popen(["explorer", "/select,", str(p)])
                return
        except Exception:
            pass

    # Fallback: open containing folder
    try:
        _open_in_file_manager(p.parent if p.exists() else Path(file_path).parent)
    except Exception:
        pass


def _parse_date(value: str | None):
    """Parse UI date input.

    Accepts 'YYYY-MM-DD' or 'MM/DD/YYYY'. Returns datetime.date or None.
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_mmddyyyy(raw: str, field_label: str = "Date"):
    """Parse dates entered as MMDDYYYY, MM/DD/YYYY, or MM-DD-YYYY.

    Returns (date_obj, error_message). If parsing succeeds, error_message is None.
    If input is empty, returns (None, None).
    """
    if not raw:
        return None, None

    s = raw.strip()
    if not s:
        return None, None

    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) != 8:
        return None, f"{field_label} must be 8 digits in MMDDYYYY format."

    try:
        month = int(digits[0:2])
        day = int(digits[2:4])
        year = int(digits[4:8])

        if year < 1900 or year > 2100:
            return None, f"{field_label} year must be between 1900 and 2100."

        return date(year, month, day), None
    except ValueError:
        return None, f"{field_label} must be a valid calendar date."


def _ensure_settings() -> Settings:
    """Return the single Settings row, creating it if necessary."""
    settings = Settings.query.first()
    if not settings:
        settings = Settings(
            business_name="Impact Medical Consulting, PLLC",
            state="ID",
            hourly_rate=50.0,
            telephonic_rate=50.0,
            mileage_rate=0.50,
        )
        db.session.add(settings)
        db.session.commit()
    return settings


def _get_documents_root() -> Path:
    """Resolve the root folder for all documents."""
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


def _get_report_folder(report: Report) -> Path:
    """Folder for a report's documents (under the claim folder /reports)."""
    claim_folder = _get_claim_folder(report.claim)
    report_root = claim_folder / "reports"
    report_root.mkdir(parents=True, exist_ok=True)
    return report_root



def _get_barrier_options_grouped():
    """Return active BarrierOption rows grouped by category."""
    options = (
        BarrierOption.query.filter_by(is_active=True)
        .order_by(BarrierOption.sort_order, BarrierOption.label)
        .all()
    )
    grouped: dict[str, list[BarrierOption]] = defaultdict(list)
    for opt in options:
        category = opt.category or "General"
        grouped[category].append(opt)
    return grouped


# Helper: return selected BarrierOption rows for a report, ordered for display.
def _get_selected_barriers(report: Report) -> list[BarrierOption]:
    """Return selected BarrierOption rows for a report, ordered for display."""
    raw = getattr(report, "barriers_json", None)
    if not raw:
        return []

    ids: list[int] = []
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = None

    if isinstance(parsed, list):
        for x in parsed:
            try:
                i = int(x)
            except (TypeError, ValueError):
                continue
            if i not in ids:
                ids.append(i)

    if not ids:
        return []

    # Fetch active options for selected IDs.
    rows = (
        BarrierOption.query.filter(BarrierOption.id.in_(ids))
        .filter_by(is_active=True)
        .all()
    )

    # Preserve the user's selection order when possible; fall back to sort_order/label.
    by_id = {b.id: b for b in rows}
    ordered = [by_id[i] for i in ids if i in by_id]

    # Append any remaining (shouldn't happen often) in stable display order.
    remaining = [b for b in rows if b.id not in set(ids)]
    remaining.sort(key=lambda b: (b.sort_order or 0, b.label or ""))
    ordered.extend(remaining)

    return ordered


# Helper: compute 1-based sequence number for progress reports within a claim.

def _compute_progress_report_number(claim_id: int, report_id: int) -> int | None:
    """Return 1-based sequence number for a progress report within a claim.

    Sequence is based on chronological order (DOS start, then created_at, then id).
    Returns None if the report is not found among progress reports.
    """
    q = Report.query.filter_by(claim_id=claim_id, report_type="progress")

    # Optional soft-delete guards (only applied if fields exist).
    if hasattr(Report, "is_deleted"):
        q = q.filter(Report.is_deleted.is_(False))
    if hasattr(Report, "deleted_at"):
        q = q.filter(Report.deleted_at.is_(None))

    progress_reports = q.order_by(
        Report.dos_start.asc().nullslast(),
        Report.created_at.asc().nullslast(),
        Report.id.asc(),
    ).all()

    for idx, r in enumerate(progress_reports, start=1):
        if r.id == report_id:
            return idx

    return None

# Helper: compute 1-based sequence number for ALL reports within a claim.
# Requirement: Initial report is #1, then count up chronologically across report types.
def _compute_claim_report_number(claim_id: int, report_id: int) -> int | None:
    """Return 1-based sequence number for any report within a claim.

    Sequence is based on chronological order (DOS start, then created_at, then id).
    Returns None if the report is not found.
    """
    q = Report.query.filter_by(claim_id=claim_id)

    # Optional soft-delete guards (only applied if fields exist).
    if hasattr(Report, "is_deleted"):
        q = q.filter(Report.is_deleted.is_(False))
    if hasattr(Report, "deleted_at"):
        q = q.filter(Report.deleted_at.is_(None))

    reports = q.order_by(
        Report.dos_start.asc().nullslast(),
        Report.created_at.asc().nullslast(),
        Report.id.asc(),
    ).all()

    for idx, r in enumerate(reports, start=1):
        if r.id == report_id:
            return idx

    return None


# ------------------------
# Report routes
# ------------------------

@bp.route("/claims/<int:claim_id>/reports/new", methods=["GET", "POST"])
def report_new(claim_id):
    """Create a new report for a claim and redirect to edit.

    Keep URL identical to legacy routes.py.

    Report type is accepted from:
      - querystring: ?report_type=initial|progress|closure
      - form field: report_type

    Defaults:
      - Initial: DOS start = claim created date (if present) else claim referral date (if present) else today; DOS end = today
      - Progress/Closure: DOS start = day after the most recent prior report DOS end (based on DOS end, then created_at); DOS end = today

    Also:
      - Copy barriers_json from the most recent prior report if present.
      - Auto-create a BillableItem for report-writing time (Initial=1.0, Progress/Closure=0.5).
      - If creating a Closure report and Claim has an is_closed flag, mark claim closed.
    """
    claim = Claim.query.get_or_404(claim_id)

    report_type_raw = ((request.values.get("report_type") or "").strip().lower())
    if report_type_raw not in {"initial", "progress", "closure"}:
        flash("Report type is required.", "danger")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    today = date.today()

    # Most recent prior report (any type) for defaults + carry-forward.
    # Use DOS end when available (this matches “last submitted report” behavior better than created_at).
    last_report = (
        Report.query.filter_by(claim_id=claim.id)
        .order_by(
            Report.dos_end.desc().nullslast(),
            Report.created_at.desc().nullslast(),
            Report.id.desc(),
        )
        .first()
    )

    if report_type_raw == "initial":
        # Initial report: DOS start should be the date the claim was put in the system.
        # Prefer claim.created_at (date) when present; fall back to referral_date; else today.
        created = getattr(claim, "created_at", None)
        if isinstance(created, datetime):
            created = created.date()

        referral = getattr(claim, "referral_date", None)
        if isinstance(referral, datetime):
            referral = referral.date()

        if isinstance(created, date):
            dos_start = created
        elif isinstance(referral, date):
            dos_start = referral
        else:
            dos_start = today

        dos_end = today
    else:
        # Progress/Closure: DOS start is the day AFTER the last submitted report.
        # Prefer last_report.dos_end; fall back to last_report.created_at; else today.
        last_end = getattr(last_report, "dos_end", None) if last_report else None
        if isinstance(last_end, datetime):
            last_end = last_end.date()

        if isinstance(last_end, date):
            dos_start = last_end + timedelta(days=1)
        else:
            last_created = getattr(last_report, "created_at", None) if last_report else None
            if isinstance(last_created, datetime):
                dos_start = last_created.date() + timedelta(days=1)
            elif isinstance(last_created, date):
                dos_start = last_created + timedelta(days=1)
            else:
                dos_start = today

        dos_end = today

    report = Report(
        claim_id=claim.id,
        report_type=report_type_raw,
        dos_start=dos_start,
        dos_end=dos_end,
    )

    # Carry forward barriers selection to new reports.
    # IMPORTANT: do NOT assume the immediate last report has barriers selected.
    # Find the most recent prior report (any type) with a non-empty, valid selection.
    source_report_for_barriers = None
    recent_reports = (
        Report.query.filter_by(claim_id=claim.id)
        .order_by(Report.created_at.desc())
        .all()
    )

    for r in recent_reports:
        raw = getattr(r, "barriers_json", None)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except Exception:
            continue
        if isinstance(parsed, list) and len(parsed) > 0:
            source_report_for_barriers = r
            break

    if source_report_for_barriers:
        report.barriers_json = source_report_for_barriers.barriers_json

    db.session.add(report)
    db.session.flush()  # ensure report.id exists

    # Carry forward Treating Provider(s) selections to new reports.
    # IMPORTANT: do NOT assume the immediate last report has selections.
    # Prefer join-table rows from the most recent report that has them; fall back to the most recent
    # legacy treating_provider_id if no join rows exist anywhere.
    prev_provider_ids: list[int] = []

    source_report_for_providers = None
    for r in recent_reports:
        ids = (
            db.session.query(ReportApprovedProvider.provider_id)
            .filter(ReportApprovedProvider.report_id == r.id)
            .order_by(ReportApprovedProvider.sort_order.asc())
            .all()
        )
        row_ids = [pid for (pid,) in ids]
        if row_ids:
            source_report_for_providers = r
            prev_provider_ids = row_ids
            break

    if not prev_provider_ids:
        # Back-compat: fall back to the most recent legacy single treating provider
        for r in recent_reports:
            legacy = getattr(r, "treating_provider_id", None)
            if legacy:
                prev_provider_ids = [legacy]
                break

    if prev_provider_ids:
        for idx, pid in enumerate(prev_provider_ids):
            db.session.add(
                ReportApprovedProvider(
                    report_id=report.id,
                    provider_id=pid,
                    sort_order=idx,
                )
            )

        # Back-compat: keep legacy single provider pointing to first selection
        report.treating_provider_id = prev_provider_ids[0]

    # Auto-create report-writing billable (editable later)
    qty = 1.0 if report_type_raw == "initial" else 0.5

    # Use the canonical billing activity code for report writing.
    # (Older data/spec uses REP; keep this stable across the app.)
    bill = BillableItem(
        claim_id=claim.id,
        activity_code="REP",
        date_of_service=dos_end,
        quantity=qty,
        description=f"{report_type_raw.title()} report writing",
        is_complete=True,
    )
    db.session.add(bill)

    # If Closure report, mark Claim closed when supported
    if report_type_raw == "closure" and hasattr(claim, "is_closed"):
        claim.is_closed = True

    db.session.commit()

    return redirect(url_for("main.report_edit", claim_id=claim.id, report_id=report.id))


@bp.route("/claims/<int:claim_id>/reports/<int:report_id>")
def report_detail(claim_id, report_id):
    """Read-only view / preview of a single report."""
    claim = Claim.query.get_or_404(claim_id)
    report = Report.query.filter_by(id=report_id, claim_id=claim.id).first_or_404()

    barriers_by_category = _get_barrier_options_grouped()
    selected_barrier_ids = set()
    if report.barriers_json:
        try:
            data = json.loads(report.barriers_json)
            selected_barrier_ids = {int(x) for x in data}
        except (TypeError, ValueError):
            selected_barrier_ids = set()

    settings = _ensure_settings()

    return render_template(
        "report_detail.html",
        active_page="claims",
        claim=claim,
        report=report,
        settings=settings,
        barriers_by_category=barriers_by_category,
        selected_barrier_ids=selected_barrier_ids,
    )


@bp.route("/claims/<int:claim_id>/reports/append-field", methods=["GET"])
def report_append_field(claim_id):
    """Return the requested field's text from the most recent report for this claim."""
    field = (request.args.get("field") or "").strip()

    allowed_fields = {
        "work_status",
        "case_management_plan",
    }

    if field not in allowed_fields:
        return jsonify({"error": "Invalid field"}), 400

    last_report = (
        Report.query.filter_by(claim_id=claim_id)
        .order_by(Report.created_at.desc())
        .first()
    )

    if not last_report:
        return jsonify({"value": ""}), 200

    value = getattr(last_report, field, "") or ""
    return jsonify({"value": value}), 200


@bp.route(
    "/claims/<int:claim_id>/reports/<int:report_id>/documents/upload",
    methods=["POST"],
)
def report_document_upload(claim_id, report_id):
    """Handle upload of a document linked to a specific report."""
    claim = Claim.query.get_or_404(claim_id)
    report = Report.query.filter_by(id=report_id, claim_id=claim.id).first_or_404()

    file = request.files.get("file")
    doc_type = (request.form.get("doc_type") or "").strip() or None
    description = (request.form.get("description") or "").strip() or None
    document_date = (request.form.get("document_date") or "").strip() or None

    if not file or not file.filename:
        flash("Please choose a file to upload.", "danger")
        return redirect(url_for("main.report_edit", claim_id=claim.id, report_id=report.id))

    if not _allowed_file(file.filename):
        flash("File type not allowed.", "danger")
        return redirect(url_for("main.report_edit", claim_id=claim.id, report_id=report.id))

    report_folder = _get_report_folder(report)

    original_safe = secure_filename(file.filename)
    claim_number_part = (
        _safe_segment(report.claim.claim_number)
        if report.claim and report.claim.claim_number
        else f"claim_{report.claim_id}"
    )
    report_part = f"report_{report.id}"

    base_name, ext = os.path.splitext(original_safe)
    base_name = _safe_segment(base_name) or "document"
    ext = ext or ""

    candidate = f"{claim_number_part}_{report_part}_{base_name}{ext}"
    stored_name = candidate
    counter = 1
    while (report_folder / stored_name).exists():
        stored_name = f"{claim_number_part}_{report_part}_{base_name}_{counter}{ext}"
        counter += 1

    file_path = report_folder / stored_name
    file.save(file_path)

    doc = ReportDocument(
        report_id=report.id,
        doc_type=doc_type,
        description=description,
        original_filename=file.filename,
        stored_path=stored_name,
        document_date=document_date or date.today().isoformat(),
    )
    db.session.add(doc)
    db.session.commit()

    flash("Report document uploaded.", "success")
    return redirect(url_for("main.report_edit", claim_id=claim.id, report_id=report.id))


@bp.route(
    "/claims/<int:claim_id>/reports/<int:report_id>/roll-forward/<string:field_name>",
    methods=["GET"],
)
def report_roll_forward(claim_id, report_id, field_name):
    """Return ONLY the requested field's content from the most recent prior report."""
    allowed_fields = {
        # Shared long-text fields
        "status_treatment_plan",
        "work_status",
        "employment_status",
        "case_management_plan",
        # Initial-specific fields
        "initial_diagnosis",
        "initial_mechanism_of_injury",
        "initial_coexisting_conditions",
        "initial_surgical_history",
        "initial_medications",
        "initial_diagnostics",
        # Closure-specific fields
        "closure_details",
        "closure_case_management_impact",
    }

    if field_name not in allowed_fields:
        return jsonify({"error": "Invalid field"}), 400

    previous = (
        Report.query.filter(Report.claim_id == claim_id, Report.id != report_id)
        .order_by(Report.created_at.desc())
        .first()
    )

    if not previous:
        return jsonify({"value": ""}), 200

    value = getattr(previous, field_name, "") or ""
    return jsonify({"value": value}), 200


@bp.route("/claims/<int:claim_id>/reports/<int:report_id>/edit", methods=["GET", "POST"])
def report_edit(claim_id, report_id):
    """Edit an existing report for a claim."""
    claim = Claim.query.get_or_404(claim_id)
    report = Report.query.filter_by(id=report_id, claim_id=claim.id).first_or_404()

    # Display number for this report within the claim (Initial should be #1)
    display_report_number = _compute_claim_report_number(claim.id, report.id)

    # Convenience for templates that prefer attribute access
    try:
        setattr(report, "display_report_number", display_report_number)
    except Exception:
        pass

    # --- PCP / Family Doctor field back-compat ---
    # DB/schema may store this as `initial_primary_care_provider` (preferred) but
    # older templates/logic may still reference `primary_care_provider`.
    if hasattr(report, "initial_primary_care_provider"):
        try:
            # Provide a runtime alias for templates expecting `report.primary_care_provider`
            setattr(report, "primary_care_provider", getattr(report, "initial_primary_care_provider") or None)
        except Exception:
            pass

    providers = Provider.query.order_by(Provider.name).all()
    error = None

    # Treating Provider(s) multi-select: current selections from join table (ordered)
    approved_provider_ids = (
        db.session.query(ReportApprovedProvider.provider_id)
        .filter(ReportApprovedProvider.report_id == report.id)
        .order_by(ReportApprovedProvider.sort_order.asc())
        .all()
    )
    approved_provider_ids = [pid for (pid,) in approved_provider_ids]

    # Back-compat: if none yet, fall back to legacy single treating provider
    if not approved_provider_ids and getattr(report, "treating_provider_id", None):
        approved_provider_ids = [report.treating_provider_id]

    # ---------------------------------------------------------------------
    # Carry-forward safety net (GET only)
    #
    # Sometimes the "new report" flow can be routed through older/alternate
    # endpoints. If the newly-created report ends up with empty selections,
    # we still want the edit screen to auto-populate from the most recent
    # prior report on the same claim.
    #
    # This is intentionally conservative:
    # - Only runs on GET
    # - Only fills providers if there are no join-table rows yet
    # - Only fills barriers if current barriers_json is empty/invalid/[]
    # - Persists the copied selections so subsequent loads remain stable
    # ---------------------------------------------------------------------
    if request.method == "GET":
        did_change = False

        # ---- Providers carry-forward (join table) ----
        has_join_rows = (
            db.session.query(ReportApprovedProvider.id)
            .filter(ReportApprovedProvider.report_id == report.id)
            .first()
            is not None
        )

        if not has_join_rows:
            # Find the most recent prior report that has approved provider rows
            source_provider_ids: list[int] = []
            prior_reports = (
                Report.query.filter(Report.claim_id == claim.id, Report.id != report.id)
                .order_by(Report.created_at.desc())
                .all()
            )

            for r in prior_reports:
                ids = (
                    db.session.query(ReportApprovedProvider.provider_id)
                    .filter(ReportApprovedProvider.report_id == r.id)
                    .order_by(ReportApprovedProvider.sort_order.asc())
                    .all()
                )
                row_ids = [pid for (pid,) in ids]
                if row_ids:
                    source_provider_ids = row_ids
                    break

            if not source_provider_ids:
                # Back-compat: fall back to most recent legacy treating_provider_id
                for r in prior_reports:
                    legacy = getattr(r, "treating_provider_id", None)
                    if legacy:
                        source_provider_ids = [legacy]
                        break

            if source_provider_ids:
                # Populate join rows on the current report
                for idx, pid in enumerate(source_provider_ids):
                    db.session.add(
                        ReportApprovedProvider(
                            report_id=report.id,
                            provider_id=pid,
                            sort_order=idx,
                        )
                    )

                # Keep legacy field aligned
                report.treating_provider_id = source_provider_ids[0]

                approved_provider_ids = source_provider_ids
                did_change = True

        # ---- Barriers carry-forward ----
        current_barrier_ids: list[int] = []
        if report.barriers_json:
            try:
                parsed = json.loads(report.barriers_json)
                if isinstance(parsed, list):
                    current_barrier_ids = [int(x) for x in parsed if str(x).strip()]
            except Exception:
                current_barrier_ids = []

        if not current_barrier_ids:
            prior_reports = (
                Report.query.filter(Report.claim_id == claim.id, Report.id != report.id)
                .order_by(Report.created_at.desc())
                .all()
            )

            source_barriers_json = None
            for r in prior_reports:
                raw = getattr(r, "barriers_json", None)
                if not raw:
                    continue
                try:
                    parsed = json.loads(raw)
                except Exception:
                    continue
                if isinstance(parsed, list) and len(parsed) > 0:
                    source_barriers_json = raw
                    break

            if source_barriers_json:
                report.barriers_json = source_barriers_json
                did_change = True

        if did_change:
            db.session.commit()

    if request.method == "POST":
        report_type_raw = (request.form.get("report_type") or "").strip().lower()
        report_type = report_type_raw if report_type_raw else (report.report_type or "").lower()

        dos_start_raw = (request.form.get("dos_start") or "").strip() or None
        dos_end_raw = (request.form.get("dos_end") or "").strip() or None
        work_status = (request.form.get("work_status") or "").strip() or None
        case_management_plan = (request.form.get("case_management_plan") or "").strip() or None
        next_report_due_raw = (request.form.get("next_report_due") or "").strip() or None

        treating_provider_id_raw = (request.form.get("treating_provider_id") or "").strip() or None

        status_treatment_plan = (request.form.get("status_treatment_plan") or "").strip() or None
        employment_status = (request.form.get("employment_status") or "").strip() or None
        # PCP / Family Doctor is ONLY captured on the Initial Report (report-level only).
        # Do not persist PCP on Progress/Closure.
        primary_care_provider = (request.form.get("primary_care_provider") or "").strip() or None
        if report_type_raw and report_type_raw.strip().lower() in {"progress", "closure"}:
            primary_care_provider = None

        # Initial-specific clinical content
        initial_diagnosis = (request.form.get("initial_diagnosis") or "").strip() or None
        initial_mechanism_of_injury = (request.form.get("initial_mechanism_of_injury") or "").strip() or None
        initial_coexisting_conditions = (request.form.get("initial_coexisting_conditions") or "").strip() or None
        initial_surgical_history = (request.form.get("initial_surgical_history") or "").strip() or None
        initial_medications = (request.form.get("initial_medications") or "").strip() or None
        initial_diagnostics = (request.form.get("initial_diagnostics") or "").strip() or None

        # Next appointment (initial report)
        initial_next_appt_datetime_raw = (request.form.get("initial_next_appt_datetime") or "").strip()
        initial_next_appt_provider_name = (request.form.get("initial_next_appt_provider_name") or "").strip() or None

        # Closure-specific fields
        closure_reason = (request.form.get("closure_reason") or "").strip() or None
        closure_details = (request.form.get("closure_details") or "").strip() or None
        closure_case_management_impact = (request.form.get("closure_case_management_impact") or "").strip() or None

        # Barriers: list of selected BarrierOption IDs
        barrier_ids_raw = request.form.getlist("barrier_ids")
        barrier_ids: list[int] = []
        for raw_id in barrier_ids_raw:
            try:
                barrier_ids.append(int(raw_id))
            except (TypeError, ValueError):
                continue

        # Use MMDDYYYY parser for the three report date fields
        dos_start = None
        dos_end = None
        next_report_due = None

        if dos_start_raw:
            dos_start, err = _parse_mmddyyyy(dos_start_raw, "DOS Start")
            if err and not error:
                error = err

        if dos_end_raw:
            dos_end, err = _parse_mmddyyyy(dos_end_raw, "DOS End")
            if err and not error:
                error = err

        if next_report_due_raw:
            next_report_due, err = _parse_mmddyyyy(next_report_due_raw, "Next Report Due")
            if err and not error:
                error = err

        treating_provider_id = None
        if treating_provider_id_raw:
            try:
                treating_provider_id = int(treating_provider_id_raw)
            except ValueError:
                treating_provider_id = None

        valid_types = {"initial", "progress", "closure"}
        if not report_type or report_type not in valid_types:
            error = "Report type is required."

        if error:
            flash(error, "danger")
        else:
            report.report_type = report_type
            report.dos_start = dos_start
            report.dos_end = dos_end
            report.work_status = work_status
            report.case_management_plan = case_management_plan
            report.next_report_due = next_report_due

            # --- Treating Provider(s): persist selections via join table ---
            selected_ids = request.form.getlist("approved_provider_ids")
            cleaned_ids: list[int] = []
            for raw in selected_ids:
                raw = (raw or "").strip()
                if not raw:
                    continue
                try:
                    pid = int(raw)
                except ValueError:
                    continue
                if pid not in cleaned_ids:
                    cleaned_ids.append(pid)

            ReportApprovedProvider.query.filter_by(report_id=report.id).delete()
            for idx, pid in enumerate(cleaned_ids):
                db.session.add(
                    ReportApprovedProvider(
                        report_id=report.id,
                        provider_id=pid,
                        sort_order=idx,
                    )
                )

            # Back-compat: keep legacy single provider pointing to first selection
            report.treating_provider_id = cleaned_ids[0] if cleaned_ids else None
            approved_provider_ids = cleaned_ids

            report.status_treatment_plan = status_treatment_plan
            report.employment_status = employment_status

            # Persist initial-style clinical fields for all report types
            report.initial_diagnosis = initial_diagnosis
            report.initial_mechanism_of_injury = initial_mechanism_of_injury
            report.initial_coexisting_conditions = initial_coexisting_conditions
            report.initial_surgical_history = initial_surgical_history
            report.initial_medications = initial_medications
            report.initial_diagnostics = initial_diagnostics

            if initial_next_appt_datetime_raw:
                try:
                    report.initial_next_appt_datetime = datetime.strptime(
                        initial_next_appt_datetime_raw, "%Y-%m-%dT%H:%M"
                    )
                except ValueError:
                    report.initial_next_appt_datetime = None
            else:
                report.initial_next_appt_datetime = None

            report.initial_next_appt_provider_name = initial_next_appt_provider_name

            report.closure_reason = closure_reason
            report.closure_details = closure_details
            report.closure_case_management_impact = closure_case_management_impact

            if barrier_ids:
                report.barriers_json = json.dumps(barrier_ids)
            else:
                report.barriers_json = None

            # PCP / Family Doctor is report-only (Initial Report). Do not write to Claim.
            # Persist only for Initial reports; clear it for other types.
            pcp_value = primary_care_provider if (report.report_type or "").lower() == "initial" else None

            # Preferred schema field name
            if hasattr(report, "initial_primary_care_provider"):
                report.initial_primary_care_provider = pcp_value

            # Back-compat schema field name (if present)
            if hasattr(report, "primary_care_provider"):
                report.primary_care_provider = pcp_value

            # Keep runtime alias in sync for templates
            try:
                setattr(report, "primary_care_provider", pcp_value)
            except Exception:
                pass

            db.session.commit()
            flash("Saved.", "success")
            return redirect(url_for("main.report_edit", claim_id=claim.id, report_id=report.id))

    barriers_by_category = _get_barrier_options_grouped()
    selected_barrier_ids = set()
    if report.barriers_json:
        try:
            data = json.loads(report.barriers_json)
            selected_barrier_ids = {int(x) for x in data}
        except (TypeError, ValueError):
            selected_barrier_ids = set()

    return render_template(
        "report_edit.html",
        active_page="claims",
        claim=claim,
        report=report,
        error=error,
        barriers_by_category=barriers_by_category,
        selected_barrier_ids=selected_barrier_ids,
        providers=providers,
        approved_provider_ids=approved_provider_ids,
        display_report_number=display_report_number,
    )


@bp.route("/reports/documents/<int:report_document_id>/download")
def report_document_download(report_document_id):
    """Download a stored report document."""
    doc = ReportDocument.query.get_or_404(report_document_id)
    report = doc.report
    if not report or not report.claim:
        flash("Document is not linked to a valid report/claim.", "danger")
        return redirect(url_for("main.claims_list"))

    if not getattr(doc, "stored_path", None):
        flash("Document record is missing a stored filename.", "danger")
        return redirect(url_for("main.report_edit", claim_id=report.claim.id, report_id=report.id))

    report_folder = _get_report_folder(report)
    file_path = report_folder / doc.stored_path

    if not file_path.exists():
        flash("File not found on disk.", "danger")
        return redirect(url_for("main.report_edit", claim_id=report.claim.id, report_id=report.id))

    return send_file(
        file_path,
        as_attachment=True,
        download_name=doc.original_filename,
    )


@bp.route("/reports/documents/<int:report_document_id>/delete", methods=["POST"])
def report_document_delete(report_document_id):
    """Delete a report-level document from disk and DB."""
    doc = ReportDocument.query.get_or_404(report_document_id)

    claim_id = doc.report.claim_id if doc.report else None
    report_id = doc.report.id if doc.report else None

    if getattr(doc, "stored_path", None) and doc.report and doc.report.claim:
        report_folder = _get_report_folder(doc.report)
        file_path = report_folder / doc.stored_path
        try:
            os.remove(file_path)
        except FileNotFoundError:
            pass

    db.session.delete(doc)
    db.session.commit()
    flash("Report document deleted.", "success")

    if claim_id and report_id:
        return redirect(url_for("main.report_edit", claim_id=claim_id, report_id=report_id))

    return redirect(url_for("main.claims_list"))



@bp.route("/reports/documents/<int:report_document_id>/open-location", methods=["POST"])
def report_document_open_location(report_document_id):
    """Reveal this report-level document in the OS file manager."""
    doc = ReportDocument.query.get_or_404(report_document_id)
    report = doc.report

    if not report or not report.claim:
        flash("Document is not linked to a valid report/claim.", "danger")
        return redirect(url_for("main.claims_list"))

    if not getattr(doc, "stored_path", None):
        flash("Document record is missing a stored filename.", "danger")
        return redirect(url_for("main.report_edit", claim_id=report.claim.id, report_id=report.id))

    report_folder = _get_report_folder(report)
    file_path = report_folder / doc.stored_path

    if not file_path.exists():
        flash("File not found on disk.", "danger")
        return redirect(url_for("main.report_edit", claim_id=report.claim.id, report_id=report.id))

    _reveal_in_file_manager(file_path)
    return redirect(url_for("main.report_edit", claim_id=report.claim.id, report_id=report.id))


@bp.route("/claims/<int:claim_id>/reports/<int:report_id>/next-appointment.ics")
def report_next_appointment_ics(claim_id, report_id):
    """Generate a simple ICS calendar event for the report's next appointment."""
    claim = Claim.query.get_or_404(claim_id)
    report = Report.query.filter_by(id=report_id, claim_id=claim.id).first_or_404()

    if not report.initial_next_appt_datetime:
        flash("This report does not have a next appointment date/time set.", "warning")
        return redirect(url_for("main.report_edit", claim_id=claim.id, report_id=report.id))

    dt = report.initial_next_appt_datetime

    location_parts: list[str] = []
    if report.treating_provider:
        p = report.treating_provider
        if getattr(p, "name", None):
            location_parts.append(p.name)

        addr1 = getattr(p, "address1", None) or getattr(p, "address", None)
        addr2 = getattr(p, "address2", None)

        if addr1:
            location_parts.append(addr1)
        if addr2:
            location_parts.append(addr2)

        city = getattr(p, "city", None)
        state = getattr(p, "state", None)
        postal = getattr(p, "postal_code", None) or getattr(p, "zip", None)

        city_state_zip = ", ".join(part for part in [city, state, postal] if part)
        if city_state_zip:
            location_parts.append(city_state_zip)

    location = ", ".join(location_parts) if location_parts else "TBD"

    summary = f"Next appointment – {claim.claimant_name or 'Claimant'}"
    description_lines = [
        f"Claim: {claim.claim_number or ''}",
        f"Claimant: {claim.claimant_name or ''}",
    ]
    if report.initial_next_appt_provider_name:
        description_lines.append(f"Provider: {report.initial_next_appt_provider_name}")
    description = "\\n".join(description_lines)

    dtstamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    dtstart = dt.strftime("%Y%m%dT%H%M%S")
    uid = f"{report.id}-{claim.id}@impact-medical-local"

    ics_lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Impact Medical CMS//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{dtstart}",
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{description}",
        f"LOCATION:{location}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    ics_content = "\r\n".join(ics_lines) + "\r\n"

    response = make_response(ics_content)
    response.headers["Content-Type"] = "text/calendar; charset=utf-8"
    filename = f"next_appointment_claim{claim.id}_report{report.id}.ics"
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response


@bp.route("/claims/<int:claim_id>/reports/<int:report_id>/print")
def report_print(claim_id, report_id):
    """Render a print-friendly version of a report (HTML)."""
    claim = Claim.query.get_or_404(claim_id)
    report = Report.query.filter_by(id=report_id, claim_id=claim.id).first_or_404()
    settings = _ensure_settings()

    barriers = _get_selected_barriers(report)

    display_report_number = _compute_claim_report_number(claim.id, report.id)

    # Convenience for templates that prefer attribute access
    try:
        setattr(report, "display_report_number", display_report_number)
    except Exception:
        pass

    cm_activities: list[dict] = []
    if report.dos_start and report.dos_end:
        q = (
            BillableItem.query.filter_by(claim_id=claim.id)
            .filter(BillableItem.date_of_service.isnot(None))
            .filter(BillableItem.date_of_service >= report.dos_start)
            .filter(BillableItem.date_of_service <= report.dos_end)
        )
        q = q.filter(BillableItem.activity_code != "EXP")

        items = (
            q.order_by(
                BillableItem.date_of_service.asc().nullslast(),
                BillableItem.created_at.asc(),
            ).all()
        )

        for item in items:
            cm_activities.append(
                {
                    "date_of_service": item.date_of_service,
                    "activity_code": item.activity_code or "",
                    "description": item.description or "",
                    "notes": item.notes or "",
                }
            )

    cm_items = cm_activities  # back-compat

    return render_template(
        "report_print.html",
        active_page="claims",
        settings=settings,
        claim=claim,
        report=report,
        cm_activities=cm_activities,
        cm_items=cm_items,
        barriers=barriers,
        display_report_number=display_report_number,
    )


@bp.route("/claims/<int:claim_id>/reports/<int:report_id>/pdf")
def report_pdf(claim_id, report_id):
    """Generate a PDF of the report using the same template as report_print."""
    claim = Claim.query.get_or_404(claim_id)
    report = Report.query.filter_by(id=report_id, claim_id=claim.id).first_or_404()

    documents = (
        ReportDocument.query.filter_by(report_id=report.id)
        .order_by(ReportDocument.id.desc())
        .all()
    )

    settings = _ensure_settings()

    barriers = _get_selected_barriers(report)

    display_report_number = _compute_claim_report_number(claim.id, report.id)

    try:
        setattr(report, "display_report_number", display_report_number)
    except Exception:
        pass

    if HTML is None:
        flash("PDF generation is not available (WeasyPrint is not installed).", "danger")
        return redirect(url_for("main.report_print", claim_id=claim.id, report_id=report.id))

    # Build Case Manager Activities list (must match report_print)
    cm_activities: list[dict] = []
    if report.dos_start and report.dos_end:
        q = (
            BillableItem.query.filter_by(claim_id=claim.id)
            .filter(BillableItem.date_of_service.isnot(None))
            .filter(BillableItem.date_of_service >= report.dos_start)
            .filter(BillableItem.date_of_service <= report.dos_end)
        )
        q = q.filter(BillableItem.activity_code != "EXP")

        items = (
            q.order_by(
                BillableItem.date_of_service.asc().nullslast(),
                BillableItem.created_at.asc(),
            ).all()
        )

        for item in items:
            cm_activities.append(
                {
                    "date_of_service": item.date_of_service,
                    "activity_code": item.activity_code or "",
                    "description": item.description or "",
                    "notes": item.notes or "",
                }
            )

    cm_items = cm_activities  # back-compat

    html = render_template(
        "report_print.html",
        active_page="claims",
        claim=claim,
        report=report,
        settings=settings,
        documents=documents,
        cm_activities=cm_activities,
        cm_items=cm_items,
        barriers=barriers,
        display_report_number=display_report_number,
    )

    pdf_bytes = HTML(string=html, base_url=current_app.root_path).write_pdf()

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"report_claim{claim.id}_report{report.id}.pdf",
    )


@bp.route("/claims/<int:claim_id>/reports/<int:report_id>/delete", methods=["GET", "POST"])
def report_delete(claim_id, report_id):
    """Delete a report and return to the claim detail page."""
    claim = Claim.query.get_or_404(claim_id)
    report = Report.query.filter_by(id=report_id, claim_id=claim.id).first_or_404()

    db.session.delete(report)
    db.session.commit()
    flash("Report deleted successfully.", "success")

    return redirect(url_for("main.claim_detail", claim_id=claim.id))