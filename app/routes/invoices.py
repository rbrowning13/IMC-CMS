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

    return render_template(
        "invoice_detail.html",
        active_page="billing",
        invoice=invoice,
        claim=invoice.claim,
        items=items,
        settings=settings,
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

    return render_template(
        "invoice_print.html",
        active_page="billing",
        invoice=invoice,
        claim=invoice.claim,
        items=items,
        settings=settings,
    )


@bp.route("/billing/<int:invoice_id>/update", methods=["POST"])
def invoice_update(invoice_id: int):
    """Update invoice header fields (Draft-only).

    The UI has been moved around during the routes split/migration.
    To avoid "save does nothing" issues, accept a few common alternate
    form field names.
    """

    invoice = Invoice.query.get_or_404(invoice_id)

    if not _invoice_is_draft(invoice):
        flash("Only Draft invoices can be edited.", "warning")
        return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))

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

    # Invoice number
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

    # Invoice date
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

    # DOS range
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
