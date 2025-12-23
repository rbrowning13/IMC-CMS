

"""Claim-related routes.

This module was split out of the old monolithic routes.py.

Notes during transition:
- Some small helpers are duplicated here temporarily (date parsing, settings loader)
  until we consolidate them into app/routes/helpers.py.
- Claim-level PCP has been removed. PCP will live on Initial Reports only.
"""

from __future__ import annotations

from datetime import date, datetime

from flask import redirect, render_template, request, url_for, flash
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import (
    BillingActivityCode,
    Carrier,
    Claim,
    ClaimDocument,
    Contact,
    Employer,
    Invoice,
    Report,
    Settings,
    BillableItem,
)

from . import bp


# ---- helpers (temporary duplicates; will move to routes/helpers.py) ----

def _parse_date(value: str | None):
    """Parse UI date input.

    Accepts 'YYYY-MM-DD' (native date input) or 'MM/DD/YYYY' (text input).
    Returns datetime.date or None.
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


def _ensure_settings() -> Settings:
    """Return the singleton Settings row, creating it if needed."""

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


# ---- routes ----


@bp.route("/")
@bp.route("/claims")
def claims_list():
    claims = Claim.query.order_by(Claim.id.desc()).all()

    # Optional filters
    status_filter = (request.args.get("status") or "").strip().lower()
    billing_filter = (request.args.get("billing") or "").strip().lower()

    if status_filter not in ("active", "dormant"):
        status_filter = "all"
    if billing_filter not in ("none", "open", "closed"):
        billing_filter = "all"

    # Dormant status calculation
    dormant_info = {}
    dormant_threshold_days = _ensure_settings().dormant_claim_days or 0
    for c in claims:
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
            .order_by(Invoice.id.desc())
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
            delta = (date.today() - last_date).days
            is_dormant = dormant_threshold_days > 0 and delta >= dormant_threshold_days
        else:
            is_dormant = False

        dormant_info[c.id] = {
            "is_dormant": is_dormant,
            "last_activity": last_date,
        }

    # Billing summary per-claim
    billing_summary = {}
    if claims:
        claim_ids = [c.id for c in claims]
        invoices = Invoice.query.filter(Invoice.claim_id.in_(claim_ids)).all()

        for cid in claim_ids:
            billing_summary[cid] = {"total": 0, "open": 0, "closed": 0}

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

    # Apply filters
    filtered_claims = []
    for c in claims:
        info = dormant_info.get(c.id, {})
        is_dormant = bool(info.get("is_dormant", False))

        if status_filter == "active" and is_dormant:
            continue
        if status_filter == "dormant" and not is_dormant:
            continue

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


@bp.route("/claims/new", methods=["GET", "POST"])
def new_claim():
    carriers = Carrier.query.order_by(Carrier.name).all()
    employers = Employer.query.order_by(Employer.name).all()

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
        surgery_date_raw = (request.form.get("surgery_date") or "").strip()

        claim_state = (request.form.get("claim_state") or "").strip() or None

        injured_body_part = (request.form.get("injured_body_part") or "").strip() or None

        claimant_address1 = (request.form.get("claimant_address1") or "").strip() or None
        claimant_address2 = (request.form.get("claimant_address2") or "").strip() or None
        claimant_city = (request.form.get("claimant_city") or "").strip() or None
        claimant_state = (request.form.get("claimant_state") or "").strip() or None
        claimant_postal_code = (request.form.get("claimant_postal_code") or "").strip() or None
        claimant_phone = (request.form.get("claimant_phone") or "").strip() or None
        claimant_email = (request.form.get("claimant_email") or "").strip() or None

        carrier_id_raw = (request.form.get("carrier_id") or "").strip()
        employer_id_raw = (request.form.get("employer_id") or "").strip()
        carrier_contact_id_raw = (request.form.get("carrier_contact_id") or "").strip()

        dob = _parse_date(dob_raw)
        doi = _parse_date(doi_raw)
        surgery_date = _parse_date(surgery_date_raw)

        if not claimant_name or not claim_number:
            error = "Claimant name and claim number are required."
        else:
            existing = Claim.query.filter_by(claim_number=claim_number).first()
            if existing:
                error = "A claim with that claim number already exists."
            else:
                claim = Claim(
                    claimant_name=claimant_name,
                    claim_number=claim_number,
                    dob=dob,
                    doi=doi,
                    surgery_date=surgery_date,
                    injured_body_part=injured_body_part,
                    claim_state=claim_state,
                    is_telephonic=False,
                    claimant_address1=claimant_address1,
                    claimant_address2=claimant_address2,
                    claimant_city=claimant_city,
                    claimant_state=claimant_state,
                    claimant_postal_code=claimant_postal_code,
                    claimant_phone=claimant_phone,
                    claimant_email=claimant_email,
                )

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


@bp.route("/claims/<int:claim_id>/edit", methods=["GET", "POST"])
def claim_edit(claim_id: int):
    claim = Claim.query.get_or_404(claim_id)
    error = None

    carriers = Carrier.query.order_by(Carrier.name).all()
    employers = Employer.query.order_by(Employer.name).all()

    carrier_contacts = []
    if claim.carrier_id:
        carrier_contacts = (
            Contact.query.filter_by(carrier_id=claim.carrier_id)
            .order_by(Contact.name)
            .all()
        )

    if request.method == "POST":
        claimant_name = (request.form.get("claimant_name") or "").strip()
        claim_number = (request.form.get("claim_number") or "").strip()

        dob = _parse_date(request.form.get("dob"))
        doi = _parse_date(request.form.get("doi"))
        surgery_date = _parse_date(request.form.get("surgery_date"))

        claim_state = (request.form.get("claim_state") or "").strip() or None

        injured_body_part = (request.form.get("injured_body_part") or "").strip() or None

        claimant_address1 = (request.form.get("claimant_address1") or "").strip() or None
        claimant_address2 = (request.form.get("claimant_address2") or "").strip() or None
        claimant_city = (request.form.get("claimant_city") or "").strip() or None
        claimant_state = (request.form.get("claimant_state") or "").strip() or None
        claimant_postal_code = (request.form.get("claimant_postal_code") or "").strip() or None
        claimant_phone = (request.form.get("claimant_phone") or "").strip() or None
        claimant_email = (request.form.get("claimant_email") or "").strip() or None

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

        effective_carrier_id = carrier_id if carrier_id is not None else claim.carrier_id
        carrier_contacts = []
        if effective_carrier_id:
            carrier_contacts = (
                Contact.query.filter_by(carrier_id=effective_carrier_id)
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
            claim.surgery_date = surgery_date
            claim.injured_body_part = injured_body_part

            claim.claim_state = claim_state

            claim.claimant_address1 = claimant_address1
            claim.claimant_address2 = claimant_address2
            claim.claimant_city = claimant_city
            claim.claimant_state = claimant_state
            claim.claimant_postal_code = claimant_postal_code
            claim.claimant_phone = claimant_phone
            claim.claimant_email = claimant_email

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


@bp.route("/claims/<int:claim_id>", methods=["GET", "POST"])
def claim_detail(claim_id: int):
    claim = Claim.query.get_or_404(claim_id)
    settings = _ensure_settings()

    # Handle quick-add Billable Item form (POSTs back to this same page)
    if request.method == "POST":
        # Be tolerant of older/newer template field names
        dos_raw = (
            (request.form.get("date_of_service") or "")
            or (request.form.get("service_date") or "")
            or (request.form.get("date") or "")
        ).strip()
        activity_code = (
            (request.form.get("activity_code") or "")
            or (request.form.get("activity") or "")
            or (request.form.get("code") or "")
        ).strip()
        description = (
            (request.form.get("description") or "")
            or (request.form.get("short_desc") or "")
        ).strip() or None
        qty_raw = (
            (request.form.get("quantity") or "")
            or (request.form.get("qty") or "")
        ).strip()
        notes = (
            (request.form.get("notes") or "")
            or (request.form.get("note") or "")
        ).strip() or None

        if not activity_code:
            flash("Select an activity before adding a billable item.", "error")
            return redirect(url_for("main.claim_detail", claim_id=claim.id))

        dos = _parse_date(dos_raw)
        qty = None
        if qty_raw:
            try:
                qty = float(qty_raw)
            except ValueError:
                flash("Quantity must be a number.", "error")
                return redirect(url_for("main.claim_detail", claim_id=claim.id))

        item = BillableItem(claim_id=claim.id)

        # Set attributes defensively (model field names changed during migrations)
        if hasattr(item, "date_of_service"):
            setattr(item, "date_of_service", dos)
        elif hasattr(item, "service_date"):
            setattr(item, "service_date", dos)

        if hasattr(item, "activity_code"):
            setattr(item, "activity_code", activity_code)
        elif hasattr(item, "activity"):
            setattr(item, "activity", activity_code)

        if hasattr(item, "description"):
            setattr(item, "description", description)
        elif hasattr(item, "short_desc"):
            setattr(item, "short_desc", description)

        if hasattr(item, "quantity"):
            setattr(item, "quantity", qty)
        elif hasattr(item, "qty"):
            setattr(item, "qty", qty)

        if hasattr(item, "notes"):
            setattr(item, "notes", notes)

        db.session.add(item)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash("Could not save billable item. Check required fields and try again.", "error")
            return redirect(url_for("main.claim_detail", claim_id=claim.id))

        flash("Billable item added.", "success")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    billable_items = (
        BillableItem.query.filter_by(claim_id=claim.id)
        .order_by(
            BillableItem.date_of_service.desc().nullslast(),
            BillableItem.created_at.desc(),
        )
        .all()
    )

    reports = (
        Report.query.filter_by(claim_id=claim.id)
        .order_by(Report.created_at.desc())
        .all()
    )

    documents = (
        ClaimDocument.query.filter_by(claim_id=claim.id)
        .order_by(ClaimDocument.uploaded_at.desc())
        .all()
    )

    invoices = (
        Invoice.query.filter_by(claim_id=claim.id)
        .order_by(
            Invoice.invoice_date.desc().nullslast(),
            Invoice.id.desc(),
        )
        .all()
    )

    invoice_map = {inv.id: inv for inv in invoices}
    open_invoice_count = sum(
        1 for inv in invoices if (inv.status or "Draft") not in ("Paid", "Void")
    )

    # Build billable activity choices from the BillingActivityCode table.
    activity_rows = (
        BillingActivityCode.query.order_by(
            BillingActivityCode.sort_order,
            BillingActivityCode.code,
        ).all()
    )
    if activity_rows:
        billable_activity_choices = [(r.code, r.label) for r in activity_rows]
    else:
        billable_activity_choices = []

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
    )


@bp.route("/claims/<int:claim_id>/delete", methods=["GET", "POST"])
def claim_delete(claim_id: int):
    """Two-step delete for a claim: confirm on GET, actually delete on POST."""

    claim = Claim.query.get_or_404(claim_id)

    if request.method == "POST":
        try:
            # --- Delete children in a FK-safe order ---

            # 1) Reports: clear join-table rows that reference Report first.
            report_ids = [rid for (rid,) in db.session.query(Report.id).filter_by(claim_id=claim.id).all()]
            if report_ids:
                db.session.execute(
                    text("DELETE FROM report_approved_provider WHERE report_id = ANY(:report_ids)"),
                    {"report_ids": report_ids},
                )

            # 2) Claim-level children
            BillableItem.query.filter_by(claim_id=claim.id).delete(synchronize_session=False)
            ClaimDocument.query.filter_by(claim_id=claim.id).delete(synchronize_session=False)

            # 3) Report rows (after join-table cleanup)
            Report.query.filter_by(claim_id=claim.id).delete(synchronize_session=False)

            # 4) Invoices (claim-level)
            Invoice.query.filter_by(claim_id=claim.id).delete(synchronize_session=False)

            # 5) Finally the claim
            db.session.delete(claim)
            db.session.commit()

            flash("Claim deleted.", "success")
            return redirect(url_for("main.claims_list"))

        except IntegrityError as e:
            db.session.rollback()
            flash(
                "Could not delete claim because related records still exist (FK constraint). "
                "See server logs for details.",
                "error",
            )
            raise

    return render_template(
        "claim_delete_confirm.html",
        active_page="claims",
        claim=claim,
    )