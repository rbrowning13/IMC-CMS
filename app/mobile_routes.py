from __future__ import annotations

from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for

from .extensions import db
from .models import BillingActivityCode, BillableItem, Claim

mobile_bp = Blueprint("mobile", __name__, template_folder="templates/mobile")


def _parse_mmddyyyy(raw: str | None):
    """Parse MM/DD/YYYY -> date or None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%m/%d/%Y").date()
    except ValueError:
        return None


def _billable_activity_choices():
    """Return list of (code, label) for active billing codes."""
    try:
        codes = (
            BillingActivityCode.query.filter_by(is_active=True)
            .order_by(BillingActivityCode.sort_order, BillingActivityCode.code)
            .all()
        )
        out = []
        for c in codes:
            code = (c.code or "").strip()
            label = (c.label or c.code or "").strip()
            if code:
                out.append((code, label))
        return out
    except Exception:
        # Fallback if table isn't available yet.
        return [
            ("Admin", "Admin"),
            ("Email", "Email"),
            ("Exp", "Expense"),
            ("Fax", "Fax"),
            ("FR", "File Review"),
            ("GDL", "Guidelines"),
            ("LTR", "Letter"),
            ("MR", "Medical Research"),
            ("MTG", "Meeting"),
            ("MIL", "Mileage"),
            ("REP", "Report"),
            ("RR", "Records Review"),
            ("TC", "Telephone Call"),
            ("TCM", "Telephonic CM"),
            ("Text", "Text"),
            ("Travel", "Travel Time"),
            ("Wait", "Wait time"),
            ("NO BILL", "NO BILL"),
        ]


@mobile_bp.route("/")
def mobile_home():
    """Mobile root just forwards to the claim selector."""
    return redirect(url_for("mobile.mobile_claims"))


@mobile_bp.route("/claims")
def mobile_claims():
    """List all claims for quick selection."""
    claims = Claim.query.order_by(Claim.id.desc()).all()
    return render_template("mobile_claim_select.html", claims=claims)


@mobile_bp.route("/claims/<int:claim_id>/billable/new", methods=["GET", "POST"])
def mobile_billable_new(claim_id):
    """Mobile-first billable item entry."""
    claim = Claim.query.get_or_404(claim_id)
    error = None

    BILLABLE_ACTIVITY_CHOICES = _billable_activity_choices()

    # Show recent billables for quick visual confirmation on mobile
    recent_items = (
        BillableItem.query.filter_by(claim_id=claim.id)
        .order_by(BillableItem.date_of_service.desc().nullslast(), BillableItem.id.desc())
        .limit(50)
        .all()
    )

    if request.method == "POST":
        activity_code = (request.form.get("activity_code") or "").strip()
        description = (request.form.get("description") or "").strip() or None
        notes = (request.form.get("notes") or "").strip() or None

        qty_raw = (request.form.get("quantity") or "").strip()
        quantity = None
        if qty_raw:
            try:
                quantity = float(qty_raw)
            except ValueError:
                error = "Quantity must be a number."

        service_date_raw = (request.form.get("service_date") or "").strip() or None
        service_date = _parse_mmddyyyy(service_date_raw)
        if service_date_raw and service_date is None and error is None:
            error = "Service date must be MM/DD/YYYY."

        if error is None and not activity_code:
            error = "Activity code is required."

        if error is None:
            item = BillableItem(
                claim_id=claim.id,
                activity_code=activity_code,
                description=description,
                notes=notes,
                quantity=quantity,
                date_of_service=service_date,
                is_complete=True,
            )
            db.session.add(item)
            db.session.commit()
            # Keep user on the same claim so they can enter many items quickly
            flash("Billable item added.", "success")
            return redirect(url_for("mobile.mobile_billable_new", claim_id=claim.id))

    return render_template(
        "mobile_billables.html",
        claim=claim,
        billable_activity_choices=BILLABLE_ACTIVITY_CHOICES,
        recent_items=recent_items,
        error=error,
    )
