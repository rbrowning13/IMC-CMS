"""Billing / billables routes.

This module intentionally focuses on BillableItem CRUD and related helpers.
"""

from __future__ import annotations

from datetime import datetime

from flask import flash, redirect, render_template, request, url_for

from app import db
from app.models import BillableItem, BillingActivityCode, Claim, Invoice

from . import bp
try:
    # Prefer the shared canonical helpers when available.
    from .helpers import BILLABLE_ACTIVITY_CHOICES, _billable_is_complete  # type: ignore
except Exception:  # pragma: no cover
    # Fallbacks so the app can boot while helpers are being refactored.
    BILLABLE_ACTIVITY_CHOICES = [
        ("Admin", "Admin"),
        ("Email", "Email"),
        ("Exp", "Exp"),
        ("Fax", "Fax"),
        ("FR", "FR"),
        ("GDL", "GDL"),
        ("LTR", "LTR"),
        ("MR", "MR"),
        ("MTG", "MTG"),
        ("MIL", "MIL"),
        ("REP", "REP"),
        ("RR", "RR"),
        ("TC", "TC"),
        ("TCM", "TCM"),
        ("Text", "Text"),
        ("Travel", "Travel"),
        ("Wait", "Wait"),
        ("NO BILL", "NO BILL"),
    ]

    def _billable_is_complete(activity_code, service_date, quantity):
        """Best-effort completeness rules.

        Mirrors the intent of the legacy app:
        - "NO BILL": requires either a service date or a quantity.
        - All other codes: require both service date and quantity.
        """
        code = (activity_code or "").strip().upper()
        if code == "NO BILL":
            return bool(service_date) or (quantity is not None)
        return bool(service_date) and (quantity is not None)


@bp.route("/billing")
def billing_list():
    """List invoices (Billing page)."""

    invoices = Invoice.query.order_by(Invoice.id.desc()).all()

    return render_template(
        "billing_list.html",
        active_page="billing",
        invoices=invoices,
    )


@bp.route("/claims/<int:claim_id>/billable/<int:item_id>/edit", methods=["GET", "POST"])
def billable_edit(claim_id, item_id):
    """Edit a billable item for a claim."""

    claim = Claim.query.get_or_404(claim_id)
    item = BillableItem.query.filter_by(id=item_id, claim_id=claim.id).first_or_404()

    # Build billable activity choices from the BillingActivityCode table.
    db_codes = (
        BillingActivityCode.query.filter_by(is_active=True)
        .order_by(BillingActivityCode.sort_order, BillingActivityCode.code)
        .all()
    )
    if db_codes:
        billable_activity_choices = [(c.code, c.label or c.code) for c in db_codes]
    else:
        billable_activity_choices = BILLABLE_ACTIVITY_CHOICES

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

    error = None

    if request.method == "POST":
        raw_date_value = (
            (request.form.get("service_date") or "").strip()
            or (request.form.get("date") or "").strip()
            or (request.form.get("date_of_service") or "").strip()
        )
        service_date_parsed = _parse_billable_date(raw_date_value)

        activity_code = (request.form.get("activity_code") or "").strip()
        qty_raw = (request.form.get("quantity") or "").strip()
        quantity = float(qty_raw) if qty_raw else None

        raw_description = (request.form.get("description") or "").strip()
        description = raw_description if raw_description else None

        notes_raw = (request.form.get("notes") or "").strip()
        notes = notes_raw if notes_raw else None

        # If description is still empty, fall back to the human label for this activity code.
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
            is_complete = _billable_is_complete(activity_code, service_date_parsed, quantity)

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


@bp.route("/claims/<int:claim_id>/billable/<int:item_id>/delete", methods=["GET", "POST"])
def billable_delete(claim_id, item_id):
    """Delete a billable item from a claim.

    If the item is already invoiced, we block deletion to avoid data loss.
    """

    claim = Claim.query.get_or_404(claim_id)
    item = BillableItem.query.filter_by(id=item_id, claim_id=claim.id).first_or_404()

    # If your model uses a different relationship/field, adjust this check.
    if getattr(item, "invoice_id", None):
        flash("That billable is already linked to an invoice and cannot be deleted.", "warning")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    if request.method == "POST":
        db.session.delete(item)
        db.session.commit()
        flash("Billable deleted.", "success")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    return render_template(
        "confirm_delete.html",
        active_page="claims",
        title="Delete billable",
        message="Are you sure you want to delete this billable item?",
        cancel_url=url_for("main.claim_detail", claim_id=claim.id),
        confirm_url=url_for("main.billable_delete", claim_id=claim.id, item_id=item.id),
    )