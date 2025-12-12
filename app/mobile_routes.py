from flask import Blueprint, render_template, request, redirect, url_for, flash
from .models import Claim, BillableItem
from . import db

def _get_billable_helpers():
    """Lazy import to avoid circular imports between routes and mobile_routes."""
    from .routes import _parse_date, BILLABLE_ACTIVITY_CHOICES
    return _parse_date, BILLABLE_ACTIVITY_CHOICES

mobile_bp = Blueprint("mobile", __name__, template_folder="templates/mobile")

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
    _parse_date, BILLABLE_ACTIVITY_CHOICES = _get_billable_helpers()

    if request.method == "POST":
        activity_code = (request.form.get("activity_code") or "").strip()
        description = (request.form.get("description") or "").strip() or None
        notes = (request.form.get("notes") or "").strip() or None

        qty_raw = (request.form.get("quantity") or "").strip()
        quantity = float(qty_raw) if qty_raw else None

        service_date_raw = (request.form.get("service_date") or "").strip() or None
        service_date = _parse_date(service_date_raw)

        if not activity_code:
            error = "Activity code is required."
        else:
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
            flash("Billable item added.", "success")
            return redirect(url_for("mobile.mobile_claims"))

    return render_template(
        "mobile_billables.html",
        claim=claim,
        billable_activity_choices=BILLABLE_ACTIVITY_CHOICES,
        error=error,
    )
