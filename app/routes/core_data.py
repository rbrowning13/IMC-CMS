"""app.routes._legacy

LEGACY ROUTES RESTORE (Dec 2025)

This file is intentionally a SMALL, temporary compatibility layer.

Rules:
- Prefer adding/moving routes into proper modules: claims/reports/invoices/billing/
  documents/forms/settings/api/helpers.
- Only keep endpoints here that are required for navigation/back-compat.
- Do NOT add new features here.

Note: It is OK for this module to provide *aliases* for older endpoint names
(e.g., `reporting_view`) so older templates/nav links continue working.
"""

from __future__ import annotations

from datetime import date, datetime

from flask import flash, jsonify, redirect, render_template, request, url_for
from jinja2 import TemplateNotFound
from sqlalchemy import or_

from app import db
from app.models import Carrier, Claim, Contact, Employer, Invoice, Provider

from . import bp
from .helpers import (
    _ensure_settings,
    validate_email,
    validate_phone,
    validate_postal_code,
)


# -----------------------------------------------------------------------------
# Dashboard-ish pages that existed in legacy routes
# -----------------------------------------------------------------------------


@bp.route("/analysis")
def analysis_view():
    settings = _ensure_settings()
    return render_template(
        "analysis.html",
        active_page="analysis",
        settings=settings,
        avg_open_claim_age_days=0.0,
        oldest_open_claim_age_days=0.0,
        open_claims_count=0,
        closed_claims_count=0,
        total_claims_count=0,
        uninvoiced_billable_count=0,
        uninvoiced_total_amount=0.0,
    )


@bp.route("/reporting", endpoint="reporting_dashboard")
def reporting_dashboard():
    """Reporting dashboard for AR / aging / open invoices.

    Template: reporting_dashboard.html
    Supports optional drill-down filters:
      - ?carrier=<carrier name>
      - ?bucket=0-30|31-60|61-90|90+
    """

    settings = _ensure_settings()
    today = date.today()

    carrier_filter = (request.args.get("carrier") or "").strip() or None
    bucket_filter = (request.args.get("bucket") or "").strip() or None

    total_claims = Claim.query.count()
    total_invoices = Invoice.query.count()
    invoices = Invoice.query.all()

    aging_buckets: dict[str, float] = {
        "0-30": 0.0,
        "31-60": 0.0,
        "61-90": 0.0,
        "90+": 0.0,
    }
    ar_by_carrier: dict[str, float] = {}
    open_invoice_rows: list[dict] = []

    for inv in invoices:
        status = inv.status or "Draft"
        amount = float(inv.total_amount or 0.0)

        # Only treat non-Paid / non-Void invoices as open AR
        is_open = status not in ("Paid", "Void")
        if not is_open:
            continue

        # Determine effective invoice date for aging
        effective_date = None
        if getattr(inv, "invoice_date", None):
            effective_date = inv.invoice_date
        else:
            created_at = getattr(inv, "created_at", None)
            if isinstance(created_at, datetime):
                effective_date = created_at.date()
            elif isinstance(created_at, date):
                effective_date = created_at

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

            aging_buckets[bucket_label] += amount

        # Carrier for grouping
        if getattr(inv, "claim", None) and getattr(inv.claim, "carrier", None):
            carrier_name = inv.claim.carrier.name
        else:
            carrier_name = "Unassigned"

        ar_by_carrier[carrier_name] = ar_by_carrier.get(carrier_name, 0.0) + amount

        open_invoice_rows.append(
            {
                "invoice": inv,
                "claim": getattr(inv, "claim", None),
                "carrier_name": carrier_name,
                "age_days": age_days,
                "bucket": bucket_label,
                "amount": amount,
            }
        )

    # Apply drill-down filters
    if carrier_filter or bucket_filter:
        filtered_rows = []
        for row in open_invoice_rows:
            if carrier_filter and row.get("carrier_name") != carrier_filter:
                continue
            if bucket_filter and row.get("bucket") != bucket_filter:
                continue
            filtered_rows.append(row)
    else:
        filtered_rows = open_invoice_rows

    open_invoices_count = len(filtered_rows)
    total_open_amount = sum(float(row.get("amount") or 0.0) for row in filtered_rows)

    try:
        return render_template(
            "reporting_dashboard.html",
            active_page="reporting",
            settings=settings,
            today=today,
            total_claims=total_claims,
            total_invoices=total_invoices,
            open_invoices=open_invoices_count,
            open_invoice_rows=filtered_rows,
            total_open_amount=total_open_amount,
            aging_buckets=aging_buckets,
            ar_by_carrier=ar_by_carrier,
            carrier_filter=carrier_filter,
            bucket_filter=bucket_filter,
        )
    except TemplateNotFound:
        flash("Reporting dashboard template is missing (reporting_dashboard.html).", "warning")
        return redirect(url_for("main.claims_list"))


# Back-compat alias: older templates/nav referenced `main.reporting_view`.
# Provide an endpoint alias that points at the same handler.
if "reporting_view" not in bp.deferred_functions and "reporting_view" not in bp.view_functions:
    try:
        bp.add_url_rule("/reporting", endpoint="reporting_view", view_func=reporting_dashboard)
    except Exception:
        # If something already registered it (e.g., during reload), don't hard-fail.
        pass


# -----------------------------------------------------------------------------
# Carriers
# -----------------------------------------------------------------------------


@bp.route("/carriers")
def carriers_list():
    carriers = Carrier.query.order_by(Carrier.name.asc()).all()
    return render_template(
        "carriers_list.html", active_page="carriers", carriers=carriers
    )


@bp.route("/carriers/new", methods=["GET", "POST"])
def carrier_new():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Carrier name is required.", "danger")
            return render_template(
                "carrier_form.html",
                active_page="carriers",
                carrier=None,
                form=request.form,
            )

        carrier = Carrier(
            name=name,
            address1=(request.form.get("address1") or "").strip() or None,
            address2=(request.form.get("address2") or "").strip() or None,
            city=(request.form.get("city") or "").strip() or None,
            state=(request.form.get("state") or "").strip() or None,
            postal_code=(request.form.get("postal_code") or "").strip() or None,
            phone=(request.form.get("phone") or "").strip() or None,
            fax=(request.form.get("fax") or "").strip() or None,
            email=(request.form.get("email") or "").strip() or None,
        )

        if carrier.email and not validate_email(carrier.email):
            flash("Carrier email looks invalid.", "warning")
        if carrier.phone and not validate_phone(carrier.phone):
            flash("Carrier phone looks invalid.", "warning")
        if carrier.fax and not validate_phone(carrier.fax):
            flash("Carrier fax looks invalid.", "warning")
        if carrier.postal_code and not validate_postal_code(carrier.postal_code):
            flash("Carrier postal code looks invalid.", "warning")

        db.session.add(carrier)
        db.session.commit()
        flash("Carrier created.", "success")
        return redirect(url_for("main.carriers_list"))

    return render_template(
        "carrier_form.html", active_page="carriers", carrier=None, form=None
    )


@bp.route("/carriers/<int:carrier_id>")
def carrier_detail(carrier_id: int):
    carrier = Carrier.query.get_or_404(carrier_id)
    return render_template(
        "carrier_detail.html", active_page="carriers", carrier=carrier
    )


@bp.route("/carriers/<int:carrier_id>/edit", methods=["GET", "POST"])
def carrier_edit(carrier_id: int):
    carrier = Carrier.query.get_or_404(carrier_id)

    if request.method == "POST":
        carrier.name = (request.form.get("name") or "").strip() or carrier.name
        carrier.address1 = (request.form.get("address1") or "").strip() or None
        carrier.address2 = (request.form.get("address2") or "").strip() or None
        carrier.city = (request.form.get("city") or "").strip() or None
        carrier.state = (request.form.get("state") or "").strip() or None
        carrier.postal_code = (request.form.get("postal_code") or "").strip() or None
        carrier.phone = (request.form.get("phone") or "").strip() or None
        carrier.fax = (request.form.get("fax") or "").strip() or None
        carrier.email = (request.form.get("email") or "").strip() or None

        if carrier.email and not validate_email(carrier.email):
            flash("Carrier email looks invalid.", "warning")
        if carrier.phone and not validate_phone(carrier.phone):
            flash("Carrier phone looks invalid.", "warning")
        if carrier.fax and not validate_phone(carrier.fax):
            flash("Carrier fax looks invalid.", "warning")
        if carrier.postal_code and not validate_postal_code(carrier.postal_code):
            flash("Carrier postal code looks invalid.", "warning")

        db.session.commit()
        flash("Carrier updated.", "success")
        return redirect(url_for("main.carrier_detail", carrier_id=carrier.id))

    return render_template(
        "carrier_form.html",
        active_page="carriers",
        carrier=carrier,
        form=None,
    )


@bp.route("/carriers/<int:carrier_id>/delete", methods=["POST"])
def carrier_delete(carrier_id: int):
    carrier = Carrier.query.get_or_404(carrier_id)
    db.session.delete(carrier)
    db.session.commit()
    flash("Carrier deleted.", "success")
    return redirect(url_for("main.carriers_list"))


# -----------------------------------------------------------------------------
# Employers
# -----------------------------------------------------------------------------


@bp.route("/employers")
def employers_list():
    employers = Employer.query.order_by(Employer.name.asc()).all()
    carriers = Carrier.query.order_by(Carrier.name.asc()).all()
    return render_template(
        "employers_list.html",
        active_page="employers",
        employers=employers,
        carriers=carriers,
    )


@bp.route("/employers/new", methods=["GET", "POST"])
def employer_new():
    carriers = Carrier.query.order_by(Carrier.name.asc()).all()

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Employer name is required.", "danger")
            return render_template(
                "employer_form.html",
                active_page="employers",
                employer=None,
                carriers=carriers,
                form=request.form,
            )

        carrier_id = request.form.get("carrier_id")
        carrier_id_int = int(carrier_id) if (carrier_id and carrier_id.isdigit()) else None

        employer = Employer(
            name=name,
            carrier_id=carrier_id_int,
            address1=(request.form.get("address1") or "").strip() or None,
            address2=(request.form.get("address2") or "").strip() or None,
            city=(request.form.get("city") or "").strip() or None,
            state=(request.form.get("state") or "").strip() or None,
            postal_code=(request.form.get("postal_code") or "").strip() or None,
            phone=(request.form.get("phone") or "").strip() or None,
            fax=(request.form.get("fax") or "").strip() or None,
        )

        if employer.phone and not validate_phone(employer.phone):
            flash("Employer phone looks invalid.", "warning")
        if employer.fax and not validate_phone(employer.fax):
            flash("Employer fax looks invalid.", "warning")
        if employer.postal_code and not validate_postal_code(employer.postal_code):
            flash("Employer postal code looks invalid.", "warning")

        db.session.add(employer)
        db.session.commit()
        flash("Employer created.", "success")
        return redirect(url_for("main.employers_list"))

    return render_template(
        "employer_form.html",
        active_page="employers",
        employer=None,
        carriers=carriers,
        form=None,
    )


@bp.route("/employers/<int:employer_id>")
def employer_detail(employer_id: int):
    employer = Employer.query.get_or_404(employer_id)
    return render_template(
        "employer_detail.html", active_page="employers", employer=employer
    )


@bp.route("/employers/<int:employer_id>/edit", methods=["GET", "POST"])
def employer_edit(employer_id: int):
    employer = Employer.query.get_or_404(employer_id)
    carriers = Carrier.query.order_by(Carrier.name.asc()).all()

    if request.method == "POST":
        employer.name = (request.form.get("name") or "").strip() or employer.name

        carrier_id = request.form.get("carrier_id")
        employer.carrier_id = int(carrier_id) if (carrier_id and carrier_id.isdigit()) else None

        employer.address1 = (request.form.get("address1") or "").strip() or None
        employer.address2 = (request.form.get("address2") or "").strip() or None
        employer.city = (request.form.get("city") or "").strip() or None
        employer.state = (request.form.get("state") or "").strip() or None
        employer.postal_code = (request.form.get("postal_code") or "").strip() or None
        employer.phone = (request.form.get("phone") or "").strip() or None
        employer.fax = (request.form.get("fax") or "").strip() or None

        if employer.phone and not validate_phone(employer.phone):
            flash("Employer phone looks invalid.", "warning")
        if employer.fax and not validate_phone(employer.fax):
            flash("Employer fax looks invalid.", "warning")
        if employer.postal_code and not validate_postal_code(employer.postal_code):
            flash("Employer postal code looks invalid.", "warning")

        db.session.commit()
        flash("Employer updated.", "success")
        return redirect(url_for("main.employer_detail", employer_id=employer.id))

    return render_template(
        "employer_form.html",
        active_page="employers",
        employer=employer,
        carriers=carriers,
        form=None,
    )


@bp.route("/employers/<int:employer_id>/delete", methods=["POST"])
def employer_delete(employer_id: int):
    employer = Employer.query.get_or_404(employer_id)
    db.session.delete(employer)
    db.session.commit()
    flash("Employer deleted.", "success")
    return redirect(url_for("main.employers_list"))


# -----------------------------------------------------------------------------
# Providers
# -----------------------------------------------------------------------------


@bp.route("/providers")
def providers_list():
    providers = Provider.query.order_by(Provider.name.asc()).all()
    return render_template(
        "providers_list.html", active_page="providers", providers=providers
    )


@bp.route("/providers/new", methods=["GET", "POST"])
def provider_new():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Provider name is required.", "danger")
            return render_template(
                "provider_form.html",
                active_page="providers",
                provider=None,
                form=request.form,
            )

        provider = Provider(
            name=name,
            address1=(request.form.get("address1") or "").strip() or None,
            address2=(request.form.get("address2") or "").strip() or None,
            city=(request.form.get("city") or "").strip() or None,
            state=(request.form.get("state") or "").strip() or None,
            postal_code=(request.form.get("postal_code") or "").strip() or None,
            phone=(request.form.get("phone") or "").strip() or None,
            fax=(request.form.get("fax") or "").strip() or None,
            email=(request.form.get("email") or "").strip() or None,
            notes=(request.form.get("notes") or "").strip() or None,
        )

        if provider.email and not validate_email(provider.email):
            flash("Provider email looks invalid.", "warning")
        if provider.phone and not validate_phone(provider.phone):
            flash("Provider phone looks invalid.", "warning")
        if provider.fax and not validate_phone(provider.fax):
            flash("Provider fax looks invalid.", "warning")
        if provider.postal_code and not validate_postal_code(provider.postal_code):
            flash("Provider postal code looks invalid.", "warning")

        db.session.add(provider)
        db.session.commit()
        flash("Provider created.", "success")
        return redirect(url_for("main.providers_list"))

    return render_template(
        "provider_form.html", active_page="providers", provider=None, form=None
    )


@bp.route("/providers/<int:provider_id>")
def provider_detail(provider_id: int):
    provider = Provider.query.get_or_404(provider_id)
    return render_template(
        "provider_detail.html", active_page="providers", provider=provider
    )


@bp.route("/providers/<int:provider_id>/edit", methods=["GET", "POST"])
def provider_edit(provider_id: int):
    provider = Provider.query.get_or_404(provider_id)

    if request.method == "POST":
        provider.name = (request.form.get("name") or "").strip() or provider.name
        provider.address1 = (request.form.get("address1") or "").strip() or None
        provider.address2 = (request.form.get("address2") or "").strip() or None
        provider.city = (request.form.get("city") or "").strip() or None
        provider.state = (request.form.get("state") or "").strip() or None
        provider.postal_code = (request.form.get("postal_code") or "").strip() or None
        provider.phone = (request.form.get("phone") or "").strip() or None
        provider.fax = (request.form.get("fax") or "").strip() or None
        provider.email = (request.form.get("email") or "").strip() or None
        provider.notes = (request.form.get("notes") or "").strip() or None

        if provider.email and not validate_email(provider.email):
            flash("Provider email looks invalid.", "warning")
        if provider.phone and not validate_phone(provider.phone):
            flash("Provider phone looks invalid.", "warning")
        if provider.fax and not validate_phone(provider.fax):
            flash("Provider fax looks invalid.", "warning")
        if provider.postal_code and not validate_postal_code(provider.postal_code):
            flash("Provider postal code looks invalid.", "warning")

        db.session.commit()
        flash("Provider updated.", "success")
        return redirect(url_for("main.provider_detail", provider_id=provider.id))

    return render_template(
        "provider_form.html", active_page="providers", provider=provider, form=None
    )


@bp.route("/providers/<int:provider_id>/delete", methods=["POST"])
def provider_delete(provider_id: int):
    provider = Provider.query.get_or_404(provider_id)
    db.session.delete(provider)
    db.session.commit()
    flash("Provider deleted.", "success")
    return redirect(url_for("main.providers_list"))


# -----------------------------------------------------------------------------
# Contacts (polymorphic-ish, legacy behavior)
# -----------------------------------------------------------------------------


@bp.route("/contacts/new/<string:parent_type>/<int:parent_id>", methods=["POST"])
def contact_new(parent_type: str, parent_id: int):
    name = (request.form.get("name") or "").strip() or None
    title = (request.form.get("title") or "").strip() or None
    phone = (request.form.get("phone") or "").strip() or None
    fax = (request.form.get("fax") or "").strip() or None
    email = (request.form.get("email") or "").strip() or None
    notes = (request.form.get("notes") or "").strip() or None

    if email and not validate_email(email):
        flash("Contact email looks invalid.", "warning")
    if phone and not validate_phone(phone):
        flash("Contact phone looks invalid.", "warning")
    if fax and not validate_phone(fax):
        flash("Contact fax looks invalid.", "warning")

    contact = Contact(
        name=name,
        title=title,
        phone=phone,
        fax=fax,
        email=email,
        notes=notes,
        parent_type=parent_type,
        parent_id=parent_id,
    )

    db.session.add(contact)
    db.session.commit()
    flash("Contact added.", "success")

    # Best-effort redirect back to the parent detail page.
    if parent_type == "carrier":
        return redirect(url_for("main.carrier_detail", carrier_id=parent_id))
    if parent_type == "employer":
        return redirect(url_for("main.employer_detail", employer_id=parent_id))
    if parent_type == "provider":
        return redirect(url_for("main.provider_detail", provider_id=parent_id))

    return redirect(url_for("main.claims_list"))


@bp.route(
    "/contacts/<int:contact_id>/update/<string:parent_type>/<int:parent_id>",
    methods=["POST"],
)
def contact_update(contact_id: int, parent_type: str, parent_id: int):
    contact = Contact.query.get_or_404(contact_id)

    contact.name = (request.form.get("name") or "").strip() or None
    contact.title = (request.form.get("title") or "").strip() or None
    contact.phone = (request.form.get("phone") or "").strip() or None
    contact.fax = (request.form.get("fax") or "").strip() or None
    contact.email = (request.form.get("email") or "").strip() or None
    contact.notes = (request.form.get("notes") or "").strip() or None

    if contact.email and not validate_email(contact.email):
        flash("Contact email looks invalid.", "warning")
    if contact.phone and not validate_phone(contact.phone):
        flash("Contact phone looks invalid.", "warning")
    if contact.fax and not validate_phone(contact.fax):
        flash("Contact fax looks invalid.", "warning")

    db.session.commit()
    flash("Contact updated.", "success")

    if parent_type == "carrier":
        return redirect(url_for("main.carrier_detail", carrier_id=parent_id))
    if parent_type == "employer":
        return redirect(url_for("main.employer_detail", employer_id=parent_id))
    if parent_type == "provider":
        return redirect(url_for("main.provider_detail", provider_id=parent_id))

    return redirect(url_for("main.claims_list"))


@bp.route("/contacts/<int:contact_id>/delete", methods=["POST"])
def contact_delete(contact_id: int):
    contact = Contact.query.get_or_404(contact_id)
    parent_type = getattr(contact, "parent_type", None)
    parent_id = getattr(contact, "parent_id", None)

    db.session.delete(contact)
    db.session.commit()
    flash("Contact deleted.", "success")

    if parent_type == "carrier" and parent_id:
        return redirect(url_for("main.carrier_detail", carrier_id=parent_id))
    if parent_type == "employer" and parent_id:
        return redirect(url_for("main.employer_detail", employer_id=parent_id))
    if parent_type == "provider" and parent_id:
        return redirect(url_for("main.provider_detail", provider_id=parent_id))

    return redirect(url_for("main.claims_list"))


# -----------------------------------------------------------------------------
# API: contact search (used by UI autocomplete)
# -----------------------------------------------------------------------------


@bp.route("/api/contact-search", methods=["GET"])
def api_contact_search():
    """Legacy lightweight search endpoint.

    Query params:
      - q: free-text
      - parent_type (optional)

    Returns a small JSON list.
    """
    q = (request.args.get("q") or "").strip()
    parent_type = (request.args.get("parent_type") or "").strip() or None

    if not q:
        return jsonify([])

    like = f"%{q}%"
    query = Contact.query
    if parent_type:
        query = query.filter(Contact.parent_type == parent_type)

    results = (
        query.filter(
            or_(
                Contact.name.ilike(like),
                Contact.email.ilike(like),
                Contact.phone.ilike(like),
            )
        )
        .order_by(Contact.name.asc())
        .limit(25)
        .all()
    )

    payload = []
    for c in results:
        payload.append(
            {
                "id": c.id,
                "name": c.name,
                "title": getattr(c, "title", None),
                "email": c.email,
                "phone": c.phone,
                "fax": getattr(c, "fax", None),
                "parent_type": getattr(c, "parent_type", None),
                "parent_id": getattr(c, "parent_id", None),
            }
        )

    return jsonify(payload)


__all__: list[str] = []