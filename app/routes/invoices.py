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
    """Generate a human-friendly invoice number."""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


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
# Routes
# -----------------------------------------------------------------------------


@bp.route("/claims/<int:claim_id>/invoice/new", methods=["GET"])
def invoice_new_for_claim(claim_id: int):
    """Create a new invoice for a claim using all uninvoiced, complete billables."""

    claim = Claim.query.get_or_404(claim_id)

    items = (
        BillableItem.query
        .filter_by(claim_id=claim.id, invoice_id=None)
        .filter(BillableItem.is_complete.is_(True))
        .all()
    )

    if not items:
        flash("This claim has no complete billable items to invoice yet.", "warning")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    dated_items = [i for i in items if getattr(i, "date_of_service", None)]
    if dated_items:
        dos_start = min(i.date_of_service for i in dated_items)
        dos_end = max(i.date_of_service for i in dated_items)
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
    """Update invoice header fields (Draft-only)."""

    invoice = Invoice.query.get_or_404(invoice_id)

    if not _invoice_is_draft(invoice):
        return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))

    # Common fields used in the UI
    status = (request.form.get("status") or "").strip() or (invoice.status or "Draft")
    invoice_number = (request.form.get("invoice_number") or "").strip() or None
    invoice_date_raw = (request.form.get("invoice_date") or "").strip() or None

    invoice.status = status

    if invoice_number is not None and hasattr(invoice, "invoice_number"):
        invoice.invoice_number = invoice_number

    if hasattr(invoice, "invoice_date"):
        invoice.invoice_date = _parse_date_any(invoice_date_raw)

    # Allow editing DOS range if present on model
    if hasattr(invoice, "dos_start"):
        invoice.dos_start = _parse_date_any(request.form.get("dos_start"))
    if hasattr(invoice, "dos_end"):
        invoice.dos_end = _parse_date_any(request.form.get("dos_end"))

    _calculate_invoice_totals(invoice)
    db.session.commit()

    return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))


@bp.route("/billing/<int:invoice_id>/add-uninvoiced", methods=["POST"])
def invoice_add_uninvoiced(invoice_id: int):
    """Attach all complete, uninvoiced billables for this claim to this invoice (Draft-only)."""

    invoice = Invoice.query.get_or_404(invoice_id)

    if not _invoice_is_draft(invoice):
        return redirect(url_for("main.invoice_detail", invoice_id=invoice.id))

    claim = invoice.claim

    items = (
        BillableItem.query
        .filter_by(claim_id=claim.id, invoice_id=None)
        .filter(BillableItem.is_complete.is_(True))
        .all()
    )

    if not items:
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
