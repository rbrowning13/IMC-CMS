import os
import uuid
from pathlib import Path
from datetime import date
import json
from collections import defaultdict
from datetime import datetime
from sqlalchemy.exc import IntegrityError
from .models import Carrier, Claim, Employer, Contact


from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    current_app,
    send_from_directory,
    flash,  # <-- added flash
    abort,
    jsonify,
)
from werkzeug.utils import secure_filename
from markupsafe import Markup, escape  # <-- ADD THIS LINE

from .extensions import db
from .models import (
    Settings,
    Claim,
    BillableItem,
    Report,
    ClaimDocument,
    ReportDocument,
    Invoice,
    Carrier,
    Employer,
    Provider,
    Contact,
    BarrierOption,
)

bp = Blueprint("main", __name__)

# ---- Billable activity choices (code, label) ----
BILLABLE_ACTIVITY_CHOICES = [
    ("RPT", "Report writing / documentation"),
    ("TEL", "Telephonic case management"),
    ("MTG", "Meetings / conferences"),
    ("DOC", "Document review"),
    ("EMAIL", "Email correspondence"),
    ("TRV", "Travel time"),
    ("MIL", "Mileage (miles)"),
    ("EXP", "Expenses (dollars)"),
    ("NO BILL", "Non-billable activity"),
]

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


# ---- date parsing helper ----
def _parse_date(value: str):
    """\
    Parse a YYYY-MM-DD string (from an HTML date input) into a Python date.
    Returns None if the value is empty or invalid.
    """
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


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
    Base folder for all documents. If Settings.documents_root is set, use that.
    Otherwise default to instance/documents.
    """
    settings = Settings.query.first()
    if settings and settings.documents_root:
        root = Path(settings.documents_root)
    else:
        root = Path(current_app.instance_path) / "documents"

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

# ---------- basic pages ----------


@bp.route("/")
@bp.route("/claims")
def claims_list():
    claims = Claim.query.order_by(Claim.id.desc()).all()

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

    return render_template(
        "claims_list.html",
        active_page="claims",
        claims=claims,
        billing_summary=billing_summary,
    )


@bp.route("/billing")
def billing_list():
    invoices = Invoice.query.order_by(Invoice.id.desc()).all()
    return render_template(
        "billing_list.html",
        active_page="billing",
        invoices=invoices,
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
    Print-friendly view of a single invoice.
    Uses the same stored totals on the Invoice record so that
    what you see on screen and what you print always match.
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
    new_invoice_date = (request.form.get("invoice_date") or "").strip() or None

    allowed_statuses = ["Draft", "Sent", "Paid", "Void"]
    if new_status not in allowed_statuses:
        new_status = old_status or "Draft"

    invoice.status = new_status
    invoice.invoice_date = new_invoice_date

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

# ----------- Placeholder for new report creation ----------
@bp.route("/claims/<int:claim_id>/reports/new/<report_type>")
def report_new(claim_id, report_type):
    """Placeholder route for creating a new report.

    Currently just ensures the claim exists and redirects back to the
    claim detail page. This satisfies url_for('main.report_new', ...)
    in the templates so the page can render without error. Later we
    can expand this into a full-screen report creation workflow.
    """
    claim = Claim.query.get_or_404(claim_id)
    return redirect(url_for("main.claim_detail", claim_id=claim.id))

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

        def parse_date(value: str):
            if not value:
                return None
            try:
                return datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                return None

        dob = parse_date(dob_raw)
        doi = parse_date(doi_raw)

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

    # Default contact list based on the claim's current carrier
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

        # Parse dates from HTML date inputs
        dob = _parse_date(request.form.get("dob"))
        doi = _parse_date(request.form.get("doi"))

        carrier_id = (request.form.get("carrier_id") or "").strip() or None
        employer_id = (request.form.get("employer_id") or "").strip() or None

        # Optional carrier contact (adjuster)
        carrier_contact_id_raw = (request.form.get("carrier_contact_id") or "").strip()
        carrier_contact_id = None
        if carrier_contact_id_raw:
            try:
                carrier_contact_id = int(carrier_contact_id_raw)
            except ValueError:
                carrier_contact_id = None

        # If the user changed the carrier, refresh the contact list for that carrier
        effective_carrier_id = carrier_id or claim.carrier_id
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

    error = None

    if request.method == "POST":
        form_type = request.form.get("form_type")

        # ---- New billable item ----
        if form_type == "billable_new":
            activity_code = (request.form.get("activity_code") or "").strip()
            service_date_raw = (request.form.get("service_date") or "").strip() or None
            service_date_parsed = _parse_date(service_date_raw)
            description = (request.form.get("description") or "").strip() or None

            qty_raw = (request.form.get("quantity") or "").strip()
            quantity = float(qty_raw) if qty_raw else None

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
                filename_safe = secure_filename(file.filename)
                unique_prefix = uuid.uuid4().hex[:8]
                stored_name = f"{unique_prefix}_{filename_safe}"
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

        # ---- New report ----
        elif form_type == "report_new":
            report_type = (request.form.get("report_type") or "").strip()
            dos_start = (request.form.get("dos_start") or "").strip() or None
            dos_end = (request.form.get("dos_end") or "").strip() or None
            status_text = (request.form.get("status_text") or "").strip() or None
            work_status = (request.form.get("work_status") or "").strip() or None
            plan = (request.form.get("case_management_plan") or "").strip() or None
            next_report_due = (request.form.get("next_report_due") or "").strip() or None

            # Parse date fields to date objects
            dos_start_parsed = _parse_date(dos_start)
            dos_end_parsed = _parse_date(dos_end)
            next_report_due_parsed = _parse_date(next_report_due)

            if not report_type:
                error = "Report type is required."
            else:
                report = Report(
                    claim_id=claim.id,
                    report_type=report_type,
                    dos_start=dos_start_parsed,
                    dos_end=dos_end_parsed,
                    work_status=work_status,
                    case_management_plan=plan,
                    next_report_due=next_report_due_parsed,
                )
                db.session.add(report)
                db.session.commit()
                return redirect(url_for("main.claim_detail", claim_id=claim.id))

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
        billable_activity_choices=BILLABLE_ACTIVITY_CHOICES,
        error=error,
    )


@bp.route("/claims/<int:claim_id>/reports/<int:report_id>")
def report_detail(claim_id, report_id):
    """Read-only view / preview of a single report."""
    claim = Claim.query.get_or_404(claim_id)
    report = Report.query.filter_by(id=report_id, claim_id=claim.id).first_or_404()

    return render_template(
        "report_detail.html",
        active_page="claims",
        claim=claim,
        report=report,
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
            url_for("main.report_detail", claim_id=claim.id, report_id=report.id)
        )

    if not _allowed_file(file.filename):
        flash("File type not allowed.", "danger")
        return redirect(
            url_for("main.report_detail", claim_id=claim.id, report_id=report.id)
        )

    # Build a safe path under the report folder
    report_folder = _get_report_folder(report)
    filename_safe = secure_filename(file.filename)
    unique_prefix = uuid.uuid4().hex[:8]
    stored_name = f"{unique_prefix}_{filename_safe}"
    stored_path = report_folder / stored_name

    file.save(stored_path)

    doc = ReportDocument(
        report_id=report.id,
        doc_type=doc_type,
        description=description,
        original_filename=file.filename,
        stored_path=str(stored_path),
        document_date=document_date or date.today().isoformat(),
    )
    db.session.add(doc)
    db.session.commit()

    flash("Report document uploaded.", "success")
    return redirect(
        url_for("main.report_detail", claim_id=claim.id, report_id=report.id)
    )

@bp.route("/claims/<int:claim_id>/reports/<int:report_id>/edit", methods=["GET", "POST"])
def report_edit(claim_id, report_id):
    """
    Edit an existing report for a claim.

    For now this just manages the high-level metadata fields:
    - report_type
    - DOS start / end
    - current_status
    - work_status
    - case_management_plan
    - next_report_due
    """
    claim = Claim.query.get_or_404(claim_id)
    report = Report.query.filter_by(id=report_id, claim_id=claim.id).first_or_404()

    error = None

    if request.method == "POST":
        report_type = (request.form.get("report_type") or "").strip()
        dos_start = (request.form.get("dos_start") or "").strip() or None
        dos_end = (request.form.get("dos_end") or "").strip() or None
        work_status = (request.form.get("work_status") or "").strip() or None
        case_management_plan = (request.form.get("case_management_plan") or "").strip() or None
        next_report_due = (request.form.get("next_report_due") or "").strip() or None

        if not report_type:
            error = "Report type is required."
        else:
            report.report_type = report_type
            report.dos_start = dos_start
            report.dos_end = dos_end
            report.work_status = work_status
            report.case_management_plan = case_management_plan
            report.next_report_due = next_report_due

            # Save selected barriers as JSON list of IDs
            barrier_ids_raw = request.form.getlist("barriers")
            barrier_ids = []
            for b in barrier_ids_raw:
                try:
                    barrier_ids.append(int(b))
                except (TypeError, ValueError):
                    continue

            report.barriers_json = json.dumps(barrier_ids or [])

            db.session.commit()
            return redirect(url_for("main.claim_detail", claim_id=claim.id))

    # Load active barrier options and group by category for the edit form
    options = (
        BarrierOption.query
        .filter_by(is_active=True)
        .order_by(BarrierOption.category, BarrierOption.sort_order, BarrierOption.label)
        .all()
    )
    barriers_by_category = defaultdict(list)
    for opt in options:
        barriers_by_category[opt.category].append(opt)

    # Parse selected barriers from report.barriers_json
    selected_barrier_ids = set()
    if report.barriers_json:
        try:
            loaded = json.loads(report.barriers_json)
            if isinstance(loaded, list):
                selected_barrier_ids = {int(v) for v in loaded}
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
    )

@bp.route("/reports/documents/<int:report_document_id>/download")
def report_document_download(report_document_id):
    """Download a stored report document."""
    doc = ReportDocument.query.get_or_404(report_document_id)

    if not doc.stored_path or not os.path.exists(doc.stored_path):
        flash("File not found on disk.", "danger")
        # Gracefully go back to the report if we can
        if doc.report and doc.report.claim:
            return redirect(
                url_for(
                    "main.report_detail",
                    claim_id=doc.report.claim.id,
                    report_id=doc.report.id,
                )
            )
        # Fallback: home
        return redirect(url_for("main.index"))

    directory, filename = os.path.split(doc.stored_path)
    download_name = doc.original_filename or filename

    return send_from_directory(
        directory,
        filename,
        as_attachment=True,
        download_name=download_name,
    )

@bp.route("/claims/<int:claim_id>/reports/<int:report_id>/delete", methods=["GET", "POST"])
def report_delete(claim_id, report_id):
    claim = Claim.query.get_or_404(claim_id)
    report = Report.query.get_or_404(report_id)

    if request.method == "POST":
        db.session.delete(report)
        db.session.commit()
        flash("Report deleted successfully.", "success")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    # GET request – show confirmation page
    return render_template("report_delete_confirmation.html", claim=claim, report=report)

@bp.route("/claims/<int:claim_id>/billable/<int:item_id>/edit", methods=["GET", "POST"])
def billable_edit(claim_id, item_id):
    claim = Claim.query.get_or_404(claim_id)
    item = BillableItem.query.filter_by(id=item_id, claim_id=claim.id).first_or_404()

    error = None

    if request.method == "POST":
        activity_code = (request.form.get("activity_code") or "").strip()
        service_date_raw = (request.form.get("service_date") or "").strip() or None
        service_date_parsed = _parse_date(service_date_raw)
        description = (request.form.get("description") or "").strip() or None

        qty_raw = (request.form.get("quantity") or "").strip()
        quantity = float(qty_raw) if qty_raw else None

        if not activity_code:
            error = "Activity code is required."
        else:
            item.activity_code = activity_code
            item.date_of_service = service_date_parsed
            item.description = description
            item.quantity = quantity
            item.is_complete = _billable_is_complete(activity_code, service_date_parsed, quantity)
            db.session.commit()
            return redirect(url_for("main.claim_detail", claim_id=claim.id))

    return render_template(
        "billable_edit.html",
        active_page="claims",
        claim=claim,
        item=item,
        error=error,
        billable_activity_choices=BILLABLE_ACTIVITY_CHOICES,
    )


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
            db.session.add(employer)
            db.session.commit()
            return redirect(url_for("main.employers_list"))

    return render_template(
        "employer_new.html",
        active_page="employers",
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
            employer.name = name
            employer.address1 = address1
            employer.address2 = address2
            employer.city = city
            employer.state = state
            employer.postal_code = postal_code
            employer.phone = phone
            employer.fax = fax
            employer.email = email

            db.session.commit()
            return redirect(url_for("main.employers_list"))

    return render_template(
        "employer_edit.html",
        active_page="employers",
        employer=employer,
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

    return render_template(
        "carrier_detail.html",
        active_page="carriers",
        carrier=carrier,
        contacts=contacts,
        edit_contact=edit_contact,
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

    return render_template(
        "employer_detail.html",
        active_page="employers",
        employer=employer,
        contacts=contacts,
        edit_contact=edit_contact,
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

    return render_template(
        "provider_detail.html",
        active_page="providers",
        provider=provider,
        contacts=contacts,
        edit_contact=edit_contact,
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
        settings.business_name = (request.form.get("business_name") or "").strip() or None
        settings.address1 = (request.form.get("address1") or "").strip() or None
        settings.address2 = (request.form.get("address2") or "").strip() or None
        settings.city = (request.form.get("city") or "").strip() or None
        settings.state = (request.form.get("state") or "").strip() or None
        settings.postal_code = (request.form.get("postal_code") or "").strip() or None
        settings.phone = (request.form.get("phone") or "").strip() or None
        settings.email = (request.form.get("email") or "").strip() or None

        settings.hourly_rate = float(request.form.get("hourly_rate") or 0) or None
        settings.telephonic_rate = float(request.form.get("telephonic_rate") or 0) or None
        settings.mileage_rate = float(request.form.get("mileage_rate") or 0) or None

        settings.payment_terms_default = (request.form.get("payment_terms_default") or "").strip() or None

        settings.dormant_claim_days = int(request.form.get("dormant_claim_days") or 0) or None
        settings.target_min_hours_per_week = float(
            request.form.get("target_min_hours_per_week") or 0
        ) or None
        settings.target_max_hours_per_week = float(
            request.form.get("target_max_hours_per_week") or 0
        ) or None

        settings.accent_color = (request.form.get("accent_color") or "").strip() or None
        settings.report_footer_text = (request.form.get("report_footer_text") or "").strip() or None
        settings.invoice_footer_text = (request.form.get("invoice_footer_text") or "").strip() or None

        settings.documents_root = (request.form.get("documents_root") or "").strip() or None

        db.session.commit()
        return redirect(url_for("main.settings_view"))

    return render_template(
        "settings.html",
        active_page="settings",
        settings=settings,
        error=error,
    )


