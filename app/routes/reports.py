

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
import inspect
import json
import os
import sys
import subprocess
import traceback
from pathlib import Path
from datetime import timedelta, datetime, date, time
def to_system_timezone(dt):
    """Convert a datetime or date to system/local timezone if needed (stub for now)."""
    # Replace this with actual timezone logic as needed.
    # For now, just return dt (assume naive datetimes are system-local).
    return dt

# Centralized now() and today() helpers
def system_now():
    """Return the current datetime in system/local timezone."""
    return to_system_timezone(__import__('datetime').datetime.now())

def system_today():
    """Return the current date in system/local timezone."""
    return to_system_timezone(__import__('datetime').date.today())
from collections import defaultdict

from flask import (
    abort,
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
from sqlalchemy import inspect as sa_inspect, text


# Optional Playwright import for server-side Chromium PDF generation.
# If not installed/available, PDF routes should fail gracefully.
try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None

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
    DocumentArtifact,
)

from ..services import ai_service

from . import bp



# ---- helpers (temporary duplicates; will move to routes/helpers.py) ----

# ---- claim-level treating providers (join table; best-effort) ----

def _table_exists(table_name: str) -> bool:
    """Return True if the given table exists in the current DB."""
    try:
        return sa_inspect(db.engine).has_table(table_name)
    except Exception:
        # Be conservative: if inspection fails, assume it exists.
        return True


def _claim_provider_table_name() -> str | None:
    """Best-effort: return the join table name used for claim<->provider.

    Supports multiple historical names to avoid breaking older DBs.
    Expected columns (best case): claim_id, provider_id, sort_order
    """
    candidates = [
        "claim_treating_provider",
        "claim_provider",
        "claim_approved_provider",
    ]
    for t in candidates:
        if _table_exists(t):
            return t
    return None


def _claim_load_provider_ids(claim_id: int) -> list[int]:
    """Return provider IDs for the claim, preserving sort order when available."""
    t = _claim_provider_table_name()
    if not t:
        return []

    try:
        rows = db.session.execute(
            text(
                f"""
                SELECT provider_id
                FROM {t}
                WHERE claim_id = :claim_id
                ORDER BY sort_order NULLS LAST, provider_id
                """
            ),
            {"claim_id": claim_id},
        ).fetchall()
        return [int(r[0]) for r in rows if r and r[0] is not None]
    except Exception:
        # If the first query failed (missing column/table/privileges), the transaction
        # may now be aborted; rollback before attempting any fallback query.
        try:
            db.session.rollback()
        except Exception:
            pass

        try:
            rows = db.session.execute(
                text(
                    f"""
                    SELECT provider_id
                    FROM {t}
                    WHERE claim_id = :claim_id
                    ORDER BY provider_id
                    """
                ),
                {"claim_id": claim_id},
            ).fetchall()
            return [int(r[0]) for r in rows if r and r[0] is not None]
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
            return []



def _claim_load_providers(claim: Claim) -> list[Provider]:
    """Return Provider rows for the claim in join-table order."""
    ids = _claim_load_provider_ids(claim.id)
    if not ids:
        return []
    rows = Provider.query.filter(Provider.id.in_(ids)).all()
    by_id = {p.id: p for p in rows}
    return [by_id.get(pid) for pid in ids if by_id.get(pid)]

# ---- claim-level surgeries (multi-date support) ----

def _claim_surgery_table_name() -> str | None:
    """Best-effort: return the claim surgery table name.

    Historical/dev DBs may have used different names.
    Expected columns: id, claim_id, surgery_date, description, sort_order
    """
    candidates = [
        "claim_surgery_date",  # current
        "claim_surgery",       # older/accidental
        "claim_surgeries",     # possible variant
    ]
    for t in candidates:
        if _table_exists(t):
            return t
    return None


def _claim_load_surgeries(claim: Claim) -> list[dict]:
    """Return ordered surgery rows for a claim (best-effort).

    Supports:
    - Raw join table lookup (legacy/dev DBs)
    - ORM relationship fallback (e.g., claim.surgeries)
    """
    t = _claim_surgery_table_name()

    # --- 1️⃣ Raw table lookup (preferred when table exists) ---
    if t:
        try:
            rows = db.session.execute(
                text(
                    f"""
                    SELECT
                        id,
                        surgery_date,
                        description,
                        sort_order
                    FROM {t}
                    WHERE claim_id = :claim_id
                    ORDER BY sort_order NULLS LAST, surgery_date NULLS LAST, id
                    """
                ),
                {"claim_id": claim.id},
            ).fetchall()

            if rows:
                return [
                    {
                        "id": r[0],
                        "surgery_date": r[1],
                        "description": r[2],
                        "sort_order": r[3],
                    }
                    for r in rows
                ]
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass

    # --- 2️⃣ ORM relationship fallback (if model defines it) ---
    try:
        rel = getattr(claim, "surgeries", None)
        if rel:
            out = []
            for s in rel:
                out.append(
                    {
                        "id": getattr(s, "id", None),
                        "surgery_date": getattr(s, "surgery_date", None),
                        "description": getattr(s, "description", None),
                        "sort_order": getattr(s, "sort_order", None),
                    }
                )
            return sorted(
                out,
                key=lambda x: (
                    x.get("sort_order") or 0,
                    x.get("surgery_date") or date.min,
                    x.get("id") or 0,
                ),
            )
    except Exception:
        pass

    # --- 3️⃣ Legacy single-date fallback (Claim.surgery_date) ---
    try:
        legacy_date = getattr(claim, "surgery_date", None)
        if legacy_date:
            return [
                {
                    "id": None,
                    "surgery_date": legacy_date,
                    "description": None,
                    "sort_order": None,
                }
            ]
    except Exception:
        pass

    return []

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


# ---- PDF filename helper ----

def _build_report_pdf_filename(claim: Claim, report: Report, display_report_number: int | None) -> str:
    """Human-friendly filename for report PDF downloads."""
    # Format: LastName_ClaimNumber_IR_2-12-26.pdf

    # --- Claimant last name only ---
    claimant_full = (getattr(claim, "claimant_name", None) or "").strip()
    last_name = ""
    if claimant_full:
        parts = claimant_full.split()
        if parts:
            last_name = parts[-1]

    last_name_seg = _safe_segment(last_name).strip("_")

    # --- Claim number ---
    claim_no = (getattr(claim, "claim_number", None) or "").strip()
    claim_no_seg = _safe_segment(claim_no).strip("_")

    # --- Report type abbreviation ---
    rt_raw = (getattr(report, "report_type", None) or "").strip().lower()
    if rt_raw == "initial":
        rt_abbrev = "IR"
    elif rt_raw == "progress":
        rt_abbrev = "PR"
    elif rt_raw == "closure":
        rt_abbrev = "CR"
    else:
        rt_abbrev = "R"

    # --- Append display report number (e.g., PR3) ---
    if display_report_number:
        rt_with_number = f"{rt_abbrev}{display_report_number}"
    else:
        rt_with_number = rt_abbrev

    # --- Date (M-D-YY, no leading zeros, not ISO) ---
    d = getattr(report, "dos_end", None) or getattr(report, "created_at", None)
    if hasattr(d, "date"):
        d = d.date()
    from datetime import date as _date  # for type check only
    if not isinstance(d, _date):
        d = system_today()

    year_short = str(d.year)[2:]
    date_seg = f"{d.month}-{d.day}-{year_short}"

    parts = [p for p in [last_name_seg, claim_no_seg, rt_with_number, date_seg] if p]

    if parts:
        filename = "_".join(parts) + ".pdf"
    else:
        filename = f"report_{report.id}.pdf"

    return filename

# --- Playwright PDF rendering helper ---
def _render_pdf_from_url_playwright(url: str) -> bytes:
    """Render the given URL to PDF using headless Chromium (Playwright).

    Notes:
    - This is synchronous and intended for single-user, low-concurrency use.
    - We copy request cookies into the browser context so authenticated print pages render.
    """
    if sync_playwright is None:
        raise RuntimeError("Playwright is not available")

    # Copy current request cookies so /print routes that require auth still render.
    cookies = []
    try:
        for name, value in (request.cookies or {}).items():
            cookies.append({"name": name, "value": value, "url": request.host_url})
    except Exception:
        cookies = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()

        if cookies:
            try:
                context.add_cookies(cookies)
            except Exception:
                pass

        page = context.new_page()
        # Prefer networkidle so CSS/assets finish loading.
        page.goto(url, wait_until="networkidle")

        # Ensure we use print media rules.
        try:
            page.emulate_media(media="print")
        except Exception:
            pass

        pdf_bytes = page.pdf(
            format="Letter",
            print_background=True,
            prefer_css_page_size=True,
            margin={
                "top": "0in",
                "right": "0in",
                "bottom": "0in",
                "left": "0in",
            },
        )

        try:
            page.close()
        except Exception:
            pass
        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass

    return pdf_bytes

# New route: artifact download
@bp.route("/artifacts/<int:artifact_id>/download")
def artifact_download(artifact_id: int):
    """Download a stored PDF artifact from the database."""
    art = DocumentArtifact.query.get_or_404(artifact_id)

    data = getattr(art, "data", None)
    if data is None:
        # Back-compat if the column was named differently
        data = getattr(art, "content", None)

    if not data:
        flash("Artifact file is missing.", "danger")
        return redirect(url_for("main.claims_list"))

    filename = getattr(art, "download_filename", None) or getattr(art, "filename", None) or "document.pdf"
    content_type = getattr(art, "content_type", None) or "application/pdf"

    return send_file(
        io.BytesIO(data),
        mimetype=content_type,
        as_attachment=True,
        download_name=filename,
    )


# ---- Tab title helpers ----

def _claimant_last_first(name: str | None) -> str:
    """Return claimant name formatted as 'Last, First' when possible."""
    if not name:
        return ""

    s = " ".join((name or "").strip().split())
    if not s:
        return ""

    # If it already looks like "Last, First", keep it.
    if "," in s:
        return s

    parts = s.split(" ")
    if len(parts) == 1:
        return parts[0]

    first = parts[0]
    last = parts[-1]
    middle = " ".join(parts[1:-1]).strip()

    if middle:
        return f"{last}, {first} {middle}"

    return f"{last}, {first}"


def _build_report_page_title(claim: Claim, report: Report, display_report_number: int | None) -> str:
    """Browser tab title for report pages."""
    claimant = _claimant_last_first(getattr(claim, "claimant_name", None))
    claim_no = (getattr(claim, "claim_number", None) or "").strip()

    rt = (getattr(report, "report_type", None) or "").strip().title() or "Report"

    if display_report_number:
        report_part = f"{rt} Report #{display_report_number}"
    else:
        report_part = f"{rt} Report"

    parts = [p for p in [claimant, claim_no, report_part] if p]
    if parts:
        return " - ".join(parts) + " - Impact CMS"

    return "Impact CMS"



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

        return __import__('datetime').date(year, month, day), None
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


def _find_overlapping_reports(
    claim_id: int,
    dos_start: date | None,
    dos_end: date | None,
    exclude_report_id: int | None = None,
) -> list[Report]:
    """Return reports on the same claim whose DOS range overlaps [dos_start, dos_end].

    Overlap rule (inclusive): existing.dos_start <= dos_end AND existing.dos_end >= dos_start

    Notes:
    - Only enforces overlap checks when BOTH dates are present on BOTH reports.
      (If an existing report has missing DOS dates, we skip it rather than blocking edits.)
    """
    if not dos_start or not dos_end:
        return []

    q = Report.query.filter(Report.claim_id == claim_id)
    if exclude_report_id is not None:
        q = q.filter(Report.id != exclude_report_id)

    # Only compare against reports that have both dates.
    q = q.filter(Report.dos_start.isnot(None)).filter(Report.dos_end.isnot(None))

    # Inclusive overlap: start <= other_end AND end >= other_start
    q = q.filter(Report.dos_start <= dos_end).filter(Report.dos_end >= dos_start)

    # Order for stable messaging
    return q.order_by(
        Report.dos_start.asc().nullslast(),
        Report.dos_end.asc().nullslast(),
        Report.id.asc(),
    ).all()

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

    today = system_today()

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
        if created is not None:
            if hasattr(created, "date"):
                created = created.date()
            created = to_system_timezone(created)
        if referral := getattr(claim, "referral_date", None):
            if hasattr(referral, "date"):
                referral = referral.date()
            referral = to_system_timezone(referral)
        if created is not None:
            dos_start = created
        elif referral is not None:
            dos_start = referral
        else:
            dos_start = today
        dos_end = today
    else:
        # Progress/Closure: DOS start is the day AFTER the last submitted report.
        # Prefer last_report.dos_end; fall back to last_report.created_at; else today.
        last_end = getattr(last_report, "dos_end", None) if last_report else None
        if last_end is not None:
            if hasattr(last_end, "date"):
                last_end = last_end.date()
            last_end = to_system_timezone(last_end)
        if last_end is not None:
            dos_start = last_end + timedelta(days=1)
        else:
            last_created = getattr(last_report, "created_at", None) if last_report else None
            if last_created is not None:
                if hasattr(last_created, "date"):
                    last_created = last_created.date()
                last_created = to_system_timezone(last_created)
                dos_start = last_created + timedelta(days=1)
            else:
                dos_start = today
        dos_end = today

    # Ensure all datetimes are system-local
    dos_start = to_system_timezone(dos_start)
    dos_end = to_system_timezone(dos_end)

    # Prevent overlapping DOS ranges for reports on the same claim.
    if dos_start and dos_end and dos_start > dos_end:
        flash("DOS Start cannot be after DOS End.", "danger")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    overlaps = _find_overlapping_reports(claim.id, dos_start, dos_end)
    if overlaps:
        parts = []
        for r in overlaps[:3]:
            rt = (r.report_type or "report").title()
            parts.append(f"{rt} #{r.id} ({r.dos_start.strftime('%m/%d/%Y')}–{r.dos_end.strftime('%m/%d/%Y')})")
        more = "" if len(overlaps) <= 3 else f" (+{len(overlaps) - 3} more)"
        flash(
            "Report dates overlap an existing report on this claim: " + ", ".join(parts) + more,
            "danger",
        )
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

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


    # Treating Providers are claim-owned.
    # Keep legacy single provider pointing to the first claim provider (best-effort)
    # so older templates/ICS/location logic still work.
    claim_provider_ids = _claim_load_provider_ids(claim.id)
    if claim_provider_ids:
        report.treating_provider_id = claim_provider_ids[0]

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

    display_report_number = _compute_claim_report_number(claim.id, report.id)
    try:
        setattr(report, "display_report_number", display_report_number)
    except Exception:
        pass

    page_title = _build_report_page_title(claim, report, display_report_number)

    claim_providers = _claim_load_providers(claim)
    claim_surgeries = _claim_load_surgeries(claim)

    # Standardize time display for key fields
    if hasattr(report, "initial_next_appt_datetime"):
        report.initial_next_appt_datetime = to_system_timezone(report.initial_next_appt_datetime)
    if hasattr(report, "dos_start"):
        report.dos_start = to_system_timezone(report.dos_start)
    if hasattr(report, "dos_end"):
        report.dos_end = to_system_timezone(report.dos_end)

    return render_template(
        "report_detail.html",
        active_page="claims",
        claim=claim,
        report=report,
        claim_providers=claim_providers,
        settings=settings,
        barriers_by_category=barriers_by_category,
        selected_barrier_ids=selected_barrier_ids,
        page_title=page_title,
        display_report_number=display_report_number,
        report_display_number=display_report_number,
        claim_surgeries=claim_surgeries,
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
        document_date=document_date or system_today().isoformat(),
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


# ---- AI Draft Field Prompt Route ----
@bp.route(
    "/claims/<int:claim_id>/reports/<int:report_id>/ai-draft/<string:field_name>",
    methods=["GET", "POST"],
)
def report_ai_draft_field(claim_id, report_id, field_name):
    """AI draft/assist for a single report field.

    GET: Return the assembled prompt for preview/inspection (no model call).
    POST: Generate a draft using the AI provider (returns generated text).
    """
    claim = Claim.query.get_or_404(claim_id)
    report = Report.query.filter_by(id=report_id, claim_id=claim.id).first_or_404()
    # Current field value (used as context so the model can revise/extend instead of writing blind)
    current_value = getattr(report, field_name, "") or ""

    # Keep this aligned with roll-forward: only allow multi-line/long-text fields.
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

    settings = Settings.query.first()
    ai_enabled = bool(getattr(settings, "ai_enabled", False)) if settings else False
    if not ai_enabled:
        return jsonify({"error": "AI is disabled in Settings."}), 403

    # Optional freeform guidance from the user (e.g. "Write in Gina's tone, mention latest ortho visit")
    user_prompt = ""
    if request.method == "POST":
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            user_prompt = (payload.get("user_prompt") or "").strip()
        else:
            user_prompt = (request.form.get("user_prompt") or "").strip()

    # ---- GET: return the assembled prompt for inspection/debug (no model call) ----
    if request.method == "GET":
        try:
            def _call_prompt_builder(**kwargs):
                """Call ai_service.build_report_field_draft_prompt with only supported kwargs.

                This makes the route resilient when ai_service evolves and avoids 500s from
                unexpected keyword arguments.
                """
                fn = ai_service.build_report_field_draft_prompt
                sig = inspect.signature(fn)
                allowed = set(sig.parameters.keys())
                filtered = {k: v for k, v in kwargs.items() if k in allowed}
                return fn(**filtered)

            prompt = _call_prompt_builder(
                claim_id=claim.id,
                report_id=report.id,
                field_name=field_name,
                user_prompt=user_prompt,
                settings=settings,
                current_value=current_value,
                prompt_only=True,
            )

        except Exception as e:
            current_app.logger.exception("AI draft prompt build failed")
            payload = {
                "ok": False,
                "error": "AI draft prompt build failed",
                "detail": str(e),
                "type": e.__class__.__name__,
            }
            if current_app.debug:
                payload["traceback"] = traceback.format_exc()
            return jsonify(payload), 500

        # Normalize response contract
        if isinstance(prompt, dict):
            return jsonify(prompt), 200
        return jsonify({"ok": True, "prompt": str(prompt)}), 200

    # ---- POST: generate a draft (may be stubbed until provider is wired) ----
    try:
        fn = getattr(ai_service, "generate_report_field", None)
        if fn is None:
            raise NotImplementedError("AI draft generation is not wired yet.")

        def _call_generator(**kwargs):
            fn2 = fn
            sig2 = inspect.signature(fn2)
            allowed2 = set(sig2.parameters.keys())
            filtered2 = {k: v for k, v in kwargs.items() if k in allowed2}
            return fn2(**filtered2)

        text = _call_generator(
            claim_id=claim.id,
            report_id=report.id,
            field_name=field_name,
            user_prompt=user_prompt,
            settings=settings,
            current_value=current_value,
        )
        return jsonify({"ok": True, "text": text}), 200

    except NotImplementedError as e:
        return jsonify({"ok": False, "error": str(e)}), 501
    except Exception as e:
        current_app.logger.exception("AI draft generation failed")
        payload = {
            "ok": False,
            "error": "AI draft generation failed",
            "detail": str(e),
            "type": e.__class__.__name__,
        }
        if current_app.debug:
            payload["traceback"] = traceback.format_exc()
        return jsonify(payload), 500


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

    page_title = _build_report_page_title(claim, report, display_report_number)
    claim_surgeries = _claim_load_surgeries(claim)

    # --- PCP / Family Doctor field back-compat ---
    # DB/schema may store this as `initial_primary_care_provider` (preferred) but
    # older templates/logic may still reference `primary_care_provider`.
    if hasattr(report, "initial_primary_care_provider"):
        try:
            # Provide a runtime alias for templates expecting `report.primary_care_provider`
            setattr(report, "primary_care_provider", getattr(report, "initial_primary_care_provider") or None)
        except Exception:
            pass

    error = None

    if request.method == "POST":
        report_type_raw = (request.form.get("report_type") or "").strip().lower()
        report_type = report_type_raw if report_type_raw else (report.report_type or "").lower()

        dos_start_raw = (request.form.get("dos_start") or "").strip() or None
        dos_end_raw = (request.form.get("dos_end") or "").strip() or None
        work_status = (request.form.get("work_status") or "").strip() or None
        case_management_plan = (request.form.get("case_management_plan") or "").strip() or None
        next_report_due_raw = (request.form.get("next_report_due") or "").strip() or None

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

        # Next appointment (initial report): split date and time fields
        initial_next_appt_date_raw = (request.form.get("initial_next_appt_date") or "").strip()
        initial_next_appt_time_raw = (request.form.get("initial_next_appt_time") or "").strip()
        initial_next_appt_datetime_raw = (request.form.get("initial_next_appt_datetime") or "").strip()
        # Explicitly assign initial_next_appt_provider_id and name from form, parse as int or None
        raw_initial_next_appt_provider_id = request.form.get("initial_next_appt_provider_id", "").strip()
        if raw_initial_next_appt_provider_id == "":
            initial_next_appt_provider_id = None
        else:
            try:
                initial_next_appt_provider_id = int(raw_initial_next_appt_provider_id)
            except Exception:
                initial_next_appt_provider_id = None
        initial_next_appt_notes = (request.form.get("initial_next_appt_notes") or "").strip() or None
        # Optionally, for display purposes, keep the provider name (resolved below)
        raw_initial_next_appt_provider_name = request.form.get("initial_next_appt_provider_name", "").strip()
        initial_next_appt_provider_name = raw_initial_next_appt_provider_name or None

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
            dos_start = to_system_timezone(dos_start)

        if dos_end_raw:
            dos_end, err = _parse_mmddyyyy(dos_end_raw, "DOS End")
            if err and not error:
                error = err
            dos_end = to_system_timezone(dos_end)

        if next_report_due_raw:
            next_report_due, err = _parse_mmddyyyy(next_report_due_raw, "Next Report Due")
            if err and not error:
                error = err
            next_report_due = to_system_timezone(next_report_due)

        valid_types = {"initial", "progress", "closure"}
        if not report_type or report_type not in valid_types:
            error = "Report type is required."

        if not error and dos_start and dos_end and dos_start > dos_end:
            error = "DOS Start cannot be after DOS End."

        if not error:
            overlaps = _find_overlapping_reports(claim.id, dos_start, dos_end, exclude_report_id=report.id)
            if overlaps:
                parts = []
                for r in overlaps[:3]:
                    rt = (r.report_type or "report").title()
                    parts.append(f"{rt} #{r.id} ({r.dos_start.strftime('%m/%d/%Y')}–{r.dos_end.strftime('%m/%d/%Y')})")
                more = "" if len(overlaps) <= 3 else f" (+{len(overlaps) - 3} more)"
                error = "Report dates overlap an existing report on this claim: " + ", ".join(parts) + more

        if error:
            # NOTE: Do not `flash()` here because the template already renders the
            # `error` variable. Flashing would duplicate the message (banner + toast).
            pass
        else:
            report.report_type = report_type
            report.dos_start = dos_start
            report.dos_end = dos_end
            report.work_status = work_status
            report.case_management_plan = case_management_plan
            claim.next_report_due = next_report_due
            db.session.add(claim)

            # Remove fallback that sets treating_provider_id to avoid overwriting
            # report.treating_provider_id assignment is not performed here

            report.status_treatment_plan = status_treatment_plan
            report.employment_status = employment_status

            # Persist initial-style clinical fields for all report types
            report.initial_diagnosis = initial_diagnosis
            report.initial_mechanism_of_injury = initial_mechanism_of_injury
            report.initial_coexisting_conditions = initial_coexisting_conditions
            report.initial_surgical_history = initial_surgical_history
            report.initial_medications = initial_medications
            report.initial_diagnostics = initial_diagnostics

            # --- Next appointment: parse separate date (MM/DD/YYYY) and time (h:mm AM/PM) ---
            dt_val = None

            if initial_next_appt_date_raw or initial_next_appt_time_raw:
                appt_date = None
                appt_time = None

                # Parse MM/DD/YYYY
                if initial_next_appt_date_raw:
                    try:
                        appt_date = datetime.strptime(
                            initial_next_appt_date_raw, "%m/%d/%Y"
                        ).date()
                    except Exception:
                        appt_date = None

                # Parse 12-hour time with AM/PM (e.g., 3:45 PM)
                if initial_next_appt_time_raw:
                    try:
                        appt_time = datetime.strptime(
                            initial_next_appt_time_raw, "%I:%M %p"
                        ).time()
                    except Exception:
                        appt_time = None

                if appt_date and appt_time:
                    dt_val = datetime.combine(appt_date, appt_time)
                elif appt_date:
                    dt_val = datetime.combine(appt_date, time(0, 0))
                elif appt_time:
                    dt_val = datetime.combine(system_today(), appt_time)

                report.initial_next_appt_datetime = (
                    to_system_timezone(dt_val) if dt_val else None
                )

            else:
                report.initial_next_appt_datetime = None
            # Persist notes as before
            report.initial_next_appt_notes = initial_next_appt_notes

            # Explicitly assign initial_next_appt_provider_id and name from form to model
            report.initial_next_appt_provider_id = initial_next_appt_provider_id
            report.initial_next_appt_provider_name = initial_next_appt_provider_name

            # If provider_id is set, optionally resolve and update provider name (overriding if necessary)
            if initial_next_appt_provider_id is not None:
                provider_row = Provider.query.get(initial_next_appt_provider_id)
                if provider_row and getattr(provider_row, "name", None):
                    report.initial_next_appt_provider_name = (provider_row.name or "").strip()
            elif initial_next_appt_provider_name == "":
                report.initial_next_appt_provider_name = None

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

    claim_providers = _claim_load_providers(claim)
    # Standardize time display for key fields
    if hasattr(report, "initial_next_appt_datetime"):
        report.initial_next_appt_datetime = to_system_timezone(report.initial_next_appt_datetime)
    if hasattr(report, "dos_start"):
        report.dos_start = to_system_timezone(report.dos_start)
    if hasattr(report, "dos_end"):
        report.dos_end = to_system_timezone(report.dos_end)
    return render_template(
        "report_edit.html",
        active_page="claims",
        claim=claim,
        report=report,
        claim_providers=claim_providers,
        claim_surgeries=claim_surgeries,
        error=error,
        barriers_by_category=barriers_by_category,
        selected_barrier_ids=selected_barrier_ids,
        display_report_number=display_report_number,
        page_title=page_title,
        report_display_number=display_report_number,
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

    # Default behavior: open in-browser when possible (PDF/images), not forced download.
    # Use ?download=1 to force attachment.
    download_raw = (request.args.get("download") or "").strip().lower()
    force_download = download_raw in {"1", "true", "yes"}

    resp = send_file(
        file_path,
        as_attachment=force_download,
        download_name=doc.original_filename,
    )

    # Help browsers treat this as inline when not forcing download.
    if not force_download and doc.original_filename:
        resp.headers["Content-Disposition"] = f'inline; filename="{doc.original_filename}"'

    return resp


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


@bp.route("/reports/<int:report_id>/appointment.ics")
def report_appointment_ics_short(report_id):
    """Short ICS route used by dashboard (no claim_id in URL)."""
    report = Report.query.get_or_404(report_id)
    return redirect(
        url_for(
            "main.report_next_appointment_ics",
            claim_id=report.claim_id,
            report_id=report.id,
        )
    )


@bp.route("/claims/<int:claim_id>/reports/<int:report_id>/next-appointment.ics")
def report_next_appointment_ics(claim_id, report_id):
    """Generate a simple ICS calendar event for the report's next appointment."""
    claim = Claim.query.get_or_404(claim_id)
    report = Report.query.filter_by(id=report_id, claim_id=claim.id).first_or_404()

    if not report.initial_next_appt_datetime:
        flash("This report does not have a next appointment date/time set.", "warning")
        return redirect(url_for("main.report_edit", claim_id=claim.id, report_id=report.id))

    dt = to_system_timezone(report.initial_next_appt_datetime)

    # Resolve provider for location/address.
    # Prefer the explicitly selected next-appointment provider (claim-owned) when present.
    provider_for_location = None
    try:
        selected_name = (report.initial_next_appt_provider_name or "").strip()
        if selected_name:
            cp_ids = _claim_load_provider_ids(claim.id)
            if cp_ids:
                cp_rows = Provider.query.filter(Provider.id.in_(cp_ids)).all()
                for p in cp_rows:
                    if (getattr(p, "name", None) or "").strip() == selected_name:
                        provider_for_location = p
                        break
    except Exception:
        provider_for_location = None

    # Fallback to legacy single provider relationship.
    if provider_for_location is None and getattr(report, "treating_provider", None):
        provider_for_location = report.treating_provider

    # Build a maps-friendly LOCATION (street address only)
    address_parts: list[str] = []
    display_name_parts: list[str] = []

    if provider_for_location is not None:
        p = provider_for_location

        # Provider display name (goes in DESCRIPTION, not LOCATION)
        org = (getattr(p, "organization", None) or "").strip()
        nm = (getattr(p, "name", None) or "").strip()
        if org and nm:
            display_name_parts.append(f"{org} — {nm}")
        elif nm:
            display_name_parts.append(nm)
        elif org:
            display_name_parts.append(org)

        # Street address for maps
        addr1 = getattr(p, "address1", None) or getattr(p, "address", None)
        addr2 = getattr(p, "address2", None)

        if addr1:
            address_parts.append(addr1.strip())
        if addr2:
            address_parts.append(addr2.strip())

        city = getattr(p, "city", None)
        state = getattr(p, "state", None)
        postal = getattr(p, "postal_code", None) or getattr(p, "zip", None)

        city_state_zip = ", ".join(part for part in [city, state, postal] if part)
        if city_state_zip:
            address_parts.append(city_state_zip.strip())

    location = ", ".join(address_parts) if address_parts else "TBD"

    summary = f"Next appointment – {claim.claimant_name or 'Claimant'}"
    description_lines = [
        f"Claim: {claim.claim_number or ''}",
        f"Claimant: {claim.claimant_name or ''}",
    ]

    if display_name_parts:
        description_lines.append(f"Provider: {display_name_parts[0]}")
    description = "\\n".join(description_lines)

    # Use system_now() for dtstamp in system-local timezone, then convert to UTC for ICS.
    now_dt = system_now()
    dtstamp = now_dt.astimezone(__import__('datetime').timezone.utc).strftime("%Y%m%dT%H%M%SZ")
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
    # Defensive: clear any aborted transaction from earlier failures in this request
    try:
        db.session.rollback()
    except Exception:
        pass
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


    generated_at = system_now()

    page_title = _build_report_page_title(claim, report, display_report_number)

    claim_providers = _claim_load_providers(claim)
    claim_surgeries = _claim_load_surgeries(claim)

    cm_activities: list[dict] = []
    # Standardize time display for dos_start/dos_end
    dos_start = to_system_timezone(report.dos_start)
    dos_end = to_system_timezone(report.dos_end)
    if dos_start and dos_end:
        q = (
            BillableItem.query.filter_by(claim_id=claim.id)
            .filter(BillableItem.date_of_service.isnot(None))
            .filter(BillableItem.date_of_service >= dos_start)
            .filter(BillableItem.date_of_service <= dos_end)
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

    # Standardize time display for key fields
    if hasattr(report, "initial_next_appt_datetime"):
        report.initial_next_appt_datetime = to_system_timezone(report.initial_next_appt_datetime)
    if hasattr(report, "dos_start"):
        report.dos_start = to_system_timezone(report.dos_start)
    if hasattr(report, "dos_end"):
        report.dos_end = to_system_timezone(report.dos_end)

    return render_template(
        "report_print.html",
        active_page="claims",
        settings=settings,
        claim=claim,
        report=report,
        claim_providers=claim_providers,
        cm_activities=cm_activities,
        cm_items=cm_items,
        barriers=barriers,
        display_report_number=display_report_number,
        page_title=page_title,
        report_display_number=display_report_number,
        claim_surgeries=claim_surgeries,
        generated_at=generated_at,
    )



@bp.route("/claims/<int:claim_id>/reports/<int:report_id>/pdf")
def report_pdf(claim_id, report_id):
    """Generate a PDF of the report by snapshotting the /print route via headless Chromium."""
    # Defensive: clear any aborted transaction from earlier failures in this request
    try:
        db.session.rollback()
    except Exception:
        pass

    claim = Claim.query.get_or_404(claim_id)
    report = Report.query.filter_by(id=report_id, claim_id=claim.id).first_or_404()

    display_report_number = _compute_claim_report_number(claim.id, report.id)
    try:
        setattr(report, "display_report_number", display_report_number)
    except Exception:
        pass

    # Canonical filename
    filename = _build_report_pdf_filename(claim, report, display_report_number)

    regen_raw = (request.args.get("regen") or "").strip().lower()
    regen = regen_raw in {"1", "true", "yes"}

    view_raw = (request.args.get("view") or "").strip().lower()
    view = view_raw in {"1", "true", "yes"}

    # Determine last-modified time of source data
    report_updated = getattr(report, "updated_at", None) or getattr(report, "created_at", None)
    # Ensure any datetime is system-local for consistency (for mtime comparison)
    report_updated = to_system_timezone(report_updated)

    provider_updated = None
    try:
        provider_ids = _claim_load_provider_ids(claim.id)
        if provider_ids:
            prov_rows = Provider.query.filter(Provider.id.in_(provider_ids)).all()
            prov_times = []
            for p in prov_rows:
                t = getattr(p, "updated_at", None) or getattr(p, "created_at", None)
                t = to_system_timezone(t)
                if t is not None:
                    prov_times.append(t)
            if prov_times:
                provider_updated = max(prov_times)
    except Exception:
        provider_updated = None

    effective_updated = report_updated
    if provider_updated is not None:
        try:
            if effective_updated is None or provider_updated > effective_updated:
                effective_updated = provider_updated
        except Exception:
            pass

    # Filesystem target path
    report_folder = _get_report_folder(report)
    pdf_path = report_folder / filename

    def _send_pdf_from_disk(path: Path):
        resp = send_file(
            path,
            mimetype="application/pdf",
            as_attachment=not view,
            download_name=filename,
        )
        if view:
            resp.headers["Content-Disposition"] = f'inline; filename="{filename}"'
        return resp

    # Reuse existing file if fresh
    if (not regen) and pdf_path.exists():
        is_fresh = True
        if effective_updated is not None:
            try:
                mtime = to_system_timezone(__import__('datetime').datetime.fromtimestamp(pdf_path.stat().st_mtime))
                is_fresh = mtime >= effective_updated
            except Exception:
                is_fresh = True
        if is_fresh:
            return _send_pdf_from_disk(pdf_path)

    # Generate new PDF via Playwright
    print_url = url_for(
        "main.report_print",
        claim_id=claim.id,
        report_id=report.id,
        _external=True,
    )

    try:
        pdf_bytes = _render_pdf_from_url_playwright(print_url)
    except Exception as e:
        current_app.logger.exception("Report PDF generation failed")
        flash(f"PDF generation failed: {e}", "danger")
        return redirect(url_for("main.report_print", claim_id=claim.id, report_id=report.id))

    # Persist to disk atomically
    try:
        report_folder.mkdir(parents=True, exist_ok=True)
        tmp_path = report_folder / (filename + ".tmp")
        with open(tmp_path, "wb") as f:
            f.write(pdf_bytes)
        os.replace(tmp_path, pdf_path)
    except Exception as e:
        current_app.logger.exception("Report PDF save-to-disk failed")
        flash(f"PDF save failed: {e}", "danger")
        # Fallback: stream generated bytes even if disk write fails
        resp = send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=not view,
            download_name=filename,
        )
        if view:
            resp.headers["Content-Disposition"] = f'inline; filename="{filename}"'
        return resp

    # Store metadata only (no blob) in DB
    try:
        art = DocumentArtifact(
            claim_id=claim.id,
            report_id=report.id,
            artifact_type="report_pdf",
            content_type="application/pdf",
            download_filename=filename,
            file_size_bytes=int(pdf_path.stat().st_size),
            storage_backend="fs",
            created_at=system_now(),
        )
        if hasattr(art, "stored_path"):
            art.stored_path = str(pdf_path)
        if hasattr(art, "content"):
            art.content = None
        if hasattr(art, "data"):
            art.data = None

        db.session.add(art)
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass

    return _send_pdf_from_disk(pdf_path)



# ---- PDF Preview Route ----
@bp.route("/claims/<int:claim_id>/reports/<int:report_id>/pdf/preview")
def report_pdf_preview(claim_id, report_id):
    """
    Inline PDF preview endpoint for iframe rendering.

    This wraps the canonical report_pdf route but forces:
      - view=1 (inline display)
      - no forced download
      - optional regen support via ?regen=1
    """
    regen_raw = (request.args.get("regen") or "").strip().lower()
    regen = regen_raw in {"1", "true", "yes"}

    return redirect(
        url_for(
            "main.report_pdf",
            claim_id=claim_id,
            report_id=report_id,
            regen=1 if regen else None,
            view=1,
        )
    )


@bp.route("/claims/<int:claim_id>/reports/<int:report_id>/pdf/regenerate", methods=["POST"])
def report_pdf_regenerate(claim_id, report_id):
    """Force regeneration of the report PDF artifact, then open the PDF inline."""
    # We reuse the existing PDF route with `regen=1` and `view=1`.
    return redirect(
        url_for(
            "main.report_pdf",
            claim_id=claim_id,
            report_id=report_id,
            regen=1,
            view=1,
        )
    )


# ------------------------
# Report Email Preview / Send
# ------------------------

@bp.route("/claims/<int:claim_id>/reports/<int:report_id>/email", methods=["GET", "POST"])
def report_email(claim_id, report_id):
    """Preview and send report email using stored SMTP + templates."""
    from ..services.email_service import (
        build_email_context,
        render_email_template,
        send_smtp_email,
    )

    claim = Claim.query.get_or_404(claim_id)
    report = Report.query.filter_by(id=report_id, claim_id=claim.id).first_or_404()
    settings = _ensure_settings()

    # Always regenerate PDF filename for tokens
    display_report_number = _compute_claim_report_number(claim.id, report.id)
    pdf_filename = _build_report_pdf_filename(claim, report, display_report_number)

    # Load claim-level surgeries for preview/PDF consistency
    claim_surgeries = _claim_load_surgeries(claim)

    # Build token context (resilient to signature changes)
    try:
        import inspect

        fn = build_email_context
        sig = inspect.signature(fn)
        allowed = set(sig.parameters.keys())

        kwargs = {
            "settings": settings,
            "report": report,
            "pdf_filename": pdf_filename,
            "report_number": display_report_number,
        }

        filtered_kwargs = {k: v for k, v in kwargs.items() if k in allowed}
        context = fn(**filtered_kwargs)

    except Exception:
        current_app.logger.exception("Failed to build email context")
        context = {}

    # Default template fields from Settings (canonical fields)
    subject_template = (
        getattr(settings, "report_email_subject_template", None)
        or "Report for {{ claimant_last_name }}, {{ claimant_first_name }}"
    )
    body_template = (
        getattr(settings, "report_email_body_template", None)
        or "Attached please find the requested report."
    )

    # Allow editing in preview
    subject = render_email_template(subject_template, context)
    body = render_email_template(body_template, context)

    # Default recipient (GET): auto-fill from claim.carrier_contact (adjuster)
    default_to_email = ""
    try:
        adjuster = getattr(claim, "carrier_contact", None)
        if adjuster and getattr(adjuster, "email", None):
            default_to_email = (adjuster.email or "").strip()
    except Exception:
        default_to_email = ""

    to_email = request.form.get("to_email") if request.method == "POST" else default_to_email

    if request.method == "POST":
        action = request.form.get("action")

        # Regenerate preview from current Settings templates (ignore posted edits)
        if action == "regenerate":
            subject_template = (
                getattr(settings, "report_email_subject_template", None)
                or subject_template
            )
            body_template = (
                getattr(settings, "report_email_body_template", None)
                or body_template
            )

            subject = render_email_template(subject_template, context)
            body = render_email_template(body_template, context)

        # Send email
        if action == "send":
            subject = request.form.get("subject") or subject
            body = request.form.get("body") or body
            to_email = request.form.get("to_email") or ""

            if not to_email:
                flash("Recipient email is required.", "danger")
            else:
                try:
                    # Ensure PDF exists on disk (regenerate if necessary)
                    pdf_response = report_pdf(claim.id, report.id)
                    # We do not use the response object; calling ensures file is generated if missing.

                    # Resolve filesystem path to PDF
                    report_folder = _get_report_folder(report)
                    pdf_path = report_folder / pdf_filename

                    if not pdf_path.exists():
                        raise RuntimeError("Report PDF file not found on disk.")

                    # Read PDF bytes for attachment
                    with open(pdf_path, "rb") as f:
                        pdf_bytes = f.read()

                    send_smtp_email(
                        to_email=to_email,
                        subject=subject,
                        body=body,
                        settings=settings,
                        attachments=[
                            (
                                pdf_filename,
                                pdf_bytes,
                                "application/pdf",
                            )
                        ],
                    )

                    flash("Email sent successfully.", "success")
                    return redirect(
                        url_for(
                            "main.report_edit",
                            claim_id=claim.id,
                            report_id=report.id,
                        )
                    )

                except Exception as e:
                    current_app.logger.exception("Email send failed")
                    flash(f"Email failed: {e}", "danger")

    # Standardize time display for key fields
    if hasattr(report, "initial_next_appt_datetime"):
        report.initial_next_appt_datetime = to_system_timezone(report.initial_next_appt_datetime)
    if hasattr(report, "dos_start"):
        report.dos_start = to_system_timezone(report.dos_start)
    if hasattr(report, "dos_end"):
        report.dos_end = to_system_timezone(report.dos_end)
    return render_template(
        "report_email_preview.html",
        claim=claim,
        report=report,
        subject=subject,
        body=body,
        to_email=to_email,
        subject_template=subject_template,
        body_template=body_template,
        display_report_number=display_report_number,
        claim_surgeries=claim_surgeries,
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