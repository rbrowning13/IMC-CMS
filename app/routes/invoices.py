"""Invoice routes.

This module contains *all* invoice-related routes that were historically in the
legacy monolithic routes file.

Goals:
- Keep invoice behavior stable (Draft-only mutations, attach/detach billables)
- Avoid hidden dependencies on app/routes/helpers.py while that migration is in
  progress (we provide local helper fallbacks here)

Registered via app/routes/__init__.py by importing this module.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from flask import flash, redirect, render_template, request, url_for
from sqlalchemy import func

from .. import db
from ..models import BillableItem, Claim, Invoice, Report

# Payments are defined in some versions of this project; keep optional.
try:
    from ..models import Payment  # type: ignore
except Exception:  # pragma: no cover
    Payment = None  # type: ignore
from . import bp


# -----------------------------------------------------------------------------
# Helpers (local fallbacks)
# -----------------------------------------------------------------------------

try:
    # If/when these are migrated into app/routes/helpers.py, we will prefer them.
    from .helpers import _calculate_invoice_totals as calculate_invoice_totals  # type: ignore
except Exception:  # pragma: no cover
    calculate_invoice_totals = None


try:
    from .helpers import _generate_invoice_number as generate_invoice_number  # type: ignore
except Exception:  # pragma: no cover
    generate_invoice_number = None

try:
    # Centralized invoice totals + payment/balance math (preferred)
    from .helpers import compute_invoice_financials  # type: ignore
except Exception:  # pragma: no cover
    compute_invoice_financials = None


def _parse_date_any(value: str | None) -> Optional[date]:
    """Parse a date string in either YYYY-MM-DD or MM/DD/YYYY."""
    value = (value or "").strip()
    if not value:
        return None

    # ISO first
    if "-" in value:
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            pass

    # US format
    try:
        return datetime.strptime(value, "%m/%d/%Y").date()
    except ValueError:
        return None


def _fallback_generate_invoice_number() -> str:
    """Generate invoice numbers like INV-YY-### (per-year sequence).

    Examples:
      INV-25-001
      INV-25-002

    If existing invoice numbers don't match the pattern, we safely start at 001.
    """

    yy = datetime.now().strftime("%y")
    prefix = f"INV-{yy}-"

    # Pull the max invoice_number for the current year prefix.
    # This is string-based, so it assumes the numeric suffix is zero-padded.
    last = (
        db.session.query(func.max(Invoice.invoice_number))
        .filter(Invoice.invoice_number.like(f"{prefix}%"))
        .scalar()
    )

    next_n = 1
    if last:
        try:
            suffix = str(last).replace(prefix, "", 1)
            next_n = int(suffix) + 1
        except Exception:
            next_n = 1

    return f"{prefix}{next_n:03d}"


def _fallback_calculate_invoice_totals(invoice: Invoice) -> None:
    """Recalculate invoice totals in-place.

    The legacy app stored totals on the invoice record. This fallback attempts
    to sum the linked billable items in a defensive way.
    """

    items = getattr(invoice, "items", None) or getattr(invoice, "billable_items", None) or []

    subtotal = 0.0
    for item in items:
        amount = None
        # Try common explicit amount fields first
        for attr in ("amount", "line_total", "total", "extended", "subtotal"):
            if hasattr(item, attr):
                amount = getattr(item, attr)
                break

        # Otherwise compute quantity * rate if available
        if amount is None:
            qty = getattr(item, "quantity", None)
            rate = getattr(item, "rate", None)
            if qty is not None and rate is not None:
                try:
                    amount = float(qty) * float(rate)
                except Exception:
                    amount = 0.0
            else:
                amount = 0.0

        try:
            subtotal += float(amount or 0.0)
        except Exception:
            subtotal += 0.0

    # Persist on common invoice fields (model has varied during development)
    for field in ("subtotal", "total", "total_amount", "amount_total"):
        if hasattr(invoice, field):
            try:
                setattr(invoice, field, subtotal)
            except Exception:
                pass

    for field in ("balance_due", "amount_due"):
        if hasattr(invoice, field):
            try:
                setattr(invoice, field, subtotal)
            except Exception:
                pass


def _generate_invoice_number() -> str:
    if generate_invoice_number is not None:
        try:
            return generate_invoice_number()
        except Exception:
            pass
    return _fallback_generate_invoice_number()



def _calculate_invoice_totals(invoice: Invoice) -> None:
    if calculate_invoice_totals is not None:
        try:
            calculate_invoice_totals(invoice)
            return
        except Exception:
            pass
    _fallback_calculate_invoice_totals(invoice)



# -----------------------------------------------------------------------------
# Centralized invoice math (preferred)
# -----------------------------------------------------------------------------


def _get_invoice_payments(invoice: Invoice):
    """Return (payments_list, paid_total) defensively.

    We prefer an `invoice.payments` relationship if it exists. If not, we fall back
    to querying the Payment model (when present).
    """

    payments = []

    # Relationship-based (most common)
    rel = getattr(invoice, "payments", None)
    if rel is not None:
        try:
            payments = list(rel)
        except Exception:
            payments = []

    # Query-based fallback
    if not payments and Payment is not None:
        try:
            payments = (
                Payment.query
                .filter_by(invoice_id=invoice.id)
                .order_by(getattr(Payment, "payment_date", Payment.id).desc())
                .all()
            )
        except Exception:
            try:
                payments = Payment.query.filter_by(invoice_id=invoice.id).all()
            except Exception:
                payments = []

    paid_total = 0.0
    for p in payments or []:
        try:
            paid_total += float(getattr(p, "amount", 0.0) or 0.0)
        except Exception:
            pass

    return payments, paid_total


def _with_payment_math(fin: dict, invoice: Invoice):
    """Ensure fin contains paid_total + balance_due consistent with payments."""

    payments, paid_total = _get_invoice_payments(invoice)

    # prefer the computed invoice_total from fin, otherwise fall back to model field
    invoice_total = 0.0
    try:
        invoice_total = float(fin.get("invoice_total", 0.0) or 0.0)
    except Exception:
        invoice_total = 0.0

    if not invoice_total:
        try:
            invoice_total = float(getattr(invoice, "total_amount", 0.0) or 0.0)
        except Exception:
            invoice_total = 0.0

    balance_due = max(0.0, float(invoice_total) - float(paid_total))

    fin["payments"] = payments
    fin["paid_total"] = float(paid_total)
    fin["balance_due"] = float(balance_due)

    return fin


def _compute_invoice_financials(invoice: Invoice, settings, claim: Claim | None):
    """Compute a single canonical financials dict for invoice views.

    Rule: if app/routes/helpers.py provides compute_invoice_financials(), that result is the
    source of truth. We only fill in missing keys with local fallbacks.

    This keeps carrier-vs-default rate selection consistent across invoice detail/print/billing/payment.
    """

    fin: dict = {}

    # 1) Prefer the centralized helper (single source of truth)
    if compute_invoice_financials is not None:
        try:
            candidate = compute_invoice_financials(invoice=invoice, settings=settings, claim=claim)
            if isinstance(candidate, dict):
                fin.update(candidate)
        except Exception:
            pass

    # 2) Ensure we always have effective rates (carrier overrides > settings)
    rates = fin.get("rates") if isinstance(fin.get("rates"), dict) else None
    if not isinstance(rates, dict):
        rates = _get_effective_invoice_rates(settings, claim)
        fin["rates"] = rates

    # 3) Ensure we always have canonical totals computed from items + effective rates
    # (Only compute locally if the helper didn't already provide them.)
    if "invoice_total" not in fin or "total_hours" not in fin or "total_miles" not in fin or "total_expenses" not in fin:
        totals = _compute_totals_from_items(invoice, rates)
        fin.setdefault("invoice_total", totals["invoice_total"])
        fin.setdefault("total_hours", totals["total_hours"])
        fin.setdefault("total_miles", totals["total_miles"])
        fin.setdefault("total_expenses", totals["total_expenses"])

    # 4) Ensure paid_total + balance_due are always computed the same way.
    return _with_payment_math(fin, invoice)



def _invoice_is_draft(invoice: Invoice) -> bool:
    return (invoice.status or "Draft") in ("Draft",)


# -----------------------------------------------------------------------------
# BillableItem service date and completeness helpers
# -----------------------------------------------------------------------------

def _billable_service_date_attr():
    """Return the BillableItem attribute used for service date.

    Historically this project has used a few names (date_of_service, service_date,
    date). We pick the first one that exists on the model.
    """

    for name in ("date_of_service", "service_date", "date"):
        if hasattr(BillableItem, name):
            return getattr(BillableItem, name)
    return None


def _billable_complete_clause():
    """Return a SQLAlchemy filter clause for 'complete' items, if supported."""

    if hasattr(BillableItem, "is_complete"):
        return BillableItem.is_complete.is_(True)
    return None


# -----------------------------------------------------------------------------
# Helper: Get effective invoice rates (carrier overrides or default)
def _get_effective_invoice_rates(settings, claim: Claim | None):
    """Return effective billing rates for an invoice.

    Priority:
      1) Carrier-specific rate (if claim has a carrier AND that carrier field is set)
      2) Settings default rate

    Returns a dict with:
      hourly_rate, telephonic_rate, mileage_rate
      hourly_source, telephonic_source, mileage_source   ("Carrier" | "Default" | "Unknown")
    """

    carrier = getattr(claim, "carrier", None) if claim else None

    def _to_float(v):
        try:
            if v is None:
                return None
            # Treat empty strings as None
            if isinstance(v, str) and not v.strip():
                return None
            return float(v)
        except Exception:
            return None

    def _pick_rate(field_name: str):
        # Carrier override if present and non-zero
        c_val = _to_float(getattr(carrier, field_name, None)) if carrier is not None else None
        if c_val is not None and c_val != 0.0:
            return c_val, "Carrier"

        s_val = _to_float(getattr(settings, field_name, None)) if settings is not None else None
        if s_val is not None and s_val != 0.0:
            return s_val, "Default"

        # If zero is intentionally set, allow it (but call it Default/Carrier depending on where it came from)
        if carrier is not None and _to_float(getattr(carrier, field_name, None)) == 0.0:
            return 0.0, "Carrier"
        if settings is not None and _to_float(getattr(settings, field_name, None)) == 0.0:
            return 0.0, "Default"

        return 0.0, "Unknown"

    hourly_rate, hourly_source = _pick_rate("hourly_rate")
    telephonic_rate, telephonic_source = _pick_rate("telephonic_rate")
    mileage_rate, mileage_source = _pick_rate("mileage_rate")

    return {
        "hourly_rate": hourly_rate,
        "telephonic_rate": telephonic_rate,
        "mileage_rate": mileage_rate,
        "hourly_source": hourly_source,
        "telephonic_source": telephonic_source,
        "mileage_source": mileage_source,
    }


# -----------------------------------------------------------------------------
# Helper: Compute canonical invoice totals from items using effective rates
def _compute_totals_from_items(invoice: Invoice, rates: dict) -> dict:
    """Compute totals from invoice items using effective rates.

    We intentionally compute these totals for display/printing (single source of truth)
    rather than trusting persisted invoice totals, because rates can be updated and
    the UI expects the current effective rates (carrier overrides > settings).

    Assumptions (based on current app conventions):
      - MIL: quantity is miles, billed at mileage_rate
      - Exp: quantity is dollars (pass-through)
      - TC/TCM/Text/Phone/Fax/etc: treated as telephonic time when recognizable
      - NO BILL: excluded
      - Everything else with a quantity: treated as hourly time at hourly_rate

    If an item exposes an explicit amount/line_total/total/subtotal field, we
    prefer that as authoritative.
    """

    items = getattr(invoice, "items", None) or getattr(invoice, "billable_items", None) or []

    def _float(v, default=0.0):
        try:
            if v is None:
                return float(default)
            if isinstance(v, str) and not v.strip():
                return float(default)
            return float(v)
        except Exception:
            return float(default)

    hourly_rate = _float(rates.get("hourly_rate"), 0.0)
    telephonic_rate = _float(rates.get("telephonic_rate"), 0.0)
    mileage_rate = _float(rates.get("mileage_rate"), 0.0)

    total_hours = 0.0
    total_miles = 0.0
    total_expenses = 0.0
    invoice_total = 0.0

    telephonic_codes = {
        "TC", "TCM", "TEXT", "TEXTING", "PHONE", "CALL", "FAX", "EMAIL", "TELE", "TELEPHONIC",
    }

    for item in items or []:
        code = (getattr(item, "activity_code", None) or "").strip()
        code_u = code.upper()

        # Skip NO BILL items from totals
        if code_u == "NO BILL":
            continue

        qty = _float(getattr(item, "quantity", None), 0.0)

        # Prefer explicit stored amount if present
        explicit_amount = None
        for attr in ("amount", "line_total", "total", "extended", "subtotal"):
            if hasattr(item, attr):
                explicit_amount = getattr(item, attr)
                break
        if explicit_amount is not None:
            amt = _float(explicit_amount, 0.0)
            invoice_total += amt
            # Try to keep category totals coherent when amount is explicit
            if code_u == "MIL":
                total_miles += qty
            elif code_u == "EXP":
                total_expenses += amt
            else:
                total_hours += qty
            continue

        # Rate-driven computation
        if code_u == "MIL":
            total_miles += qty
            invoice_total += qty * mileage_rate
        elif code_u == "EXP":
            # Treat quantity as pass-through dollars
            total_expenses += qty
            invoice_total += qty
        elif code_u in telephonic_codes:
            total_hours += qty
            invoice_total += qty * telephonic_rate
        else:
            total_hours += qty
            invoice_total += qty * hourly_rate

    return {
        "invoice_total": float(invoice_total),
        "total_hours": float(total_hours),
        "total_miles": float(total_miles),
        "total_expenses": float(total_expenses),
    }


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------


@bp.route("/claims/<int:claim_id>/invoice/new", methods=["GET"])
def invoice_new_for_claim(claim_id: int):
    """Create a new invoice for a claim using all uninvoiced, complete billables."""

    claim = Claim.query.get_or_404(claim_id)

    q = BillableItem.query.filter_by(claim_id=claim.id, invoice_id=None)
    complete_clause = _billable_complete_clause()
    if complete_clause is not None:
        q = q.filter(complete_clause)

    items = q.all()

    if not items:
        flash("This claim has no complete billable items to invoice yet.", "warning")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    service_date = _billable_service_date_attr()
    dated_items = [i for i in items if (service_date is not None and getattr(i, service_date.key, None))]
    if dated_items:
        dos_start = min(getattr(i, service_date.key) for i in dated_items)
        dos_end = max(getattr(i, service_date.key) for i in dated_items)
    else:
        dos_start = None
        dos_end = None

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
    db.session.flush()  # ensures invoice.id

    for item in items:
        item.invoice_id = invoice.id

    _calculate_invoice_totals(invoice)
    db.session.commit()

    return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))


@bp.route("/claims/<int:claim_id>/reports/<int:report_id>/invoice/new", methods=["GET"])
def invoice_new_for_report(claim_id: int, report_id: int):
    """Create a new invoice from a report DOS window.

    Uses all *uninvoiced* + *complete* billable items for the claim where
    date_of_service is within [report.dos_start, report.dos_end].
    """

    claim = Claim.query.get_or_404(claim_id)
    report = Report.query.filter_by(id=report_id, claim_id=claim.id).first_or_404()

    if not report.dos_start or not report.dos_end:
        flash("This report does not have a complete date-of-service range.", "warning")
        return redirect(url_for("main.report_edit", claim_id=claim.id, report_id=report.id))

    service_date = _billable_service_date_attr()
    if service_date is None:
        flash(
            "Billable items are missing a service date field (expected date_of_service/service_date/date).",
            "warning",
        )
        return redirect(url_for("main.report_edit", claim_id=claim.id, report_id=report.id))

    q_base = BillableItem.query.filter_by(claim_id=claim.id, invoice_id=None)

    complete_clause = _billable_complete_clause()
    q = q_base
    if complete_clause is not None:
        q = q.filter(complete_clause)

    items = (
        q.filter(service_date >= report.dos_start)
        .filter(service_date <= report.dos_end)
        .all()
    )

    if not items:
        # Fallback: if items exist in-range but aren't flagged complete, don't hard-block.
        items_any = (
            q_base.filter(service_date >= report.dos_start)
            .filter(service_date <= report.dos_end)
            .all()
        )
        if items_any:
            flash(
                "Billable items exist in this report's DOS range, but none are flagged complete. Invoicing them anyway — we should fix completeness tracking next.",
                "warning",
            )
            items = items_any
        else:
            flash(
                "No billable items found in this report's date range to invoice.",
                "warning",
            )
            return redirect(url_for("main.report_edit", claim_id=claim.id, report_id=report.id))

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

    for item in items:
        item.invoice_id = invoice.id

    _calculate_invoice_totals(invoice)
    db.session.commit()

    return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))


@bp.route("/billing/<int:invoice_id>")
def invoice_detail(invoice_id: int):
    """Invoice detail screen."""

    invoice = Invoice.query.get_or_404(invoice_id)
    settings = None
    try:
        # settings is used widely in templates; safe if missing.
        from .helpers import _ensure_settings
        settings = _ensure_settings()
    except Exception:
        settings = None

    # Prefer relationship name used by the model
    items = getattr(invoice, "items", None) or getattr(invoice, "billable_items", None) or []

    claim = invoice.claim
    fin = _compute_invoice_financials(invoice, settings, claim)

    # Ensure templates always have a consistent payments list + paid/balance numbers
    payments = fin.get("payments", []) if isinstance(fin, dict) else []
    paid_total = fin.get("paid_total", 0.0) if isinstance(fin, dict) else 0.0
    balance_due = fin.get("balance_due", 0.0) if isinstance(fin, dict) else 0.0

    # Prefer rates from centralized financials (if present), otherwise fall back.
    rates = fin.get("rates") if isinstance(fin, dict) else None
    if not isinstance(rates, dict):
        rates = _get_effective_invoice_rates(settings, claim)
        if isinstance(fin, dict):
            fin["rates"] = rates

    return render_template(
        "invoice_detail.html",
        active_page="billing",
        invoice=invoice,
        claim=claim,
        items=items,
        settings=settings,
        # Centralized financials for consistent math across detail/print
        fin=fin,
        payments=payments,
        paid_total=paid_total,
        balance_due=balance_due,
        # Explicit computed totals (avoid templates falling back to invoice.total_amount)
        invoice_total=(fin.get("invoice_total", 0.0) if isinstance(fin, dict) else 0.0),
        total_due=(fin.get("balance_due", 0.0) if isinstance(fin, dict) else 0.0),
        amount_paid=(fin.get("paid_total", 0.0) if isinstance(fin, dict) else 0.0),
        # Rate context for the Totals "Rates used" section
        hourly_rate=rates["hourly_rate"],
        telephonic_rate=rates["telephonic_rate"],
        mileage_rate=rates["mileage_rate"],
        hourly_rate_source=rates["hourly_source"],
        telephonic_rate_source=rates["telephonic_source"],
        mileage_rate_source=rates["mileage_source"],
        effective_rates=rates,
    )


@bp.route("/billing/<int:invoice_id>/print")
def invoice_print(invoice_id: int):
    """Print-friendly invoice view."""

    invoice = Invoice.query.get_or_404(invoice_id)
    settings = None
    try:
        from .helpers import _ensure_settings
        settings = _ensure_settings()
    except Exception:
        settings = None

    items = getattr(invoice, "items", None) or getattr(invoice, "billable_items", None) or []

    fin = _compute_invoice_financials(invoice, settings, invoice.claim)
    payments = fin.get("payments", []) if isinstance(fin, dict) else []
    paid_total = fin.get("paid_total", 0.0) if isinstance(fin, dict) else 0.0
    balance_due = fin.get("balance_due", 0.0) if isinstance(fin, dict) else 0.0

    return render_template(
        "invoice_print.html",
        active_page="billing",
        invoice=invoice,
        claim=invoice.claim,
        items=items,
        settings=settings,
        fin=fin,
        payments=payments,
        paid_total=paid_total,
        balance_due=balance_due,
        # Explicit computed totals (avoid templates falling back to invoice.total_amount)
        invoice_total=(fin.get("invoice_total", 0.0) if isinstance(fin, dict) else 0.0),
        total_due=(fin.get("balance_due", 0.0) if isinstance(fin, dict) else 0.0),
        amount_paid=(fin.get("paid_total", 0.0) if isinstance(fin, dict) else 0.0),
    )


@bp.route("/billing/<int:invoice_id>/update", methods=["POST"])
def invoice_update(invoice_id: int):
    """Update invoice header fields (Draft-only).

    The UI has been moved around during the routes split/migration.
    To avoid "save does nothing" issues, accept a few common alternate
    form field names.
    """

    invoice = Invoice.query.get_or_404(invoice_id)
    was_draft = _invoice_is_draft(invoice)
    prior_status = (invoice.status or "Draft")

    def _first_nonempty(*keys: str) -> str | None:
        """Return the first non-empty form value for any of the keys."""
        for k in keys:
            v = request.form.get(k)
            if v is None:
                continue
            v = v.strip()
            if v:
                return v
        return None

    def _first_present(*keys: str) -> tuple[bool, str | None]:
        """Return (present, value) for the first key that exists in request.form.

        present=True means the form included that key (even if empty).
        This lets us avoid accidentally overwriting stored values when the template
        uses a different field name.
        """
        for k in keys:
            if k in request.form:
                return True, request.form.get(k)
        return False, None

    # Track whether we actually changed anything
    changed = False

    # Status
    status = _first_nonempty("status", "invoice_status")
    if status is not None:
        new_status = status
    else:
        new_status = (invoice.status or "Draft")

    if (invoice.status or "Draft") != new_status:
        invoice.status = new_status
        changed = True

    # Invoice number (Draft-only)
    if was_draft:
        invoice_number = _first_nonempty(
            "invoice_number",
            "invoice_no",
            "inv_number",
            "number",
        )
        if invoice_number is not None and hasattr(invoice, "invoice_number"):
            if (invoice.invoice_number or "") != invoice_number:
                invoice.invoice_number = invoice_number
                changed = True

    # Invoice date (always editable)
    present, invoice_date_raw = _first_present(
        "invoice_date",
        "invoiceDate",
        "date",
    )
    if present and hasattr(invoice, "invoice_date"):
        parsed = _parse_date_any(invoice_date_raw)
        if invoice.invoice_date != parsed:
            invoice.invoice_date = parsed
            changed = True

    # If marking Sent without a date, auto-set to today
    if hasattr(invoice, "invoice_date"):
        if new_status == "Sent" and invoice.invoice_date is None:
            # Only auto-fill when the form did not provide a usable date.
            if (not present) or (_parse_date_any(invoice_date_raw) is None and (invoice_date_raw or "").strip() == ""):
                invoice.invoice_date = date.today()
                changed = True

    # DOS range (Draft-only)
    if was_draft:
        present, dos_start_raw = _first_present("dos_start", "dosStart")
        if present and hasattr(invoice, "dos_start"):
            parsed = _parse_date_any(dos_start_raw)
            if invoice.dos_start != parsed:
                invoice.dos_start = parsed
                changed = True

        present, dos_end_raw = _first_present("dos_end", "dosEnd")
        if present and hasattr(invoice, "dos_end"):
            parsed = _parse_date_any(dos_end_raw)
            if invoice.dos_end != parsed:
                invoice.dos_end = parsed
                changed = True

    # Allow status transitions (e.g., Sent -> Draft) even after sending.
    if not was_draft and prior_status != (invoice.status or "Draft"):
        # Allow status transitions (e.g., Sent -> Draft) even after sending.
        pass

    # Recalc totals regardless (safe + keeps legacy behavior consistent)
    _calculate_invoice_totals(invoice)
    db.session.commit()

    if changed:
        flash("Invoice saved.", "success")
    else:
        flash("No changes detected to save.", "info")

    return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))


@bp.route("/billing/<int:invoice_id>/add-uninvoiced", methods=["POST"])
def invoice_add_uninvoiced(invoice_id: int):
    """Attach all complete, uninvoiced billables for this claim to this invoice (Draft-only)."""

    invoice = Invoice.query.get_or_404(invoice_id)

    if not _invoice_is_draft(invoice):
        return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))

    claim = invoice.claim

    q_base = BillableItem.query.filter_by(claim_id=claim.id, invoice_id=None)

    complete_clause = _billable_complete_clause()
    q = q_base
    if complete_clause is not None:
        q = q.filter(complete_clause)

    items = q.all()

    if not items:
        items_any = q_base.all()
        if items_any:
            flash(
                "No billables are flagged complete, but uninvoiced items exist. Attaching them anyway — we should fix completeness tracking next.",
                "warning",
            )
            items = items_any
        else:
            return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))

    for item in items:
        item.invoice_id = invoice.id

    _calculate_invoice_totals(invoice)
    db.session.commit()

    return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))


@bp.route("/billing/<int:invoice_id>/delete", methods=["POST"])
def invoice_delete(invoice_id: int):
    """Delete a Draft invoice and return its items to the claim."""

    invoice = Invoice.query.get_or_404(invoice_id)

    if not _invoice_is_draft(invoice):
        flash("Only Draft invoices can be deleted.", "warning")
        return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))

    claim_id = invoice.claim_id

    items = getattr(invoice, "items", None) or getattr(invoice, "billable_items", None) or []
    for item in items:
        try:
            item.invoice_id = None
        except Exception:
            pass

    db.session.delete(invoice)
    db.session.commit()

    return redirect(url_for("main.claim_detail", claim_id=claim_id))


@bp.route("/billing/<int:invoice_id>/items/<int:item_id>/remove", methods=["POST"])
def invoice_remove_item(invoice_id: int, item_id: int):
    """Remove a single billable item from a Draft invoice."""

    invoice = Invoice.query.get_or_404(invoice_id)

    if not _invoice_is_draft(invoice):
        return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))

    item = BillableItem.query.get_or_404(item_id)

    # Only allow removing items that belong to this invoice.
    if getattr(item, "invoice_id", None) != invoice.id:
        return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))

    item.invoice_id = None

    _calculate_invoice_totals(invoice)
    db.session.commit()

    return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))
