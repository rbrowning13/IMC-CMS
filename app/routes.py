import os
import uuid
import sys
import subprocess
from pathlib import Path
from datetime import date, datetime, timedelta
import json
from collections import defaultdict
import io
import re


from sqlalchemy.exc import IntegrityError

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    current_app,
    send_from_directory,
    send_file,
    flash,  # <-- added flash
    abort,
    jsonify,
    make_response,
)
from werkzeug.utils import secure_filename
from markupsafe import Markup, escape  # <-- ADD THIS LINE

# Optional WeasyPrint import for PDF generation
# On some systems (or missing native deps) importing WeasyPrint can raise
# non-ImportError exceptions (e.g., OSError when libgobject/pango aren't found),
# so we catch any Exception here and fall back to HTML = None. Routes that
# need PDF generation should check `if HTML is None` and degrade gracefully.
try:
    from weasyprint import HTML
except Exception:
    HTML = None

from .extensions import db
from .models import (
    Carrier,
    Claim,
    Employer,
    Contact,
    Provider,
    BillableItem,
    Invoice,
    Report,
    ReportDocument,
    ClaimDocument,
    Settings,
    BarrierOption,
    BillingActivityCode,
)

bp = Blueprint("main", __name__)

# ---- Billable activity definitions (canonical seed: code, label, sort_order) ----
BILLABLE_ACTIVITY_SEED = [
    ("ADMIN", "Administrative work", 10),
    ("EMAIL", "Email correspondence", 20),
    ("FAX", "Fax correspondence", 30),
    ("TEXT", "Text messaging", 40),
    ("TC", "Telephone call", 50),
    ("TCM", "Telephonic case management", 60),
    ("MTG", "Meetings / conferences", 70),
    ("MR", "Medical record review", 80),
    ("FR", "File / chart review", 90),
    ("GDL", "Guideline / policy review", 100),
    ("LTR", "Letter drafting / dictation", 110),
    ("RPT", "Report writing / documentation", 120),
    ("DOC", "Other document preparation / review", 130),
    ("TRV", "Travel time", 140),
    ("MIL", "Mileage (miles)", 150),
    ("EXP", "Expenses (dollars)", 160),
    ("WAIT", "Waiting time", 170),
    ("NO BILL", "Non-billable activity", 999),
]

# Simple (code, label) list used by forms as a fallback
BILLABLE_ACTIVITY_CHOICES = [
    (code, label) for code, label, _ in BILLABLE_ACTIVITY_SEED
]

# ---- Contact roles (editable via Settings) ----
CONTACT_ROLE_DEFAULTS = [
    "Adjuster",
    "Nurse Case Manager",
    "Claims Representative",
    "HR / Employer Contact",
    "Billing",
    "Attorney",
    "Provider Office Contact",
    "Other",
]

def _get_contact_roles():
    """
    Load contact roles from Settings.contact_roles_json, falling back
    to CONTACT_ROLE_DEFAULTS if not set or invalid.
    """
    settings = _ensure_settings()
    raw = getattr(settings, "contact_roles_json", None)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                roles = [str(r).strip() for r in data if str(r).strip()]
                if roles:
                    return roles
        except (TypeError, ValueError):
            pass
    return CONTACT_ROLE_DEFAULTS[:]

# ---------- helpers ----------

@bp.app_context_processor
def inject_state_options():
    """
    Make a state_options() helper available in all templates.

    Usage in Jinja:
      <select name="state">
        {{ state_options(current_state_code) }}
      </select>
    """
    def state_options(selected=None):
        states = [
            ("", "—"),
            ("AL", "AL"),
            ("AK", "AK"),
            ("AZ", "AZ"),
            ("AR", "AR"),
            ("CA", "CA"),
            ("CO", "CO"),
            ("CT", "CT"),
            ("DE", "DE"),
            ("FL", "FL"),
            ("GA", "GA"),
            ("HI", "HI"),
            ("ID", "ID"),
            ("IL", "IL"),
            ("IN", "IN"),
            ("IA", "IA"),
            ("KS", "KS"),
            ("KY", "KY"),
            ("LA", "LA"),
            ("ME", "ME"),
            ("MD", "MD"),
            ("MA", "MA"),
            ("MI", "MI"),
            ("MN", "MN"),
            ("MS", "MS"),
            ("MO", "MO"),
            ("MT", "MT"),
            ("NE", "NE"),
            ("NV", "NV"),
            ("NH", "NH"),
            ("NJ", "NJ"),
            ("NM", "NM"),
            ("NY", "NY"),
            ("NC", "NC"),
            ("ND", "ND"),
            ("OH", "OH"),
            ("OK", "OK"),
            ("OR", "OR"),
            ("PA", "PA"),
            ("RI", "RI"),
            ("SC", "SC"),
            ("SD", "SD"),
            ("TN", "TN"),
            ("TX", "TX"),
            ("UT", "UT"),
            ("VT", "VT"),
            ("VA", "VA"),
            ("WA", "WA"),
            ("WV", "WV"),
            ("WI", "WI"),
            ("WY", "WY"),
        ]

        option_tags = []
        for code, label in states:
            sel = " selected" if selected == code else ""
            option_tags.append(
                f'<option value="{escape(code)}"{sel}>{escape(label)}</option>'
            )

        return Markup("\n".join(option_tags))

    return dict(state_options=state_options)

def _allowed_file(filename: str) -> bool:
    allowed = {"pdf", "doc", "docx", "rtf", "txt", "jpg", "jpeg", "png", "mp4", "mov", "avi"}
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in allowed



def _safe_segment(text: str) -> str:
    """Filesystem-safe name chunk."""
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in text)

def _open_in_file_manager(path: Path) -> None:
    """
    Open the given folder path in the host OS file manager (Finder / Explorer / etc).

    This only makes sense because the app and the browser are on the same machine.
    Errors are swallowed so we don't crash the request if the OS call fails.
    """
    folder = Path(path).resolve()
    if not folder.exists():
        return

    try:
        if sys.platform.startswith("darwin"):
            # macOS
            subprocess.Popen(["open", str(folder)])
        elif os.name == "nt":
            # Windows
            os.startfile(str(folder))  # type: ignore[attr-defined]
        else:
            # Linux / other
            subprocess.Popen(["xdg-open", str(folder)])
    except Exception:
        # Don't let OS issues break the web request
        pass

# ---- date parsing helper ----

def _parse_date(value: str):
    """
    Parse a date string from UI input.

    Accepts either 'YYYY-MM-DD' (native date input) or 'MM/DD/YYYY'
    (our js-date-autofmt text fields). Returns a datetime.date or None.
    """
    if not value:
        return None

    text = value.strip()
    if not text:
        return None

    # Try ISO format first, then MM/DD/YYYY
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    return None


# ---- MMDDYYYY parsing helper ----
def _parse_mmddyyyy(raw: str, field_label: str = "Date"):
    """
    Parse dates entered as MMDDYYYY, MM/DD/YYYY, or MM-DD-YYYY.

    Returns (date_obj, error_message). If parsing succeeds, error_message is None.
    If the input is empty, returns (None, None) so the caller can decide whether
    the field is required.
    """
    if not raw:
        return None, None

    s = raw.strip()
    if not s:
        return None, None

    # Strip out any non-digits so we accept 12/07/2025, 12-07-2025, or 12072025.
    digits = re.sub(r"\D", "", s)
    if len(digits) != 8:
        return None, f"{field_label} must be 8 digits in MMDDYYYY format."

    try:
        month = int(digits[0:2])
        day = int(digits[2:4])
        year = int(digits[4:8])

        # Optional safety range so obvious typos get caught.
        if year < 1900 or year > 2100:
            return None, f"{field_label} year must be between 1900 and 2100."

        return date(year, month, day), None
    except ValueError:
        return None, f"{field_label} must be a valid calendar date."


# ---- validation helpers ----
def _validate_email(value: str) -> bool:
    """Very small email sanity check: must contain @ and a dot after @."""
    if not value:
        return True  # treat empty as "not provided", which is allowed
    value = value.strip()
    if "@" not in value:
        return False
    local, _, domain = value.partition("@")
    if not local or "." not in domain:
        return False
    return True


def _validate_phone(value: str) -> bool:
    """Simple phone validation: 7–20 digits after stripping non-digits."""
    if not value:
        return True
    digits = "".join(ch for ch in value if ch.isdigit())
    return 7 <= len(digits) <= 20


def _validate_postal_code(value: str) -> bool:
    """
    Simple US-style ZIP validation: 5 digits, or ZIP+4 (#####-####).
    If empty, considered valid (field is optional).
    """
    if not value:
        return True
    value = value.strip()
    if len(value) == 5 and value.isdigit():
        return True
    if len(value) == 10 and value[5] == "-" and value[:5].isdigit() and value[6:].isdigit():
        return True
    return False


def _get_documents_root() -> Path:
    """
    Resolve the root folder for all documents.

    Behavior:
    - If Settings.documents_root is an ABSOLUTE path, use it exactly.
    - If it's RELATIVE, interpret it as relative to the project root
      (current_app.root_path), not instance/, and not inside a cloud-synced folder.
    - Create the folder if missing.

    This helps avoid accidentally storing documents in cloud-synced locations
    that can interfere with file availability or privacy.
    """
    settings = Settings.query.first()
    raw = settings.documents_root if settings and settings.documents_root else ""

    # Project root: where the Flask app package actually lives on disk
    project_root = Path(current_app.root_path).resolve()

    if raw:
        root = Path(raw).expanduser()

        # If user gave a relative path, anchor it under the project root
        if not root.is_absolute():
            root = project_root / root
    else:
        # Default: ./documents under the project directory
        root = project_root / "documents"

    root.mkdir(parents=True, exist_ok=True)
    return root


def _get_claim_folder(claim: Claim) -> Path:
    """
    Folder for a specific claim's documents, e.g. <root>/<claim_id>_<claimant>.
    """
    root = _get_documents_root()
    claimant_segment = _safe_segment(claim.claimant_name or f"claim_{claim.id}")
    folder = root / f"{claim.id}_{claimant_segment}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder

def _get_report_folder(report):
    """
    Return a Path to the folder where this report's documents live,
    creating it if needed.

    We reuse the claim folder and add a 'reports' subfolder so
    report docs don't get mixed in with claim-level docs.
    """
    claim_folder = _get_claim_folder(report.claim)
    report_root = claim_folder / "reports"
    report_root.mkdir(parents=True, exist_ok=True)
    return report_root

def _get_reports_folder(claim: Claim) -> Path:
    """
    Subfolder under claim folder for reports PDFs.
    """
    claim_folder = _get_claim_folder(claim)
    reports_folder = claim_folder / "reports"
    reports_folder.mkdir(parents=True, exist_ok=True)
    return reports_folder


def _get_invoices_folder(claim: Claim) -> Path:
    """
    Subfolder under claim folder for invoice PDFs.
    """
    claim_folder = _get_claim_folder(claim)
    invoices_folder = claim_folder / "invoices"
    invoices_folder.mkdir(parents=True, exist_ok=True)
    return invoices_folder


def _ensure_settings():
    """Return the single Settings row, creating it if necessary."""
    settings = Settings.query.first()
    if not settings:
        settings = Settings(
            business_name="Impact Medical Consulting, PLLC",
            state="ID",
            # default billing rates so a fresh DB "just works"
            hourly_rate=50.0,
            telephonic_rate=50.0,
            mileage_rate=0.50,
        )
        db.session.add(settings)
        db.session.commit()
    return settings


def _seed_basic_data():
    """Create one carrier, employer, provider if database is empty, for testing."""
    if Carrier.query.count() == 0:
        carrier = Carrier(
            name="Test Carrier",
            city="Boise",
            state="ID",
            postal_code="83701",
        )
        db.session.add(carrier)

    if Employer.query.count() == 0:
        employer = Employer(
            name="Test Employer",
            city="Boise",
            state="ID",
            postal_code="83702",
        )
        db.session.add(employer)

    if Provider.query.count() == 0:
        provider = Provider(
            name="Test Provider",
            city="Boise",
            state="ID",
            postal_code="83703",
        )
        db.session.add(provider)

    db.session.commit()


def _generate_invoice_number():
    """Generate an invoice number like 2025-001 based on the current year."""
    year = date.today().year
    prefix = f"{year}-"
    count = Invoice.query.filter(Invoice.invoice_number.like(f"{prefix}%")).count()
    seq = count + 1
    return f"{prefix}{seq:03d}"

def _calculate_invoice_totals(invoice: Invoice):
    """
    Calculate and persist totals for an invoice based on its items and current settings.

    This computes totals once and stores them on the Invoice record so they remain
    stable even if rates change later (persistent behavior).
    """
    settings = _ensure_settings()

    # Fall back to 0.0 if any rate is not set yet in Settings
    hourly_rate = settings.hourly_rate or 0.0
    mileage_rate = settings.mileage_rate or 0.0

    total_hours = 0.0
    total_miles = 0.0
    total_expenses = 0.0

    items = invoice.items or []
    for item in items:
        if not item.quantity:
            continue

        code = (item.activity_code or "").upper()
        qty = float(item.quantity)

        if code == "MIL":
            # mileage in miles
            total_miles += qty
        elif code == "EXP":
            # expenses entered as dollars
            total_expenses += qty
        elif code == "NO BILL":
            # explicitly non-billable
            continue
        else:
            # everything else treated as hours
            total_hours += qty

    total_amount = (total_hours * hourly_rate) + (total_miles * mileage_rate) + total_expenses

    invoice.total_hours = total_hours or 0.0
    invoice.total_miles = total_miles or 0.0
    invoice.total_expenses = total_expenses or 0.0
    invoice.total_amount = total_amount or 0.0

def _billable_is_complete(activity_code: str, service_date: str, quantity) -> bool:
    """
    Decide if a billable item is "complete" enough to clear the Needs Info tag.

    Rules (simplified):
    - For NO BILL: allow completion with just a date OR just a quantity.
    - For everything else: require BOTH date and quantity.
    """
    if activity_code == "NO BILL":
        return bool(service_date or quantity is not None)
    return bool(service_date and quantity is not None)

def _claim_has_related_data(claim: Claim) -> bool:
    """
    Return True if the claim has any related billables, reports, invoices,
    or documents. Used to warn/block deletion.
    """
    has_billables = BillableItem.query.filter_by(claim_id=claim.id).count() > 0
    has_reports = Report.query.filter_by(claim_id=claim.id).count() > 0
    has_invoices = Invoice.query.filter_by(claim_id=claim.id).count() > 0
    has_documents = ClaimDocument.query.filter_by(claim_id=claim.id).count() > 0
    return has_billables or has_reports or has_invoices or has_documents

# Helper to load active BarrierOption rows grouped by category
def _get_barrier_options_grouped():
    """
    Return active BarrierOption rows grouped by category.

    barriers_by_category = {
        "Psychosocial": [BarrierOption, ...],
        "Medical": [...],
        ...
    }
    """
    options = (
        BarrierOption.query.filter_by(is_active=True)
        .order_by(BarrierOption.sort_order, BarrierOption.label)
        .all()
    )
    grouped = defaultdict(list)
    for opt in options:
        category = opt.category or "General"
        grouped[category].append(opt)
    return grouped

# ---- Settings: Billable Activity Codes management ----
@bp.route("/settings/billables", methods=["GET", "POST"])
def settings_billables():
    """
    Manage BillingActivityCode entries used for billable activity selection.

    GET: render list of all billable codes with an inline add form.
    POST: create a new billable code from the inline form.
    """
    settings = _ensure_settings()
    # Build billable activity choices from the BillingActivityCode table.
    # Only include active codes, ordered by sort_order then code.
    db_codes = (
        BillingActivityCode.query
        .filter_by(is_active=True)
        .order_by(BillingActivityCode.sort_order, BillingActivityCode.code)
        .all()
    )
    if db_codes:
        billable_activity_choices = [
            (c.code, c.label or c.code) for c in db_codes
        ]
    else:
        # Fallback to the global defaults if the table is empty.
        billable_activity_choices = BILLABLE_ACTIVITY_CHOICES

    error = None

    # --- Auto-seed billing codes if table is empty ---
    if BillingActivityCode.query.count() == 0:
        for code, label, sort_order in BILLABLE_ACTIVITY_SEED:
            db.session.add(
                BillingActivityCode(
                    code=code,
                    label=label,
                    sort_order=sort_order,
                    is_active=True,
                )
            )
        db.session.commit()

    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        description = (request.form.get("description") or "").strip()
        sort_order_raw = (request.form.get("sort_order") or "").strip()

        sort_order = None
        if sort_order_raw:
            try:
                sort_order = int(sort_order_raw)
            except ValueError:
                sort_order = None

        if not code:
            error = "Code is required for a billable activity."
        else:
            # Normalize code
            code_normalized = code.upper()

            # Default sort order to a large value if not provided so it falls to the bottom.
            if sort_order is None:
                sort_order = 999

            # Use the description text as the human label; if blank, fall back to the code
            label = description or code_normalized

            bac = BillingActivityCode(
                code=code_normalized,
                label=label,
                sort_order=sort_order,
                is_active=True,
            )
            db.session.add(bac)
            try:
                db.session.commit()
                flash("Billable activity code added.", "success")
                return redirect(url_for("main.settings_billables"))
            except IntegrityError:
                db.session.rollback()
                error = "A billable activity with that code already exists. Please choose a different code."

    # List all codes (active and inactive) ordered by sort_order then code.
    codes = (
        BillingActivityCode.query
        .order_by(
            BillingActivityCode.sort_order,
            BillingActivityCode.code,
        )
        .all()
    )

    return render_template(
        "settings_billables.html",
        active_page="settings",
        settings=settings,
        codes=codes,
        error=error,
    )


@bp.route("/settings/billables/<int:code_id>/edit", methods=["GET", "POST"])
def settings_billable_edit(code_id):
    """
    Edit an existing BillingActivityCode.
    """
    settings = _ensure_settings()
    billable = BillingActivityCode.query.get_or_404(code_id)
    error = None

    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        description = (request.form.get("description") or "").strip()
        sort_order_raw = (request.form.get("sort_order") or "").strip()
        is_active_raw = (request.form.get("is_active") or "").strip().lower()

        sort_order = None
        if sort_order_raw:
            try:
                sort_order = int(sort_order_raw)
            except ValueError:
                sort_order = None

        if not code:
            error = "Code is required for a billable activity."
        else:
            billable.code = code.upper()
            # Store the human-readable text in label; fall back to code if left blank
            billable.label = description or billable.code
            if sort_order is not None:
                billable.sort_order = sort_order

            billable.is_active = is_active_raw in ("on", "true", "1", "yes")

            try:
                db.session.commit()
                flash("Billable activity code updated.", "success")
                return redirect(url_for("main.settings_billables"))
            except IntegrityError:
                db.session.rollback()
                error = "A billable activity with that code already exists. Please choose a different code."

    return render_template(
        "settings_billable_form.html",
        active_page="settings",
        settings=settings,
        billable=billable,
        error=error,
    )


@bp.route("/settings/billables/<int:code_id>/toggle", methods=["POST"])
def settings_billable_toggle(code_id):
    """
    Quick toggle for a billable code's active flag from the list view.
    """
    billable = BillingActivityCode.query.get_or_404(code_id)
    billable.is_active = not bool(billable.is_active)
    db.session.commit()
    flash("Billable activity code status updated.", "success")
    return redirect(url_for("main.settings_billables"))

# ---------- basic pages ----------


@bp.route("/")
@bp.route("/claims")
def claims_list():
    claims = Claim.query.order_by(Claim.id.desc()).all()

    # Read optional filters from query string
    status_filter = (request.args.get("status") or "").strip().lower()
    billing_filter = (request.args.get("billing") or "").strip().lower()

    # Normalise filters to known values or "all"
    if status_filter not in ("active", "dormant"):
        status_filter = "all"

    if billing_filter not in ("none", "open", "closed"):
        billing_filter = "all"

    # --- Dormant status calculation ---
    dormant_info = {}
    dormant_threshold_days = _ensure_settings().dormant_claim_days or 0

    for c in claims:
        # Determine last activity date: latest of report, billable, invoice, document
        last_date = None

        # Reports
        r = (
            Report.query.filter_by(claim_id=c.id)
            .order_by(Report.created_at.desc())
            .first()
        )
        if r and r.created_at:
            last_date = r.created_at.date()

        # Billables
        b = (
            BillableItem.query.filter_by(claim_id=c.id)
            .order_by(BillableItem.created_at.desc())
            .first()
        )
        if b and b.created_at:
            d = b.created_at.date()
            if not last_date or d > last_date:
                last_date = d

        # Invoices
        inv = (
            Invoice.query.filter_by(claim_id=c.id)
            .order_by(Invoice.id.desc())
            .first()
        )
        if inv and inv.invoice_date:
            d = inv.invoice_date
            if not last_date or d > last_date:
                last_date = d

        # Documents
        doc = (
            ClaimDocument.query.filter_by(claim_id=c.id)
            .order_by(ClaimDocument.uploaded_at.desc())
            .first()
        )
        if doc and doc.uploaded_at:
            d = doc.uploaded_at.date()
            if not last_date or d > last_date:
                last_date = d

        # Evaluate dormancy
        if last_date:
            delta = (date.today() - last_date).days
            is_dormant = dormant_threshold_days > 0 and delta >= dormant_threshold_days
        else:
            is_dormant = False

        dormant_info[c.id] = {
            "is_dormant": is_dormant,
            "last_activity": last_date,
        }

    # Build billing summary per-claim:
    # summary[claim_id] = {"total": X, "open": Y, "closed": Z}
    billing_summary = {}

    if claims:
        claim_ids = [c.id for c in claims]

        invoices = (
            Invoice.query
            .filter(Invoice.claim_id.in_(claim_ids))
            .all()
        )

        # Initialize all claims so they at least exist with zeroes
        for cid in claim_ids:
            billing_summary[cid] = {"total": 0, "open": 0, "closed": 0}

        # Tally per-claim
        for inv in invoices:
            cid = inv.claim_id
            status = (inv.status or "Draft")

            entry = billing_summary.get(cid)
            if not entry:
                entry = {"total": 0, "open": 0, "closed": 0}
                billing_summary[cid] = entry

            entry["total"] += 1
            if status in ("Paid", "Void"):
                entry["closed"] += 1
            else:
                entry["open"] += 1

    # --- Apply filters to claims list ---
    filtered_claims = []
    for c in claims:
        # Status filter based on dormant_info
        if status_filter in ("active", "dormant"):
            info = dormant_info.get(c.id, {})
            is_dormant = bool(info.get("is_dormant", False))

            if status_filter == "active" and is_dormant:
                continue
            if status_filter == "dormant" and not is_dormant:
                continue

        # Billing filter based on billing_summary
        summary = billing_summary.get(c.id, {"total": 0, "open": 0, "closed": 0})
        total_inv = summary["total"]
        open_inv = summary["open"]
        closed_inv = summary["closed"]

        if billing_filter == "none" and total_inv != 0:
            continue
        if billing_filter == "open" and open_inv <= 0:
            continue
        if billing_filter == "closed" and closed_inv <= 0:
            continue

        filtered_claims.append(c)

    return render_template(
        "claims_list.html",
        active_page="claims",
        claims=filtered_claims,
        billing_summary=billing_summary,
        dormant_info=dormant_info,
        status_filter=status_filter,
        billing_filter=billing_filter,
    )



@bp.route("/billing")
def billing_list():
    invoices = Invoice.query.order_by(Invoice.id.desc()).all()
    return render_template(
        "billing_list.html",
        active_page="billing",
        invoices=invoices,
    )


# ---- Reporting / Analytics Dashboard ----
@bp.route("/analysis")
def analysis_dashboard():
    """
    High-level reporting / analytics landing page.

    Focuses on overall workload and business health metrics.
    """
    settings = _ensure_settings()
    today = date.today()

    # ----- Active claims -----
    # Treat "active" as not explicitly closed if an is_closed flag exists,
    # otherwise include all claims.
    if hasattr(Claim, "is_closed"):
        active_claims_query = Claim.query.filter(
            (Claim.is_closed.is_(False)) | (Claim.is_closed.is_(None))
        )
    else:
        active_claims_query = Claim.query

    active_claims = active_claims_query.all()
    active_claims_count = len(active_claims)

    # Average age (in days) of open/active claims, based on DOI or created_at.
    ages = []
    for c in active_claims:
        ref = c.doi or getattr(c, "created_at", None)
        ref_date = None
        if isinstance(ref, datetime):
            ref_date = ref.date()
        elif isinstance(ref, date):
            ref_date = ref

        if ref_date:
            ages.append((today - ref_date).days)

    avg_open_claim_age_days = int(sum(ages) / len(ages)) if ages else 0

    # ----- Stale claims (no recent activity) -----
    # Use last report / billable / invoice / document date similar to claims_list.
    # Threshold comes from Settings.dormant_claim_days, falling back to 60 days if not set.
    if settings and getattr(settings, "dormant_claim_days", None):
        stale_threshold_days = settings.dormant_claim_days
    else:
        stale_threshold_days = 60

    stale_claims_count = 0
    for c in active_claims:
        last_date = None

        r = (
            Report.query.filter_by(claim_id=c.id)
            .order_by(Report.created_at.desc())
            .first()
        )
        if r and r.created_at:
            last_date = r.created_at.date()

        b = (
            BillableItem.query.filter_by(claim_id=c.id)
            .order_by(BillableItem.created_at.desc())
            .first()
        )
        if b and b.created_at:
            d = b.created_at.date()
            if not last_date or d > last_date:
                last_date = d

        inv = (
            Invoice.query.filter_by(claim_id=c.id)
            .order_by(Invoice.invoice_date.desc().nullslast(), Invoice.id.desc())
            .first()
        )
        if inv and inv.invoice_date:
            d = inv.invoice_date
            if not last_date or d > last_date:
                last_date = d

        doc = (
            ClaimDocument.query.filter_by(claim_id=c.id)
            .order_by(ClaimDocument.uploaded_at.desc())
            .first()
        )
        if doc and doc.uploaded_at:
            d = doc.uploaded_at.date()
            if not last_date or d > last_date:
                last_date = d

        if last_date:
            delta_days = (today - last_date).days
            if delta_days >= stale_threshold_days:
                stale_claims_count += 1

    # ----- Hours worked in last 30 days -----
    cutoff_date = today - timedelta(days=30)
    hours_last_30_days = 0.0

    for item in BillableItem.query.all():
        if not item.quantity:
            continue

        code = (item.activity_code or "").upper()
        # Only treat "normal" activity codes as hours, not mileage/expenses/no-bill.
        if code in ("MIL", "EXP", "NO BILL"):
            continue

        activity_date = None
        if getattr(item, "date_of_service", None):
            activity_date = item.date_of_service
        elif getattr(item, "created_at", None):
            if isinstance(item.created_at, datetime):
                activity_date = item.created_at.date()

        if isinstance(activity_date, datetime):
            activity_date = activity_date.date()

        if isinstance(activity_date, date) and activity_date >= cutoff_date:
            hours_last_30_days += float(item.quantity)

    # Targets for the same 30-day period, based on weekly targets in Settings.
    weekly_min = settings.target_min_hours_per_week or 0.0
    weekly_max = settings.target_max_hours_per_week or 0.0
    factor_30_days = 30.0 / 7.0

    hours_target_min_30 = weekly_min * factor_30_days
    hours_target_max_30 = weekly_max * factor_30_days

    # ----- Invoices / AR -----
    total_claims = Claim.query.count()
    total_invoices = Invoice.query.count()

    invoices = Invoice.query.all()

    # Anything not Paid/Void is considered "open"
    open_invoices = 0
    total_outstanding_ar = 0.0
    ar_by_carrier = {}

    for inv in invoices:
        status = inv.status or "Draft"
        amount = float(inv.total_amount or 0.0)

        is_open = status not in ("Paid", "Void")
        if is_open:
            open_invoices += 1
            total_outstanding_ar += amount

        # Aggregate AR by carrier for open invoices
        if not is_open:
            continue

        if inv.claim and inv.claim.carrier:
            carrier_name = inv.claim.carrier.name
        else:
            carrier_name = "Unassigned"

        ar_by_carrier[carrier_name] = ar_by_carrier.get(carrier_name, 0.0) + amount

    return render_template(
        "analysis.html",
        active_page="analysis",
        settings=settings,
        # High-level metrics
        active_claims_count=active_claims_count,
        avg_open_claim_age_days=avg_open_claim_age_days,
        stale_claims_count=stale_claims_count,
        hours_last_30_days=hours_last_30_days,
        hours_target_min_30=hours_target_min_30,
        hours_target_max_30=hours_target_max_30,
        total_outstanding_ar=total_outstanding_ar,
        # Existing counts for reference
        total_claims=total_claims,
        total_invoices=total_invoices,
        open_invoices=open_invoices,
        ar_by_carrier=ar_by_carrier,
    )


# ---- Reporting dashboard for downloadable/exportable reports ----
@bp.route("/reporting")
def reporting_dashboard():
    """
    High-level reporting dashboard for downloadable / exportable reports.

    Focus here is on classic "report" style outputs:
    - Aging by bucket (0–30, 31–60, 61–90, 90+ days)
    - Outstanding AR by carrier
    - Detailed open-invoice list that can later be exported.

    Supports optional drill-down filters via query params:
    - ?carrier=<carrier name>
    - ?bucket=0-30|31-60|61-90|90+
    """
    settings = _ensure_settings()
    today = date.today()

    # Optional filters for drill-down views
    carrier_filter = (request.args.get("carrier") or "").strip() or None
    bucket_filter = (request.args.get("bucket") or "").strip() or None

    # Basic counts
    total_claims = Claim.query.count()
    total_invoices = Invoice.query.count()

    invoices = Invoice.query.all()

    # Aging buckets (dollar amounts)
    aging_buckets = {
        "0-30": 0.0,
        "31-60": 0.0,
        "61-90": 0.0,
        "90+": 0.0,
    }

    # Per-carrier AR totals (open invoices only)
    ar_by_carrier = {}

    # Detailed open-invoice rows for the table / export
    open_invoice_rows = []

    for inv in invoices:
        status = inv.status or "Draft"
        amount = float(inv.total_amount or 0.0)

        # Only treat non-Paid / non-Void as open AR
        is_open = status not in ("Paid", "Void")
        if not is_open:
            continue

        # ---- Determine effective invoice date for aging ----
        effective_date = None
        if inv.invoice_date:
            # Normal path: explicit invoice_date
            effective_date = inv.invoice_date
        else:
            # Fall back to created_at if present
            created_at = getattr(inv, "created_at", None)
            if isinstance(created_at, datetime):
                effective_date = created_at.date()
            elif isinstance(created_at, date):
                effective_date = created_at

        # Compute age in days and bucket if we have any usable date
        age_days = None
        bucket_label = None
        if effective_date:
            age_days = (today - effective_date).days

            if age_days <= 30:
                bucket_label = "0-30"
            elif age_days <= 60:
                bucket_label = "31-60"
            elif age_days <= 90:
                bucket_label = "61-90"
            else:
                bucket_label = "90+"

            # Bucket the amount by age for the summary row
            aging_buckets[bucket_label] += amount

        # Carrier for grouping
        if inv.claim and inv.claim.carrier:
            carrier_name = inv.claim.carrier.name
        else:
            carrier_name = "Unassigned"

        ar_by_carrier[carrier_name] = ar_by_carrier.get(carrier_name, 0.0) + amount

        # Build a row for the open-invoice detail list
        open_invoice_rows.append(
            {
                "invoice": inv,
                "claim": inv.claim,
                "carrier_name": carrier_name,
                "age_days": age_days,
                "bucket": bucket_label,
                "amount": amount,
            }
        )

    # Apply drill-down filters, if any
    if carrier_filter or bucket_filter:
        filtered_rows = []
        for row in open_invoice_rows:
            if carrier_filter and row["carrier_name"] != carrier_filter:
                continue
            if bucket_filter and row.get("bucket") != bucket_filter:
                continue
            filtered_rows.append(row)
    else:
        filtered_rows = open_invoice_rows

    # Simple counts / totals (based on filtered rows)
    open_invoices_count = len(filtered_rows)
    total_open_amount = sum(row["amount"] for row in filtered_rows)

    return render_template(
        "reporting_dashboard.html",
        active_page="reporting",
        settings=settings,
        total_claims=total_claims,
        total_invoices=total_invoices,
        # count of currently visible (filtered) open invoices
        open_invoices=open_invoices_count,
        # detailed data for the table / export (already filtered)
        open_invoice_rows=filtered_rows,
        # total A/R for the current view
        total_open_amount=total_open_amount,
        # unfiltered summary groupings
        aging_buckets=aging_buckets,
        ar_by_carrier=ar_by_carrier,
        carrier_filter=carrier_filter,
        bucket_filter=bucket_filter,
    )


# ----------- Create invoice from claim (all uninvoiced, complete items) ----------
@bp.route("/claims/<int:claim_id>/invoice/new", methods=["GET"])
def invoice_new_for_claim(claim_id):
    """Create a new invoice for a claim using all uninvoiced, complete billable items."""
    claim = Claim.query.get_or_404(claim_id)

    # Get all complete billable items for this claim that are not yet attached to an invoice
    items = (
        BillableItem.query
        .filter_by(claim_id=claim.id, invoice_id=None)
        .filter(BillableItem.is_complete.is_(True))
        .all()
    )

    if not items:
        # Nothing to invoice – warn and send back to claim
        flash("This claim has no complete billable items to invoice yet.", "warning")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    # Compute DOS range from the items that have a date_of_service
    dated_items = [i for i in items if i.date_of_service]
    if dated_items:
        dos_start = min(i.date_of_service for i in dated_items)
        dos_end = max(i.date_of_service for i in dated_items)
    else:
        dos_start = None
        dos_end = None

    # Create invoice shell
    invoice = Invoice(
        claim_id=claim.id,
        carrier_id=claim.carrier_id,
        employer_id=claim.employer_id,
        invoice_number=_generate_invoice_number(),
        status="Draft",
        invoice_date=None,
        dos_start=dos_start,
        dos_end=dos_end,
    )
    db.session.add(invoice)
    db.session.flush()  # get invoice.id, invoice.items will work now

    # Attach items to this invoice
    for item in items:
        item.invoice_id = invoice.id

    # Calculate and persist totals once at creation time
    _calculate_invoice_totals(invoice)

    db.session.commit()

    return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))

@bp.route("/claims/<int:claim_id>/reports/<int:report_id>/invoice/new", methods=["GET"])
def invoice_new_for_report(claim_id, report_id):
    """
    Create a new invoice for a claim using all uninvoiced, complete billable items
    whose dates of service fall within this report's DOS range.
    """
    claim = Claim.query.get_or_404(claim_id)
    report = (
        Report.query.filter_by(id=report_id, claim_id=claim.id)
        .first_or_404()
    )

    # Require a DOS range to filter by
    if not report.dos_start or not report.dos_end:
        flash("This report does not have a complete date-of-service range.", "warning")
        return redirect(url_for("main.report_edit", claim_id=claim.id, report_id=report.id))

    # Find all complete, uninvoiced billable items for this claim within the report's DOS window
    items = (
        BillableItem.query
        .filter_by(claim_id=claim.id, invoice_id=None)
        .filter(BillableItem.is_complete.is_(True))
        .filter(BillableItem.date_of_service >= report.dos_start)
        .filter(BillableItem.date_of_service <= report.dos_end)
        .all()
    )

    if not items:
        flash("No complete billable items in this report's date range to invoice.", "warning")
        return redirect(url_for("main.report_edit", claim_id=claim.id, report_id=report.id))

    # Create invoice shell using the report's DOS range
    invoice = Invoice(
        claim_id=claim.id,
        carrier_id=claim.carrier_id,
        employer_id=claim.employer_id,
        invoice_number=_generate_invoice_number(),
        status="Draft",
        invoice_date=None,
        dos_start=report.dos_start,
        dos_end=report.dos_end,
    )
    db.session.add(invoice)
    db.session.flush()

    # Attach items to this invoice
    for item in items:
        item.invoice_id = invoice.id

    # Calculate and persist totals
    _calculate_invoice_totals(invoice)

    db.session.commit()

    return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))

@bp.route("/billing/<int:invoice_id>")
def invoice_detail(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    claim = invoice.claim
    items = invoice.items or []

    return render_template(
        "invoice_detail.html",
        active_page="billing",
        invoice=invoice,
        claim=claim,
        items=items,
    )

@bp.route("/billing/<int:invoice_id>/print")
def invoice_print(invoice_id):
    """
    Render a print-friendly invoice preview.

    Gina can use the browser's Print dialog (including "Save as PDF") instead of
    the app generating PDFs directly.
    """
    invoice = Invoice.query.get_or_404(invoice_id)
    claim = invoice.claim
    settings = _ensure_settings()
    items = invoice.items or []

    return render_template(
        "invoice_print.html",
        active_page="billing",
        settings=settings,
        invoice=invoice,
        claim=claim,
        items=items,
    )

@bp.route("/billing/<int:invoice_id>/update", methods=["POST"])
def invoice_update(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)

    old_status = invoice.status or "Draft"
    old_invoice_date = invoice.invoice_date

    new_status = (request.form.get("status") or "").strip() or None
    invoice_date_raw = (request.form.get("invoice_date") or "").strip() or None
    invoice_date = _parse_date(invoice_date_raw) if invoice_date_raw else None

    allowed_statuses = ["Draft", "Sent", "Paid", "Void"]
    if new_status not in allowed_statuses:
        new_status = old_status or "Draft"

    invoice.status = new_status
    invoice.invoice_date = invoice_date

    db.session.commit()

    return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))

@bp.route("/billing/<int:invoice_id>/add-uninvoiced", methods=["POST"])
def invoice_add_uninvoiced(invoice_id):
    """
    Attach all complete, uninvoiced billable items for this invoice's claim
    to this invoice. Only allowed while invoice is in Draft status.
    After attaching, recalculate and persist invoice totals.
    """
    invoice = Invoice.query.get_or_404(invoice_id)
    claim = invoice.claim

    current_status = invoice.status or "Draft"
    # Don't mutate invoices that are not draft
    if current_status not in ("Draft",):
        return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))

    # Find all complete billable items for this claim that are not yet invoiced
    items = (
        BillableItem.query
        .filter_by(claim_id=claim.id, invoice_id=None)
        .filter(BillableItem.is_complete.is_(True))
        .all()
    )

    if not items:
        # Nothing to add; just reload invoice screen
        return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))

    # Attach items
    for item in items:
        item.invoice_id = invoice.id

    # Recalculate totals now that the invoice has more items
    _calculate_invoice_totals(invoice)

    db.session.commit()

    return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))


@bp.route("/billing/<int:invoice_id>/delete", methods=["POST"])
def invoice_delete(invoice_id):
    """
    Delete a Draft invoice, returning its items to the claim (invoice_id = None).
    Sent / Paid / Void invoices are protected and cannot be deleted here.
    """
    invoice = Invoice.query.get_or_404(invoice_id)
    claim_id = invoice.claim_id

    current_status = invoice.status or "Draft"
    if current_status not in ("Draft",):
        # For now, do nothing if not Draft; could flash a message later.
        return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))

    # Detach items from this invoice
    if invoice.items:
        for item in invoice.items:
            item.invoice_id = None

    db.session.delete(invoice)
    db.session.commit()

    return redirect(url_for("main.claim_detail", claim_id=claim_id))

@bp.route("/billing/<int:invoice_id>/items/<int:item_id>/remove", methods=["POST"])
def invoice_remove_item(invoice_id, item_id):
    """
    Remove a single line item from an invoice (Draft only) and return it to the claim
    as an uninvoiced billable item.
    """
    invoice = Invoice.query.get_or_404(invoice_id)
    claim = invoice.claim

    current_status = invoice.status or "Draft"
    # Only allow changes while in Draft status
    if current_status not in ("Draft",):
        return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))

    # Ensure the item belongs to this invoice and claim
    item = (
        BillableItem.query
        .filter_by(id=item_id, claim_id=claim.id, invoice_id=invoice.id)
        .first_or_404()
    )

    # Detach the item from this invoice
    item.invoice_id = None

    # Recalculate totals now that the invoice has one fewer item
    _calculate_invoice_totals(invoice)

    db.session.commit()

    return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))

# ----------- New report creation ----------
@bp.route("/claims/<int:claim_id>/reports/new/<report_type>")
def report_new(claim_id, report_type):
    """
    Create a new report for a claim and redirect to the report edit screen.

    For now we:
    - Validate the report type (initial/progress/closure)
    - Compute a simple DOS range based on the last report for this claim
    - Create the Report row
    - Redirect to report_edit() so Gina can fill in the details
    """
    claim = Claim.query.get_or_404(claim_id)
    rt = (report_type or "").strip().lower()

    if rt not in ("initial", "progress", "closure"):
        flash("Invalid report type.", "danger")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    # Look at the most recent report to suggest DOS defaults
    last_report = (
        Report.query
        .filter_by(claim_id=claim.id)
        .order_by(Report.dos_end.desc().nullslast(), Report.created_at.desc())
        .first()
    )

    today = date.today()

    if rt == "initial" or not last_report or not last_report.dos_end:
        # For an initial (or first) report, default both dates to today for now.
        dos_start = today
        dos_end = today
    else:
        # For progress/closure, default start to the last report's DOS end,
        # and end to today. Gina can tweak these on the edit screen.
        if isinstance(last_report.dos_end, date):
            dos_start = last_report.dos_end
        else:
            dos_start = today
        dos_end = today

    # Default treating provider from the most recent prior report (if any)
    treating_provider_id = None
    if last_report and last_report.treating_provider_id:
        treating_provider_id = last_report.treating_provider_id

    report = Report(
        claim_id=claim.id,
        report_type=rt,
        dos_start=dos_start,
        dos_end=dos_end,
        treating_provider_id=treating_provider_id,
    )
    # Roll forward barriers from the most recent prior report on this claim
    if last_report and last_report.barriers_json:
        report.barriers_json = last_report.barriers_json

    db.session.add(report)
    db.session.commit()

    # --- Auto-create a billable item for this report ---
    activity_code = "RPT"
    if rt == "initial":
        qty = 1.0
        description = "Initial report"
    elif rt in ("progress", "closure"):
        qty = 0.5
        description = f"{rt.capitalize()} report"
    else:
        qty = None
        description = "Report"

    if qty is not None:
        # Prefer DOS end, then DOS start, else None
        dos_for_billing = report.dos_end or report.dos_start or None
        is_complete = _billable_is_complete(activity_code, dos_for_billing, qty)

        billable = BillableItem(
            claim_id=claim.id,
            report_id=report.id,
            activity_code=activity_code,
            date_of_service=dos_for_billing,
            quantity=qty,
            description=description,
            is_complete=is_complete,
        )
        db.session.add(billable)
        db.session.commit()

    return redirect(url_for("main.report_edit", claim_id=claim.id, report_id=report.id))



@bp.route("/claims/<int:claim_id>/delete", methods=["GET", "POST"])
def claim_delete(claim_id):
    """Two-step delete for a claim: confirm on GET, actually delete on POST."""
    claim = Claim.query.get_or_404(claim_id)

    if request.method == "POST":
        # Delete related records first to avoid FK issues
        BillableItem.query.filter_by(claim_id=claim.id).delete()
        Report.query.filter_by(claim_id=claim.id).delete()
        ClaimDocument.query.filter_by(claim_id=claim.id).delete()
        Invoice.query.filter_by(claim_id=claim.id).delete()

        # Now delete the claim itself
        db.session.delete(claim)
        db.session.commit()

        # ✅ After deletion, go back to the claims list
        return redirect(url_for("main.claims_list"))

    # GET: show the confirmation page
    return render_template(
        "claim_delete_confirm.html",
        active_page="claims",
        claim=claim,
    )


@bp.route("/claims/new", methods=["GET", "POST"])
def new_claim():
    carriers = Carrier.query.order_by(Carrier.name).all()
    employers = Employer.query.order_by(Employer.name).all()

    # All carrier contacts so we can filter per-carrier in the template/JS
    carrier_contacts = (
        Contact.query.filter(Contact.carrier_id.isnot(None))
        .order_by(Contact.name)
        .all()
    )

    error = None

    if request.method == "POST":
        claimant_name = (request.form.get("claimant_name") or "").strip()
        claim_number = (request.form.get("claim_number") or "").strip()

        dob_raw = (request.form.get("dob") or "").strip()
        doi_raw = (request.form.get("doi") or "").strip()

        claim_state = (request.form.get("claim_state") or "").strip() or None

        claimant_address1 = (request.form.get("claimant_address1") or "").strip() or None
        claimant_address2 = (request.form.get("claimant_address2") or "").strip() or None
        claimant_city = (request.form.get("claimant_city") or "").strip() or None
        claimant_state = (request.form.get("claimant_state") or "").strip() or None
        claimant_postal_code = (request.form.get("claimant_postal_code") or "").strip() or None
        claimant_phone = (request.form.get("claimant_phone") or "").strip() or None
        claimant_email = (request.form.get("claimant_email") or "").strip() or None

        primary_care_provider = (request.form.get("primary_care_provider") or "").strip() or None

        carrier_id_raw = (request.form.get("carrier_id") or "").strip()
        employer_id_raw = (request.form.get("employer_id") or "").strip()
        carrier_contact_id_raw = (request.form.get("carrier_contact_id") or "").strip()

        dob = _parse_date(dob_raw)
        doi = _parse_date(doi_raw)

        if not claimant_name or not claim_number:
            error = "Claimant name and claim number are required."
        else:
            # Pre-check for duplicate claim number
            existing = Claim.query.filter_by(claim_number=claim_number).first()
            if existing:
                error = "A claim with that claim number already exists."
            else:
                claim = Claim(
                    claimant_name=claimant_name,
                    claim_number=claim_number,
                    dob=dob,
                    doi=doi,
                    claim_state=claim_state,
                    is_telephonic=False,
                    claimant_address1=claimant_address1,
                    claimant_address2=claimant_address2,
                    claimant_city=claimant_city,
                    claimant_state=claimant_state,
                    claimant_postal_code=claimant_postal_code,
                    claimant_phone=claimant_phone,
                    claimant_email=claimant_email,
                    primary_care_provider=primary_care_provider,
                )

                # Only set these if values were provided
                if carrier_id_raw:
                    try:
                        claim.carrier_id = int(carrier_id_raw)
                    except ValueError:
                        pass

                if employer_id_raw:
                    try:
                        claim.employer_id = int(employer_id_raw)
                    except ValueError:
                        pass

                if carrier_contact_id_raw:
                    try:
                        claim.carrier_contact_id = int(carrier_contact_id_raw)
                    except ValueError:
                        pass

                db.session.add(claim)
                try:
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
                    error = "A claim with that claim number already exists."
                else:
                    return redirect(url_for("main.claim_detail", claim_id=claim.id))

    return render_template(
        "claim_new.html",
        active_page="claims",
        carriers=carriers,
        employers=employers,
        carrier_contacts=carrier_contacts,
        error=error,
    )

# ---- Claim edit route ----
@bp.route("/claims/<int:claim_id>/edit", methods=["GET", "POST"])
def claim_edit(claim_id):
    claim = Claim.query.get_or_404(claim_id)
    error = None

    carriers = Carrier.query.order_by(Carrier.name).all()
    employers = Employer.query.order_by(Employer.name).all()

    # Default contact list based on the claim's current carrier (for initial GET)
    carrier_contacts = []
    if claim.carrier_id:
        carrier_contacts = (
            Contact.query
            .filter_by(carrier_id=claim.carrier_id)
            .order_by(Contact.name)
            .all()
        )

    if request.method == "POST":
        claimant_name = (request.form.get("claimant_name") or "").strip()
        claim_number = (request.form.get("claim_number") or "").strip()

        # Dates
        dob = _parse_date(request.form.get("dob"))
        doi = _parse_date(request.form.get("doi"))

        # Claim-level state
        claim_state = (request.form.get("claim_state") or "").strip() or None

        # Claimant contact fields
        claimant_address1 = (request.form.get("claimant_address1") or "").strip() or None
        claimant_address2 = (request.form.get("claimant_address2") or "").strip() or None
        claimant_city = (request.form.get("claimant_city") or "").strip() or None
        claimant_state = (request.form.get("claimant_state") or "").strip() or None
        claimant_postal_code = (request.form.get("claimant_postal_code") or "").strip() or None
        claimant_phone = (request.form.get("claimant_phone") or "").strip() or None
        claimant_email = (request.form.get("claimant_email") or "").strip() or None

        primary_care_provider = (request.form.get("primary_care_provider") or "").strip() or None

        # Carrier / employer / contact ids
        carrier_id_raw = (request.form.get("carrier_id") or "").strip()
        employer_id_raw = (request.form.get("employer_id") or "").strip()
        carrier_contact_id_raw = (request.form.get("carrier_contact_id") or "").strip()

        carrier_id = None
        if carrier_id_raw:
            try:
                carrier_id = int(carrier_id_raw)
            except ValueError:
                carrier_id = None

        employer_id = None
        if employer_id_raw:
            try:
                employer_id = int(employer_id_raw)
            except ValueError:
                employer_id = None

        carrier_contact_id = None
        if carrier_contact_id_raw:
            try:
                carrier_contact_id = int(carrier_contact_id_raw)
            except ValueError:
                carrier_contact_id = None

        # Refresh carrier contacts list based on newly selected carrier (or existing one)
        effective_carrier_id = carrier_id if carrier_id is not None else claim.carrier_id
        carrier_contacts = []
        if effective_carrier_id:
            carrier_contacts = (
                Contact.query
                .filter_by(carrier_id=effective_carrier_id)
                .order_by(Contact.name)
                .all()
            )

        if not claimant_name or not claim_number:
            error = "Claimant name and claim number are required."
        else:
            claim.claimant_name = claimant_name
            claim.claim_number = claim_number
            claim.dob = dob
            claim.doi = doi
            claim.claim_state = claim_state

            claim.claimant_address1 = claimant_address1
            claim.claimant_address2 = claimant_address2
            claim.claimant_city = claimant_city
            claim.claimant_state = claimant_state
            claim.claimant_postal_code = claimant_postal_code
            claim.claimant_phone = claimant_phone
            claim.claimant_email = claimant_email

            claim.primary_care_provider = primary_care_provider

            claim.carrier_id = carrier_id
            claim.employer_id = employer_id
            claim.carrier_contact_id = carrier_contact_id

            db.session.commit()
            return redirect(url_for("main.claim_detail", claim_id=claim.id))

    return render_template(
        "claim_edit.html",
        active_page="claims",
        claim=claim,
        error=error,
        carriers=carriers,
        employers=employers,
        carrier_contacts=carrier_contacts,
    )


@bp.route("/claims/<int:claim_id>/", methods=["GET", "POST"])
def claim_detail(claim_id):
    claim = Claim.query.get_or_404(claim_id)
    settings = _ensure_settings()

    # Billable items
    billable_items = (
        BillableItem.query.filter_by(claim_id=claim.id)
        .order_by(
            BillableItem.date_of_service.desc().nullslast(),
            BillableItem.created_at.desc(),
    )
    .all()
)

    # Reports
    reports = (
        Report.query.filter_by(claim_id=claim.id)
        .order_by(Report.created_at.desc())
        .all()
    )

    # Documents
    documents = (
        ClaimDocument.query.filter_by(claim_id=claim.id)
        .order_by(ClaimDocument.uploaded_at.desc())
        .all()
    )

    # Invoices
    invoices = (
        Invoice.query.filter_by(claim_id=claim.id)
        .order_by(
            Invoice.invoice_date.desc().nullslast(),
            Invoice.id.desc(),
        )
        .all()
    )

    # Map: invoice_id -> invoice object (for the billable-items "badge")
    invoice_map = {inv.id: inv for inv in invoices}

    # Anything not Paid/Void is "open"
    open_invoice_count = sum(
        1
        for inv in invoices
        if (inv.status or "Draft") not in ("Paid", "Void")
    )

    # Build billable activity choices from the BillingActivityCode table.
    # Only include active codes, ordered by sort_order then code.
    db_codes = (
        BillingActivityCode.query
        .filter_by(is_active=True)
        .order_by(BillingActivityCode.sort_order, BillingActivityCode.code)
        .all()
    )
    if db_codes:
        billable_activity_choices = [
            (c.code, c.label or c.code) for c in db_codes
        ]
    else:
        # Fallback to the global defaults if the table is empty.
        billable_activity_choices = BILLABLE_ACTIVITY_CHOICES

    error = None

    if request.method == "POST":
        form_type = request.form.get("form_type")

        # ---- New billable item ----
        if form_type == "billable_new":
            activity_code = (request.form.get("activity_code") or "").strip()

            # Improved: accept MM/DD/YYYY and YYYY-MM-DD
            raw_date_value = (
                (request.form.get("service_date") or "").strip()
                or (request.form.get("date") or "").strip()
                or (request.form.get("date_of_service") or "").strip()
            )

            def _parse_billable_date(value: str):
                value = (value or "").strip()
                if not value:
                    return None
                # Try ISO first
                try:
                    if "-" in value:
                        return datetime.strptime(value, "%Y-%m-%d").date()
                except ValueError:
                    pass
                # Then MM/DD/YYYY
                try:
                    return datetime.strptime(value, "%m/%d/%Y").date()
                except ValueError:
                    return None

            service_date_parsed = _parse_billable_date(raw_date_value)

            # Allow description to be optional, but never NULL at the DB level.
            raw_description = (request.form.get("description") or "").strip()
            description = raw_description if raw_description else None

            # Read notes field
            notes_raw = (request.form.get("notes") or "").strip()
            notes = notes_raw if notes_raw else None

            qty_raw = (request.form.get("quantity") or "").strip()
            quantity = float(qty_raw) if qty_raw else None

            # If description is still empty, fall back to the human label for this
            # activity code (e.g., "Mileage (miles)" for MIL) so the DB always
            # gets a non-NULL value.
            if not description and activity_code:
                label = None
                for code, label_text in billable_activity_choices:
                    if code == activity_code:
                        label = label_text
                        break
                description = label or activity_code

            if not activity_code:
                error = "Activity code is required."
            else:
                is_complete = _billable_is_complete(
                    activity_code, service_date_parsed, quantity
                )
                item = BillableItem(
                    claim_id=claim.id,
                    activity_code=activity_code,
                    date_of_service=service_date_parsed,
                    quantity=quantity,
                    description=description,
                    notes=notes,
                    is_complete=is_complete,
                )
                db.session.add(item)
                db.session.commit()
                return redirect(url_for("main.claim_detail", claim_id=claim.id))

        # ---- Document upload ----
        elif form_type == "document_upload":
            doc_type = (request.form.get("doc_type") or "").strip() or None
            description = (request.form.get("description") or "").strip() or None
            file = request.files.get("file")

            if not file or file.filename == "":
                error = "Please choose a file to upload."
            elif not _allowed_file(file.filename):
                error = "File type not allowed."
            else:
                claim_folder = _get_claim_folder(claim)

                # Build a readable, claim-specific stored filename and ensure uniqueness
                original_safe = secure_filename(file.filename)
                claim_number_part = _safe_segment(claim.claim_number) if claim.claim_number else f"claim_{claim.id}"

                base_name, ext = os.path.splitext(original_safe)
                base_name = _safe_segment(base_name) or "document"
                ext = ext or ""

                candidate = f"{claim_number_part}_{base_name}{ext}"
                stored_name = candidate
                counter = 1
                while (claim_folder / stored_name).exists():
                    stored_name = f"{claim_number_part}_{base_name}_{counter}{ext}"
                    counter += 1

                file_path = claim_folder / stored_name
                file.save(file_path)

                doc = ClaimDocument(
                    claim_id=claim.id,
                    original_filename=file.filename,
                    filename_stored=stored_name,
                    doc_type=doc_type,
                    description=description,
                    document_date=date.today().isoformat(),
                )
                db.session.add(doc)
                db.session.commit()
                return redirect(url_for("main.claim_detail", claim_id=claim.id))

        # ---- New report: just redirect to dedicated report creation route ----
        elif form_type == "report_new":
            report_type = (request.form.get("report_type") or "").strip().lower()

            # Require a valid report type selection
            if report_type not in ("initial", "progress", "closure"):
                error = "Report type is required."
            else:
                return redirect(
                    url_for("main.report_new", claim_id=claim.id, report_type=report_type)
                )

    return render_template(
        "claim_detail.html",
        active_page="claims",
        claim=claim,
        settings=settings,
        billable_items=billable_items,
        reports=reports,
        documents=documents,
        invoices=invoices,
        invoice_map=invoice_map,
        open_invoice_count=open_invoice_count,
        billable_activity_choices=billable_activity_choices,
        error=error,
    )


@bp.route("/claims/<int:claim_id>/reports/<int:report_id>")
def report_detail(claim_id, report_id):
    """Read-only view / preview of a single report."""
    claim = Claim.query.get_or_404(claim_id)
    report = Report.query.filter_by(id=report_id, claim_id=claim.id).first_or_404()

    # Load barrier options for display in read-only view
    barriers_by_category = _get_barrier_options_grouped()
    selected_barrier_ids = set()
    if report.barriers_json:
        try:
            data = json.loads(report.barriers_json)
            selected_barrier_ids = {int(x) for x in data}
        except (TypeError, ValueError):
            selected_barrier_ids = set()

    return render_template(
        "report_detail.html",
        active_page="claims",
        claim=claim,
        report=report,
        barriers_by_category=barriers_by_category,
        selected_barrier_ids=selected_barrier_ids,
    )


# ---- Append field from last report helper ----
@bp.route("/claims/<int:claim_id>/reports/append-field", methods=["GET"])
def report_append_field(claim_id):
    """Return the requested field's text from the most recent report for this claim.

    This is used by the UI "Append from last report" buttons to pre-fill a
    textarea with the same content Gina used in the previous report, so she can
    tweak it instead of retyping.
    """
    field = (request.args.get("field") or "").strip()

    # Only allow known-safe fields to be appended from the last report
    allowed_fields = {
        "work_status",
        "case_management_plan",
    }

    if field not in allowed_fields:
        return jsonify({"error": "Invalid field"}), 400

    last_report = (
        Report.query
        .filter_by(claim_id=claim_id)
        .order_by(Report.created_at.desc())
        .first()
    )

    if not last_report:
        # No prior reports to append from; return an empty string so the
        # frontend can handle it gracefully.
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
    report = (
        Report.query.filter_by(id=report_id, claim_id=claim.id)
        .first_or_404()
    )

    file = request.files.get("file")
    doc_type = (request.form.get("doc_type") or "").strip() or None
    description = (request.form.get("description") or "").strip() or None
    document_date = (request.form.get("document_date") or "").strip() or None

    if not file or not file.filename:
        flash("Please choose a file to upload.", "danger")
        return redirect(
            url_for("main.report_edit", claim_id=claim.id, report_id=report.id)
        )

    if not _allowed_file(file.filename):
        flash("File type not allowed.", "danger")
        return redirect(
            url_for("main.report_edit", claim_id=claim.id, report_id=report.id)
        )

    # Build a readable, claim/report-specific stored filename and ensure uniqueness
    report_folder = _get_report_folder(report)

    original_safe = secure_filename(file.filename)
    claim_number_part = _safe_segment(report.claim.claim_number) if report.claim and report.claim.claim_number else f"claim_{report.claim_id}"
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
    return redirect(
        url_for("main.report_edit", claim_id=claim.id, report_id=report.id)
    )

@bp.route("/claims/<int:claim_id>/reports/<int:report_id>/roll-forward/<string:field_name>", methods=["GET"])
def report_roll_forward(claim_id, report_id, field_name):
    """
    Return ONLY the requested field's content from the most recent prior report
    on this claim (excluding the current report itself).
    Used for per-field roll-forward buttons in the report editor.
    """
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

    # Find the previous report
    previous = (
        Report.query
        .filter(Report.claim_id == claim_id, Report.id != report_id)
        .order_by(Report.created_at.desc())
        .first()
    )

    if not previous:
        return jsonify({"value": ""}), 200

    value = getattr(previous, field_name, "") or ""
    return jsonify({"value": value}), 200

@bp.route("/claims/<int:claim_id>/reports/<int:report_id>/edit", methods=["GET", "POST"])
def report_edit(claim_id, report_id):
    """
    Edit an existing report for a claim.

    This manages:
    - report_type (initial/progress/closure)
    - DOS start / end
    - treating provider
    - shared long-text fields (status_treatment_plan, work_status, employment_status,
      case_management_plan)
    - Initial-specific fields (initial_* + next appointment)
    - Closure-specific fields (closure_* fields)
    - next_report_due
    """
    claim = Claim.query.get_or_404(claim_id)
    report = Report.query.filter_by(id=report_id, claim_id=claim.id).first_or_404()

    # Providers for treating-provider dropdown
    providers = Provider.query.order_by(Provider.name).all()

    error = None

    if request.method == "POST":
        report_type_raw = (request.form.get("report_type") or "").strip().lower()
        # If the form doesn't send report_type (e.g., type is fixed on edit),
        # fall back to the existing report.report_type value.
        if report_type_raw:
            report_type = report_type_raw
        else:
            report_type = (report.report_type or "").lower()

        dos_start_raw = (request.form.get("dos_start") or "").strip() or None
        dos_end_raw = (request.form.get("dos_end") or "").strip() or None
        work_status = (request.form.get("work_status") or "").strip() or None
        case_management_plan = (request.form.get("case_management_plan") or "").strip() or None
        next_report_due_raw = (request.form.get("next_report_due") or "").strip() or None

        treating_provider_id_raw = (request.form.get("treating_provider_id") or "").strip() or None

        status_treatment_plan = (request.form.get("status_treatment_plan") or "").strip() or None
        employment_status = (request.form.get("employment_status") or "").strip() or None
        primary_care_provider = (request.form.get("primary_care_provider") or "").strip() or None

        # Initial-specific clinical content
        initial_diagnosis = (request.form.get("initial_diagnosis") or "").strip() or None
        initial_mechanism_of_injury = (
            request.form.get("initial_mechanism_of_injury") or ""
        ).strip() or None
        initial_coexisting_conditions = (
            request.form.get("initial_coexisting_conditions") or ""
        ).strip() or None
        initial_surgical_history = (
            request.form.get("initial_surgical_history") or ""
        ).strip() or None
        initial_medications = (request.form.get("initial_medications") or "").strip() or None
        initial_diagnostics = (request.form.get("initial_diagnostics") or "").strip() or None

        # Next appointment (initial report)
        initial_next_appt_datetime_raw = (
            request.form.get("initial_next_appt_datetime") or ""
        ).strip()
        initial_next_appt_provider_name = (
            request.form.get("initial_next_appt_provider_name") or ""
        ).strip() or None

        # Closure-specific fields
        closure_reason = (request.form.get("closure_reason") or "").strip() or None
        closure_details = (request.form.get("closure_details") or "").strip() or None
        closure_case_management_impact = (
            request.form.get("closure_case_management_impact") or ""
        ).strip() or None

        # Barriers: list of selected BarrierOption IDs
        barrier_ids_raw = request.form.getlist("barrier_ids")
        barrier_ids = []
        for raw_id in barrier_ids_raw:
            try:
                barrier_ids.append(int(raw_id))
            except (TypeError, ValueError):
                continue

        # Use new MMDDYYYY parser for the three report date fields
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

        if not error:
            report.report_type = report_type
            report.dos_start = dos_start
            report.dos_end = dos_end
            report.work_status = work_status
            report.case_management_plan = case_management_plan
            report.next_report_due = next_report_due

            report.treating_provider_id = treating_provider_id
            report.status_treatment_plan = status_treatment_plan
            report.employment_status = employment_status

            # Persist initial-style clinical fields for all report types so that
            # roll-forward text is never lost, even on progress/closure reports.
            report.initial_diagnosis = initial_diagnosis
            report.initial_mechanism_of_injury = initial_mechanism_of_injury
            report.initial_coexisting_conditions = initial_coexisting_conditions
            report.initial_surgical_history = initial_surgical_history
            report.initial_medications = initial_medications
            report.initial_diagnostics = initial_diagnostics

            # Next appointment (stored in the same fields regardless of type,
            # but typically only used for initial reports).
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

            # Closure-specific fields
            report.closure_reason = closure_reason
            report.closure_details = closure_details
            report.closure_case_management_impact = closure_case_management_impact

            # Persist selected barriers (as JSON list of IDs)
            if barrier_ids:
                report.barriers_json = json.dumps(barrier_ids)
            else:
                report.barriers_json = None

            # If this is an initial report, update the claim's primary care provider
            if report_type == "initial":
                claim.primary_care_provider = primary_care_provider

            db.session.commit()
            return redirect(url_for("main.report_edit", claim_id=claim.id, report_id=report.id))

    # Load barrier options and decode any selections on this report
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
    )


# ---- Download route for report-level documents ----
@bp.route("/reports/documents/<int:report_document_id>/download")
def report_document_download(report_document_id):
    """
    Download a stored report document.

    We treat ReportDocument.stored_path as just the filename and always
    reconstruct the full path from the report's folder (like claim-level docs).
    """
    doc = ReportDocument.query.get_or_404(report_document_id)
    report = doc.report
    if not report or not report.claim:
        flash("Document is not linked to a valid report/claim.", "danger")
        return redirect(url_for("main.claims_list"))

    if not getattr(doc, "stored_path", None):
        flash("Document record is missing a stored filename.", "danger")
        return redirect(
            url_for(
                "main.report_edit",
                claim_id=report.claim.id,
                report_id=report.id,
            )
        )

    report_folder = _get_report_folder(report)
    file_path = report_folder / doc.stored_path

    if not file_path.exists():
        flash("File not found on disk.", "danger")
        return redirect(
            url_for(
                "main.report_edit",
                claim_id=report.claim.id,
                report_id=report.id,
            )
        )

    # Use send_file with the full absolute path so we’re not relying on
    # directory/path joining logic inside send_from_directory.
    return send_file(
        file_path,
        as_attachment=True,
        download_name=doc.original_filename #or doc.stored_path,
    )



# ---- Delete route for report-level documents ----
@bp.route("/reports/documents/<int:report_document_id>/delete", methods=["POST"])
def report_document_delete(report_document_id):
    """
    Delete a report-level document from disk and the database, then
    return to the corresponding report edit screen.
    """
    doc = ReportDocument.query.get_or_404(report_document_id)

    # Capture claim/report IDs for redirect before deleting
    claim_id = doc.report.claim_id if doc.report else None
    report_id = doc.report.id if doc.report else None

    # Remove the file from disk if we have a stored_path
    if getattr(doc, "stored_path", None) and doc.report and doc.report.claim:
        report_folder = _get_report_folder(doc.report)
        file_path = report_folder / doc.stored_path
        try:
            os.remove(file_path)
        except FileNotFoundError:
            # If the file is already gone, just proceed with DB delete
            pass

    db.session.delete(doc)
    db.session.commit()
    flash("Report document deleted.", "success")

    # Prefer to send the user back to the report edit screen
    if claim_id and report_id:
        return redirect(
            url_for("main.report_edit", claim_id=claim_id, report_id=report_id)
        )

    # Fallback: return to claims list if for some reason we don't have IDs
    return redirect(url_for("main.claims_list"))

@bp.route("/reports/documents/<int:report_document_id>/open-location", methods=["POST"])
def report_document_open_location(report_document_id):
    """
    Open the folder containing this report-level document in the OS file manager.
    """
    doc = ReportDocument.query.get_or_404(report_document_id)
    report = doc.report

    if not report or not report.claim:
        flash("Document is not linked to a valid report/claim.", "danger")
        return redirect(url_for("main.claims_list"))

    folder = _get_report_folder(report)
    _open_in_file_manager(folder)

    return redirect(
        url_for("main.report_edit", claim_id=report.claim.id, report_id=report.id)
    )

# ---- ICS file for report's next appointment ----

@bp.route("/claims/<int:claim_id>/reports/<int:report_id>/next-appointment.ics")
def report_next_appointment_ics(claim_id, report_id):
    """
    Generate a simple ICS calendar event for the report's next appointment.

    Uses:
    - report.initial_next_appt_datetime for DTSTART
    - report.treating_provider's address (if available) for LOCATION
    """
    claim = Claim.query.get_or_404(claim_id)
    report = Report.query.filter_by(id=report_id, claim_id=claim.id).first_or_404()

    # Require a next appointment datetime
    if not report.initial_next_appt_datetime:
        flash("This report does not have a next appointment date/time set.", "warning")
        return redirect(url_for("main.report_edit", claim_id=claim.id, report_id=report.id))

    dt = report.initial_next_appt_datetime

    # Build a basic LOCATION string from the treating provider, if available
    location_parts = []
    if report.treating_provider:
        p = report.treating_provider

        # Name / practice name
        if getattr(p, "name", None):
            location_parts.append(p.name)

        # Street lines: prefer address1/address2, but fall back to a single address field
        addr1 = getattr(p, "address1", None) or getattr(p, "address", None)
        addr2 = getattr(p, "address2", None)

        if addr1:
            location_parts.append(addr1)
        if addr2:
            location_parts.append(addr2)

        # City / state / postal code (support both postal_code and zip if present)
        city = getattr(p, "city", None)
        state = getattr(p, "state", None)
        postal = getattr(p, "postal_code", None) or getattr(p, "zip", None)

        city_state_zip = ", ".join(part for part in [city, state, postal] if part)
        if city_state_zip:
            location_parts.append(city_state_zip)

    location = ", ".join(location_parts) if location_parts else "TBD"

    # Summary and description
    summary = f"Next appointment – {claim.claimant_name or 'Claimant'}"
    description_lines = [
        f"Claim: {claim.claim_number or ''}",
        f"Claimant: {claim.claimant_name or ''}",
    ]
    if report.initial_next_appt_provider_name:
        description_lines.append(f"Provider: {report.initial_next_appt_provider_name}")
    description = "\\n".join(description_lines)

    # ICS timestamps
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
    """
    Render a print-friendly version of a report, including a
    Case Manager Activities narrative built from billable items
    in the report's DOS range.
    """
    claim = Claim.query.get_or_404(claim_id)
    report = (
        Report.query.filter_by(id=report_id, claim_id=claim.id)
        .first_or_404()
    )
    settings = _ensure_settings()

    # Build Case Manager Activities list from billable items
    cm_activities = []
    if report.dos_start and report.dos_end:
        q = (
            BillableItem.query
            .filter_by(claim_id=claim.id)
            .filter(BillableItem.date_of_service.isnot(None))
            .filter(BillableItem.date_of_service >= report.dos_start)
            .filter(BillableItem.date_of_service <= report.dos_end)
        )

        # Exclude pure expenses; we only want time/mileage/etc.
        q = q.filter(BillableItem.activity_code != "EXP")

        items = (
            q.order_by(
                BillableItem.date_of_service.asc().nullslast(),
                BillableItem.created_at.asc(),
            )
            .all()
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

    return render_template(
        "report_print.html",
        active_page="claims",
        settings=settings,
        claim=claim,
        report=report,
        cm_activities=cm_activities,
    )


# ---- PDF generation for a single report using WeasyPrint ----
@bp.route("/claims/<int:claim_id>/reports/<int:report_id>/pdf")
def report_pdf(claim_id, report_id):
    """
    Generate a PDF of the report using the same template as report_print.
    """
    claim = Claim.query.get_or_404(claim_id)
    report = (
        Report.query.filter_by(id=report_id, claim_id=claim.id)
        .first_or_404()
    )

    # Include any report-level documents in case they are referenced in the print view
    documents = (
        ReportDocument.query
        .filter_by(report_id=report.id)
        .order_by(ReportDocument.id.desc())
        .all()
    )

    settings = _ensure_settings()

    # If WeasyPrint is not available, fall back to the HTML print view
    if HTML is None:
        flash("PDF generation is not available (WeasyPrint is not installed).", "danger")
        return redirect(
            url_for("main.report_print", claim_id=claim.id, report_id=report.id)
        )

    # Render the HTML for this report
    html = render_template(
        "report_print.html",
        active_page="claims",
        claim=claim,
        report=report,
        settings=settings,
        documents=documents,
    )

    # Generate PDF bytes in memory
    pdf_bytes = HTML(string=html, base_url=current_app.root_path).write_pdf()

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"report_claim{claim.id}_report{report.id}.pdf",
    )

@bp.route("/claims/<int:claim_id>/reports/<int:report_id>/delete", methods=["GET", "POST"])
def report_delete(claim_id, report_id):
    """
    Delete a report and return to the claim detail page.

    For now we skip a separate confirmation template and delete immediately
    when this endpoint is hit via GET or POST. The UI should still make it
    clear that this is a destructive action.
    """
    claim = Claim.query.get_or_404(claim_id)
    report = Report.query.filter_by(id=report_id, claim_id=claim.id).first_or_404()

    db.session.delete(report)
    db.session.commit()
    flash("Report deleted successfully.", "success")

    return redirect(url_for("main.claim_detail", claim_id=claim.id))

# --- Billable item delete route ---


# --- Billable item delete route ---
@bp.route("/claims/<int:claim_id>/billable/<int:item_id>/delete", methods=["POST"])
def billable_delete(claim_id, item_id):
    claim = Claim.query.get_or_404(claim_id)
    item = BillableItem.query.filter_by(id=item_id, claim_id=claim.id).first_or_404()

    db.session.delete(item)
    db.session.commit()

    return redirect(url_for("main.claim_detail", claim_id=claim.id))


@bp.route("/claims/<int:claim_id>/documents/<int:doc_id>/download")
def document_download(claim_id, doc_id):
    claim = Claim.query.get_or_404(claim_id)
    doc = ClaimDocument.query.filter_by(id=doc_id, claim_id=claim.id).first_or_404()

    claim_folder = _get_claim_folder(claim)
    return send_from_directory(
        claim_folder,
        doc.filename_stored,
        as_attachment=True,
        download_name=doc.original_filename,
    )

@bp.route("/claims/<int:claim_id>/documents/<int:doc_id>/delete", methods=["POST"])
def document_delete(claim_id, doc_id):
    """Delete a claim-level document from disk and the database."""
    claim = Claim.query.get_or_404(claim_id)

    doc = ClaimDocument.query.filter_by(id=doc_id, claim_id=claim.id).first_or_404()

    # Remove the file from disk, if present
    folder = _get_claim_folder(claim)
    if getattr(doc, "filename_stored", None):
        file_path = os.path.join(folder, doc.filename_stored)
        try:
            os.remove(file_path)
        except FileNotFoundError:
            # If the file is already gone, just proceed with DB delete
            pass

    db.session.delete(doc)
    db.session.commit()

    flash("Document deleted.", "success")
    return redirect(url_for("main.claim_detail", claim_id=claim.id))

@bp.route("/claims/<int:claim_id>/documents/<int:doc_id>/open-location", methods=["POST"])
def document_open_location(claim_id, doc_id):
    """
    Open the folder containing this claim-level document in the OS file manager.
    """
    claim = Claim.query.get_or_404(claim_id)
    # Ensure the document exists and belongs to this claim
    ClaimDocument.query.filter_by(id=doc_id, claim_id=claim.id).first_or_404()

    folder = _get_claim_folder(claim)
    _open_in_file_manager(folder)

    return redirect(url_for("main.claim_detail", claim_id=claim.id))

@bp.route("/carriers")
def carriers_list():
    carriers = Carrier.query.order_by(Carrier.name).all()
    return render_template(
        "carriers_list.html",
        active_page="carriers",
        carriers=carriers,
    )


@bp.route("/carriers/new", methods=["GET", "POST"])
def carrier_new():
    error = None

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        address1 = (request.form.get("address1") or "").strip() or None
        address2 = (request.form.get("address2") or "").strip() or None
        city = (request.form.get("city") or "").strip() or None
        state = (request.form.get("state") or "").strip() or None
        postal_code = (request.form.get("postal_code") or "").strip() or None
        phone = (request.form.get("phone") or "").strip() or None
        fax = (request.form.get("fax") or "").strip() or None
        email = (request.form.get("email") or "").strip() or None

        if not _validate_email(email):
            error = "Email address looks invalid."
        elif not _validate_phone(phone):
            error = "Phone number looks invalid."
        elif not _validate_postal_code(postal_code):
            error = "Postal code must be 5 digits or ZIP+4 (e.g. 83701 or 83701-1234)."
        elif not name:
            error = "Name is required."

        if not error:
            carrier = Carrier(
                name=name,
                address1=address1,
                address2=address2,
                city=city,
                state=state,
                postal_code=postal_code,
                phone=phone,
                fax=fax,
                email=email,
            )
            db.session.add(carrier)
            db.session.commit()
            return redirect(url_for("main.carriers_list"))

    return render_template(
        "carrier_new.html",
        active_page="carriers",
        error=error,
    )


@bp.route("/employers")
def employers_list():
    employers = Employer.query.order_by(Employer.name).all()
    return render_template(
        "employers_list.html",
        active_page="employers",
        employers=employers,
    )


@bp.route("/employers/new", methods=["GET", "POST"])
def employer_new():
    carriers = Carrier.query.order_by(Carrier.name).all()
    error = None

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        address1 = (request.form.get("address1") or "").strip() or None
        address2 = (request.form.get("address2") or "").strip() or None
        city = (request.form.get("city") or "").strip() or None
        state = (request.form.get("state") or "").strip() or None
        postal_code = (request.form.get("postal_code") or "").strip() or None
        phone = (request.form.get("phone") or "").strip() or None
        fax = (request.form.get("fax") or "").strip() or None
        email = (request.form.get("email") or "").strip() or None

        carrier_id_raw = (request.form.get("carrier_id") or "").strip()

        # Basic validation
        if not _validate_email(email):
            error = "Email address looks invalid."
        elif not _validate_phone(phone):
            error = "Phone number looks invalid."
        elif not _validate_postal_code(postal_code):
            error = "Postal code must be 5 digits or ZIP+4 (e.g. 83701 or 83701-1234)."
        elif not name:
            error = "Name is required."

        if not error:
            employer = Employer(
                name=name,
                address1=address1,
                address2=address2,
                city=city,
                state=state,
                postal_code=postal_code,
                phone=phone,
                fax=fax,
                email=email,
            )

            # Safely coerce carrier_id to an int or None
            carrier_id = None
            if carrier_id_raw:
                try:
                    carrier_id = int(carrier_id_raw)
                except ValueError:
                    carrier_id = None

            employer.carrier_id = carrier_id

            db.session.add(employer)
            db.session.commit()
            return redirect(url_for("main.employers_list"))

    return render_template(
        "employer_new.html",
        active_page="employers",
        carriers=carriers,
        error=error,
    )


@bp.route("/providers")
def providers_list():
    providers = Provider.query.order_by(Provider.name).all()
    return render_template(
        "providers_list.html",
        active_page="providers",
        providers=providers,
    )


@bp.route("/providers/new", methods=["GET", "POST"])
def provider_new():
    error = None

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        address1 = (request.form.get("address1") or "").strip() or None
        address2 = (request.form.get("address2") or "").strip() or None
        city = (request.form.get("city") or "").strip() or None
        state = (request.form.get("state") or "").strip() or None
        postal_code = (request.form.get("postal_code") or "").strip() or None
        phone = (request.form.get("phone") or "").strip() or None
        fax = (request.form.get("fax") or "").strip() or None
        email = (request.form.get("email") or "").strip() or None

        if not _validate_email(email):
            error = "Email address looks invalid."
        elif not _validate_phone(phone):
            error = "Phone number looks invalid."
        elif not _validate_postal_code(postal_code):
            error = "Postal code must be 5 digits or ZIP+4 (e.g. 83701 or 83701-1234)."
        elif not name:
            error = "Name is required."

        if not error:
            provider = Provider(
                name=name,
                address1=address1,
                address2=address2,
                city=city,
                state=state,
                postal_code=postal_code,
                phone=phone,
                fax=fax,
                email=email,
            )
            db.session.add(provider)
            db.session.commit()
            return redirect(url_for("main.providers_list"))

    return render_template(
        "provider_new.html",
        active_page="providers",
        error=error,
    )

@bp.route("/carriers/<int:carrier_id>/edit", methods=["GET", "POST"])
def carrier_edit(carrier_id):
    carrier = Carrier.query.get_or_404(carrier_id)
    error = None

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        address1 = (request.form.get("address1") or "").strip() or None
        address2 = (request.form.get("address2") or "").strip() or None
        city = (request.form.get("city") or "").strip() or None
        state = (request.form.get("state") or "").strip() or None
        postal_code = (request.form.get("postal_code") or "").strip() or None

        phone = (request.form.get("phone") or "").strip() or None
        fax = (request.form.get("fax") or "").strip() or None
        email = (request.form.get("email") or "").strip() or None

        if not _validate_email(email):
            error = "Email address looks invalid."
        elif not _validate_phone(phone):
            error = "Phone number looks invalid."
        elif not _validate_postal_code(postal_code):
            error = "Postal code must be 5 digits or ZIP+4 (e.g. 83701 or 83701-1234)."
        elif not name:
            error = "Name is required."

        if not error:
            carrier.name = name
            carrier.address1 = address1
            carrier.address2 = address2
            carrier.city = city
            carrier.state = state
            carrier.postal_code = postal_code
            carrier.phone = phone
            carrier.fax = fax
            carrier.email = email

            db.session.commit()
            return redirect(url_for("main.carriers_list"))

    return render_template(
        "carrier_edit.html",
        active_page="carriers",
        carrier=carrier,
        error=error,
    )


@bp.route("/employers/<int:employer_id>/edit", methods=["GET", "POST"])
def employer_edit(employer_id):
    employer = Employer.query.get_or_404(employer_id)
    carriers = Carrier.query.order_by(Carrier.name).all()
    error = None

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        city = (request.form.get("city") or "").strip() or None
        address1 = (request.form.get("address1") or "").strip() or None
        address2 = (request.form.get("address2") or "").strip() or None
        state = (request.form.get("state") or "").strip() or None
        postal_code = (request.form.get("postal_code") or "").strip() or None
        phone = (request.form.get("phone") or "").strip() or None
        fax = (request.form.get("fax") or "").strip() or None
        email = (request.form.get("email") or "").strip() or None

        carrier_id_raw = (request.form.get("carrier_id") or "").strip()
        carrier_id = None
        if carrier_id_raw:
            try:
                carrier_id = int(carrier_id_raw)
            except ValueError:
                carrier_id = None

        # Basic validation similar to employer_new/carrier_edit
        if not _validate_email(email):
            error = "Email address looks invalid."
        elif not _validate_phone(phone):
            error = "Phone number looks invalid."
        elif not _validate_postal_code(postal_code):
            error = "Postal code must be 5 digits or ZIP+4 (e.g. 83701 or 83701-1234)."
        elif not name:
            error = "Name is required."

        if not error:
            employer.name = name
            employer.city = city
            employer.address1 = address1
            employer.address2 = address2
            employer.state = state
            employer.postal_code = postal_code
            employer.phone = phone
            employer.fax = fax
            employer.email = email
            employer.carrier_id = carrier_id

            db.session.commit()
            flash("Employer updated successfully.", "success")
            return redirect(url_for("main.employer_detail", employer_id=employer.id))

    return render_template(
        "employer_edit.html",
        active_page="employers",
        employer=employer,
        carriers=carriers,
        error=error,
    )


@bp.route("/providers/<int:provider_id>/edit", methods=["GET", "POST"])
def provider_edit(provider_id):
    provider = Provider.query.get_or_404(provider_id)
    error = None

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        address1 = (request.form.get("address1") or "").strip() or None
        address2 = (request.form.get("address2") or "").strip() or None
        city = (request.form.get("city") or "").strip() or None
        state = (request.form.get("state") or "").strip() or None
        postal_code = (request.form.get("postal_code") or "").strip() or None

        phone = (request.form.get("phone") or "").strip() or None
        fax = (request.form.get("fax") or "").strip() or None
        email = (request.form.get("email") or "").strip() or None

        if not _validate_email(email):
            error = "Email address looks invalid."
        elif not _validate_phone(phone):
            error = "Phone number looks invalid."
        elif not _validate_postal_code(postal_code):
            error = "Postal code must be 5 digits or ZIP+4 (e.g. 83701 or 83701-1234)."
        elif not name:
            error = "Name is required."

        if not error:
            provider.name = name
            provider.address1 = address1
            provider.address2 = address2
            provider.city = city
            provider.state = state
            provider.postal_code = postal_code
            provider.phone = phone
            provider.fax = fax
            provider.email = email

            db.session.commit()
            return redirect(url_for("main.providers_list"))

    return render_template(
        "provider_edit.html",
        active_page="providers",
        provider=provider,
        error=error,
    )


# ---- Delete routes for carriers, employers, providers ----

@bp.route("/carriers/<int:carrier_id>/delete", methods=["POST"])
def carrier_delete(carrier_id):
    """
    Delete a carrier if it is not referenced by any claims.
    If it is in use, show a warning and do not delete.
    """
    carrier = Carrier.query.get_or_404(carrier_id)

    # Check if any claim still uses this carrier
    in_use = Claim.query.filter_by(carrier_id=carrier.id).first()
    if in_use:
        flash("Cannot delete carrier; it is referenced by one or more claims.", "warning")
        return redirect(url_for("main.carriers_list"))

    db.session.delete(carrier)
    db.session.commit()
    flash("Carrier deleted.", "success")
    return redirect(url_for("main.carriers_list"))


@bp.route("/employers/<int:employer_id>/delete", methods=["POST"])
def employer_delete(employer_id):
    """
    Delete an employer if it is not referenced by any claims.
    If it is in use, show a warning and do not delete.
    """
    employer = Employer.query.get_or_404(employer_id)

    # Check if any claim still uses this employer
    in_use = Claim.query.filter_by(employer_id=employer.id).first()
    if in_use:
        flash("Cannot delete employer; it is referenced by one or more claims.", "warning")
        return redirect(url_for("main.employers_list"))

    db.session.delete(employer)
    db.session.commit()
    flash("Employer deleted.", "success")
    return redirect(url_for("main.employers_list"))


@bp.route("/providers/<int:provider_id>/delete", methods=["POST"])
def provider_delete(provider_id):
    """
    Delete a provider. If in the future providers are linked to claims or other
    records, we can add a similar safety check here.
    """
    provider = Provider.query.get_or_404(provider_id)

    # If Claim.provider_id ever exists, we can protect against deletes-in-use:
    # in_use = Claim.query.filter_by(provider_id=provider.id).first()
    # if in_use:
    #     flash("Cannot delete provider; it is referenced by one or more claims.", "warning")
    #     return redirect(url_for("main.providers_list"))

    db.session.delete(provider)
    db.session.commit()
    flash("Provider deleted.", "success")
    return redirect(url_for("main.providers_list"))

@bp.route("/carriers/<int:carrier_id>")
def carrier_detail(carrier_id):
    carrier = Carrier.query.get_or_404(carrier_id)

    # Load contacts linked to this carrier
    contacts = (
        Contact.query.filter_by(carrier_id=carrier.id)
        .order_by(Contact.name)
        .all()
    )

    # Optional: contact being edited (for the inline form)
    edit_contact = None
    edit_contact_id = request.args.get("edit_contact_id")
    if edit_contact_id:
        try:
            cid = int(edit_contact_id)
        except (TypeError, ValueError):
            cid = None
        if cid is not None:
            edit_contact = (
                Contact.query
                .filter_by(id=cid, carrier_id=carrier.id)
                .first()
            )

    contact_roles = _get_contact_roles()
    return render_template(
        "carrier_detail.html",
        active_page="carriers",
        carrier=carrier,
        contacts=contacts,
        edit_contact=edit_contact,
        contact_roles=contact_roles,
    )


@bp.route("/employers/<int:employer_id>")
def employer_detail(employer_id):
    employer = Employer.query.get_or_404(employer_id)

    # Load contacts linked to this employer
    contacts = (
        Contact.query.filter_by(employer_id=employer.id)
        .order_by(Contact.name)
        .all()
    )

    # Optional: contact being edited (for the inline form)
    edit_contact = None
    edit_contact_id = request.args.get("edit_contact_id")
    if edit_contact_id:
        try:
            cid = int(edit_contact_id)
        except (TypeError, ValueError):
            cid = None
        if cid is not None:
            edit_contact = (
                Contact.query
                .filter_by(id=cid, employer_id=employer.id)
                .first()
            )

    contact_roles = _get_contact_roles()
    return render_template(
        "employer_detail.html",
        active_page="employers",
        employer=employer,
        contacts=contacts,
        edit_contact=edit_contact,
        contact_roles=contact_roles,
    )


@bp.route("/providers/<int:provider_id>")
def provider_detail(provider_id):
    provider = Provider.query.get_or_404(provider_id)

    # Load contacts linked to this provider
    contacts = (
        Contact.query.filter_by(provider_id=provider.id)
        .order_by(Contact.name)
        .all()
    )

    # Optional: contact being edited (for the inline form)
    edit_contact = None
    edit_contact_id = request.args.get("edit_contact_id")
    if edit_contact_id:
        try:
            cid = int(edit_contact_id)
        except (TypeError, ValueError):
            cid = None
        if cid is not None:
            edit_contact = (
                Contact.query
                .filter_by(id=cid, provider_id=provider.id)
                .first()
            )

    contact_roles = _get_contact_roles()
    return render_template(
        "provider_detail.html",
        active_page="providers",
        provider=provider,
        contacts=contacts,
        edit_contact=edit_contact,
        contact_roles=contact_roles,
    )

# ---------- Contacts (generic for carrier / employer / provider) ----------

@bp.route("/contacts/new/<string:parent_type>/<int:parent_id>", methods=["POST"]) 
def contact_new(parent_type, parent_id):
    """Create or update a Contact for a carrier, employer, or provider.

    parent_type: 'carrier' | 'employer' | 'provider'
    parent_id:   ID of that parent record
    """
    # If a contact_id is present, we treat this as an edit of an existing contact
    contact_id_raw = request.form.get("contact_id")
    contact = None
    if contact_id_raw:
        try:
            cid = int(contact_id_raw)
        except (TypeError, ValueError):
            cid = None
        if cid is not None:
            contact = Contact.query.get(cid)

    name = (request.form.get("name") or "").strip()
    role = (request.form.get("role") or "").strip() or None
    phone = (request.form.get("phone") or "").strip() or None
    fax = (request.form.get("fax") or "").strip() or None
    email = (request.form.get("email") or "").strip() or None
    notes = (request.form.get("notes") or "").strip() or None

    if not name:
        # If somehow submitted without a name, just bounce back to the parent
        if parent_type == "carrier":
            return redirect(url_for("main.carrier_detail", carrier_id=parent_id))
        if parent_type == "employer":
            return redirect(url_for("main.employer_detail", employer_id=parent_id))
        if parent_type == "provider":
            return redirect(url_for("main.provider_detail", provider_id=parent_id))
        # Fallback
        return redirect(url_for("main.settings_view"))

    # If no existing contact found, create a new one; otherwise update in place
    if contact is None:
        contact = Contact()
        db.session.add(contact)

    contact.name = name
    contact.role = role
    contact.phone = phone
    contact.fax = fax
    contact.email = email
    contact.notes = notes

    # Attach to the correct parent (and clear other parent links to keep it consistent)
    if parent_type == "carrier":
        carrier = Carrier.query.get_or_404(parent_id)
        contact.carrier_id = carrier.id
        contact.employer_id = None
        contact.provider_id = None
        redirect_target = url_for("main.carrier_detail", carrier_id=parent_id)
    elif parent_type == "employer":
        employer = Employer.query.get_or_404(parent_id)
        contact.employer_id = employer.id
        contact.carrier_id = None
        contact.provider_id = None
        redirect_target = url_for("main.employer_detail", employer_id=parent_id)
    elif parent_type == "provider":
        provider = Provider.query.get_or_404(parent_id)
        contact.provider_id = provider.id
        contact.carrier_id = None
        contact.employer_id = None
        redirect_target = url_for("main.provider_detail", provider_id=parent_id)
    else:
        # Unknown parent type – just send them home
        redirect_target = url_for("main.settings_view")

    db.session.commit()
    return redirect(redirect_target)

# ---- Inline Contact Edit Route ----
@bp.route("/contacts/<int:contact_id>/update/<string:parent_type>/<int:parent_id>", methods=["POST"])
def contact_update(contact_id, parent_type, parent_id):
    """Update an existing contact and return to the correct parent detail page."""
    contact = Contact.query.get_or_404(contact_id)

    name = (request.form.get("name") or "").strip()
    role = (request.form.get("role") or "").strip() or None
    phone = (request.form.get("phone") or "").strip() or None
    fax = (request.form.get("fax") or "").strip() or None
    email = (request.form.get("email") or "").strip() or None
    notes = (request.form.get("notes") or "").strip() or None

    if not name:
        # If somehow submitted without a name, just bounce back
        if parent_type == "carrier":
            return redirect(url_for("main.carrier_detail", carrier_id=parent_id))
        if parent_type == "employer":
            return redirect(url_for("main.employer_detail", employer_id=parent_id))
        if parent_type == "provider":
            return redirect(url_for("main.provider_detail", provider_id=parent_id))
        return redirect(url_for("main.settings_view"))

    contact.name = name
    contact.role = role
    contact.phone = phone
    contact.fax = fax
    contact.email = email
    contact.notes = notes

    db.session.commit()

    if parent_type == "carrier":
        return redirect(url_for("main.carrier_detail", carrier_id=parent_id))
    if parent_type == "employer":
        return redirect(url_for("main.employer_detail", employer_id=parent_id))
    if parent_type == "provider":
        return redirect(url_for("main.provider_detail", provider_id=parent_id))

    return redirect(url_for("main.settings_view"))


@bp.route("/contacts/<int:contact_id>/delete", methods=["POST"])
def contact_delete(contact_id):
    """
    Delete a contact after a confirmation. Redirect back to whichever
    parent (carrier/employer/provider) it belongs to.
    """
    contact = Contact.query.get_or_404(contact_id)

    carrier_id = contact.carrier_id
    employer_id = contact.employer_id
    provider_id = contact.provider_id

    db.session.delete(contact)
    db.session.commit()

    if carrier_id:
        return redirect(url_for("main.carrier_detail", carrier_id=carrier_id))
    if employer_id:
        return redirect(url_for("main.employer_detail", employer_id=employer_id))
    if provider_id:
        return redirect(url_for("main.provider_detail", provider_id=provider_id))

    # Fallback if somehow unlinked
    return redirect(url_for("main.settings_view"))

@bp.route("/settings", methods=["GET", "POST"])
def settings_view():
    settings = _ensure_settings()
    error = None

    if request.method == "POST":
        # --- Basic business info ---
        settings.business_name = (request.form.get("business_name") or "").strip() or None
        settings.address1 = (request.form.get("address1") or "").strip() or None
        settings.address2 = (request.form.get("address2") or "").strip() or None
        settings.city = (request.form.get("city") or "").strip() or None
        settings.state = (request.form.get("state") or "").strip() or None
        settings.postal_code = (request.form.get("postal_code") or "").strip() or None
        settings.phone = (request.form.get("phone") or "").strip() or None
        settings.email = (request.form.get("email") or "").strip() or None

        # --- Rates ---
        try:
            settings.hourly_rate = float(request.form.get("hourly_rate") or 0) or None
        except ValueError:
            settings.hourly_rate = None

        try:
            settings.telephonic_rate = float(request.form.get("telephonic_rate") or 0) or None
        except ValueError:
            settings.telephonic_rate = None

        try:
            settings.mileage_rate = float(request.form.get("mileage_rate") or 0) or None
        except ValueError:
            settings.mileage_rate = None

        # --- Payment terms / footer text ---
        settings.payment_terms_default = (request.form.get("payment_terms_default") or "").strip() or None
        settings.report_footer_text = (request.form.get("report_footer_text") or "").strip() or None
        settings.invoice_footer_text = (request.form.get("invoice_footer_text") or "").strip() or None

        # --- Workload targets / dormant days ---
        try:
            settings.dormant_claim_days = int(request.form.get("dormant_claim_days") or 0) or None
        except ValueError:
            settings.dormant_claim_days = None

        try:
            settings.target_min_hours_per_week = float(
                request.form.get("target_min_hours_per_week") or 0
            ) or None
        except ValueError:
            settings.target_min_hours_per_week = None

        try:
            settings.target_max_hours_per_week = float(
                request.form.get("target_max_hours_per_week") or 0
            ) or None
        except ValueError:
            settings.target_max_hours_per_week = None

        # --- Accent color ---
        settings.accent_color = (request.form.get("accent_color") or "").strip() or None

        # --- Documents root path ---
        settings.documents_root = (request.form.get("documents_root") or "").strip() or None

        # --- Contact roles (editable list, one per line) ---
        roles_text = (request.form.get("contact_roles") or "").strip()
        if roles_text:
            roles_list = [
                line.strip()
                for line in roles_text.splitlines()
                if line.strip()
            ]
        else:
            roles_list = []

        if roles_list:
            settings.contact_roles_json = json.dumps(roles_list)
        else:
            # If nothing provided, fall back to defaults
            settings.contact_roles_json = json.dumps(CONTACT_ROLE_DEFAULTS)

        # --- Logo file upload handling ---
        logo_file = request.files.get("logo_file")
        if logo_file and logo_file.filename:
            safe_name = secure_filename(logo_file.filename)
            unique_prefix = uuid.uuid4().hex[:8]
            stored_name = f"{unique_prefix}_{safe_name}"

            static_root = Path(current_app.root_path) / "static"
            logos_folder = static_root / "logos"
            logos_folder.mkdir(parents=True, exist_ok=True)

            file_path = logos_folder / stored_name
            logo_file.save(file_path)

            # Store relative path; used with url_for('static', filename=settings.logo_path)
            settings.logo_path = f"logos/{stored_name}"
        # IMPORTANT: do NOT clear settings.logo_path if no file is uploaded

        db.session.commit()
        flash("Settings saved.", "success")
        return redirect(url_for("main.settings_view"))

    # GET: build the roles text from stored JSON or defaults
    contact_roles = _get_contact_roles()
    contact_roles_text = "\n".join(contact_roles)

    return render_template(
        "settings.html",
        active_page="settings",
        settings=settings,
        error=error,
        contact_roles=contact_roles,
        contact_roles_text=contact_roles_text,
    )
    
   # ---- Settings: Barrier Options management ----

@bp.route("/settings/barriers", methods=["GET", "POST"])
def settings_barriers():
    """
    Manage BarrierOption entries (used in report barriers checklist).

    GET: render list of all barrier options with add form.
    POST: create a new barrier option from the inline form.
    """
    settings = _ensure_settings()  # reuse for header / business name
        # Auto-seed default barrier options if the table is empty so that
    # there is exactly one canonical list used everywhere (settings + reports).
    if BarrierOption.query.count() == 0:
        default_barriers = [
            ("General", "Depression / PTSD / Psychosocial", 10),
            ("General", "Smoker", 20),
            ("General", "Treatment Noncompliance", 30),
            ("General", "Diabetes", 40),
            ("General", "Frequently Missing Work", 50),
            ("General", "Hypertension", 60),
            ("General", "Substance Abuse History", 70),
            ("General", "Pain Management", 80),
            ("General", "Legal Representation", 90),
            ("General", "Surgery or Recent Hospital Stay", 100),
            ("General", "Late Injury Reporting", 110),
        ]
        for category, label, sort_order in default_barriers:
            db.session.add(
                BarrierOption(
                    category=category,
                    label=label,
                    sort_order=sort_order,
                    is_active=True,
                )
            )
        db.session.commit()
    error = None

    if request.method == "POST":
        label = (request.form.get("label") or "").strip()
        category = (request.form.get("category") or "").strip() or None
        sort_order_raw = (request.form.get("sort_order") or "").strip()

        sort_order = None
        if sort_order_raw:
            try:
                sort_order = int(sort_order_raw)
            except ValueError:
                sort_order = None

        if not label:
            error = "Label is required for a barrier."
        else:
            # Default sort order so new ones fall to the bottom if not specified
            if sort_order is None:
                sort_order = 999

            opt = BarrierOption(
                label=label,
                category=category,
                sort_order=sort_order,
                is_active=True,
            )
            db.session.add(opt)
            db.session.commit()
            flash("Barrier added.", "success")
            return redirect(url_for("main.settings_barriers"))

    # List all options (active + inactive) grouped by category then sort_order/label.
    options = (
        BarrierOption.query
        .order_by(
            BarrierOption.category.nullsfirst(),
            BarrierOption.sort_order,
            BarrierOption.label,
        )
        .all()
    )

    return render_template(
        "settings_barriers.html",
        active_page="settings",
        settings=settings,
        options=options,
        error=error,
    )


@bp.route("/settings/barriers/<int:barrier_id>/edit", methods=["GET", "POST"])
def settings_barrier_edit(barrier_id):
    """
    Edit an existing BarrierOption.
    """
    settings = _ensure_settings()
    barrier = BarrierOption.query.get_or_404(barrier_id)
    error = None

    if request.method == "POST":
        label = (request.form.get("label") or "").strip()
        category = (request.form.get("category") or "").strip() or None
        sort_order_raw = (request.form.get("sort_order") or "").strip()
        is_active_raw = (request.form.get("is_active") or "").strip().lower()

        sort_order = None
        if sort_order_raw:
            try:
                sort_order = int(sort_order_raw)
            except ValueError:
                sort_order = None

        if not label:
            error = "Label is required for a barrier."
        else:
            barrier.label = label
            barrier.category = category
            if sort_order is not None:
                barrier.sort_order = sort_order

            barrier.is_active = is_active_raw in ("on", "true", "1", "yes")

            db.session.commit()
            flash("Barrier updated.", "success")
            return redirect(url_for("main.settings_barriers"))

    return render_template(
        "settings_barrier_form.html",
        active_page="settings",
        settings=settings,
        barrier=barrier,
        error=error,
    )


@bp.route("/settings/barriers/<int:barrier_id>/toggle", methods=["POST"])
def settings_barrier_toggle(barrier_id):
    """
    Quick toggle for a barrier's active flag from the list view.
    """
    barrier = BarrierOption.query.get_or_404(barrier_id)
    barrier.is_active = not bool(barrier.is_active)
    db.session.commit()
    flash("Barrier status updated.", "success")
    return redirect(url_for("main.settings_barriers")) 

# ---- Billable edit route ----
@bp.route("/claims/<int:claim_id>/billable/<int:item_id>/edit", methods=["GET", "POST"])
def billable_edit(claim_id, item_id):
    """
    Edit a billable item for a claim.
    """
    claim = Claim.query.get_or_404(claim_id)
    item = BillableItem.query.filter_by(id=item_id, claim_id=claim.id).first_or_404()
    # Build billable activity choices from the BillingActivityCode table.
    db_codes = (
        BillingActivityCode.query
        .filter_by(is_active=True)
        .order_by(BillingActivityCode.sort_order, BillingActivityCode.code)
        .all()
    )
    if db_codes:
        billable_activity_choices = [
            (c.code, c.label or c.code) for c in db_codes
        ]
    else:
        billable_activity_choices = BILLABLE_ACTIVITY_CHOICES

    error = None
    if request.method == "POST":
        # Read form fields
        raw_date_value = (
            (request.form.get("service_date") or "").strip()
            or (request.form.get("date") or "").strip()
            or (request.form.get("date_of_service") or "").strip()
        )
        def _parse_billable_date(value: str):
            value = (value or "").strip()
            if not value:
                return None
            # Try ISO first
            try:
                if "-" in value:
                    return datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                pass
            # Then MM/DD/YYYY
            try:
                return datetime.strptime(value, "%m/%d/%Y").date()
            except ValueError:
                return None
        service_date_parsed = _parse_billable_date(raw_date_value)
        activity_code = (request.form.get("activity_code") or "").strip()
        qty_raw = (request.form.get("quantity") or "").strip()
        quantity = float(qty_raw) if qty_raw else None
        raw_description = (request.form.get("description") or "").strip()
        description = raw_description if raw_description else None
        # Read notes field
        notes_raw = (request.form.get("notes") or "").strip()
        notes = notes_raw if notes_raw else None

        # If description is still empty, fall back to the human label for this
        # activity code
        if not description and activity_code:
            label = None
            for code, label_text in billable_activity_choices:
                if code == activity_code:
                    label = label_text
                    break
            description = label or activity_code

        if not activity_code:
            error = "Activity code is required."
        else:
            is_complete = _billable_is_complete(
                activity_code, service_date_parsed, quantity
            )
            item.activity_code = activity_code
            item.date_of_service = service_date_parsed
            item.quantity = quantity
            item.description = description
            item.notes = notes
            item.is_complete = is_complete
            db.session.commit()
            return redirect(url_for("main.claim_detail", claim_id=claim.id))

    return render_template(
        "billable_edit.html",
        active_page="claims",
        claim=claim,
        item=item,
        billable_activity_choices=billable_activity_choices,
        error=error,
    )