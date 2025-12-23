"""app.routes.core_data

CORE DATA + LEGACY COMPAT (Dec 2025)

This module is intentionally a SMALL, temporary compatibility layer for:
- Core reference data CRUD (Carriers, Employers, Providers, Contacts)
- A couple legacy dashboard-ish pages (Reporting + Analysis)

Rules:
- Prefer adding/moving routes into proper modules: claims/reports/invoices/billing/
  documents/forms/settings/api/helpers.
- Keep this file limited to navigation/back-compat + core reference data.
- Do NOT add new features here.

Note: It is OK for this module to provide *aliases* for older endpoint names
(e.g., `reporting_view`) so older templates/nav links continue working.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import flash, jsonify, redirect, render_template, request, url_for
from jinja2 import TemplateNotFound
from sqlalchemy import or_

from app import db
from app.models import Carrier, Claim, Contact, ContactRole, Employer, Invoice, Provider

# Reports live in their own module now, but some templates still post “New Report”
# actions to legacy endpoints. We import Report defensively to support back-compat.
try:
    from app.models import Report
except Exception:  # pragma: no cover
    Report = None
# -----------------------------------------------------------------------------
# Internal helpers for cross-schema Contact linkage
# -----------------------------------------------------------------------------

def _contact_supports_polymorphic() -> bool:
    return hasattr(Contact, "parent_type") and hasattr(Contact, "parent_id")


def _contacts_for(parent_type: str, parent_id: int):
    """Return a query for contacts for the given parent.

    Supports either:
      - polymorphic-ish Contact.parent_type/parent_id
      - legacy explicit FK columns (carrier_id/employer_id/provider_id)
    """
    if _contact_supports_polymorphic():
        return Contact.query.filter_by(parent_type=parent_type, parent_id=parent_id)

    # Legacy schema fallback
    if parent_type == "carrier" and hasattr(Contact, "carrier_id"):
        return Contact.query.filter(Contact.carrier_id == parent_id)
    if parent_type == "employer" and hasattr(Contact, "employer_id"):
        return Contact.query.filter(Contact.employer_id == parent_id)
    if parent_type == "provider" and hasattr(Contact, "provider_id"):
        return Contact.query.filter(Contact.provider_id == parent_id)

    # Worst-case: no known linkage columns
    return Contact.query.filter(False)


def _assign_contact_parent(contact: Contact, parent_type: str, parent_id: int) -> None:
    """Assign parent linkage onto a Contact instance safely across schemas."""
    if _contact_supports_polymorphic():
        contact.parent_type = parent_type
        contact.parent_id = parent_id
        return

    # Legacy schema fallback
    if parent_type == "carrier" and hasattr(contact, "carrier_id"):
        contact.carrier_id = parent_id
    elif parent_type == "employer" and hasattr(contact, "employer_id"):
        contact.employer_id = parent_id
    elif parent_type == "provider" and hasattr(contact, "provider_id"):
        contact.provider_id = parent_id

from . import bp
from .helpers import (
    _ensure_settings,
    validate_email,
    validate_phone,
    validate_postal_code,
)



# -----------------------------------------------------------------------------
# Helper: object to form dict for GET edit routes
# -----------------------------------------------------------------------------

def _obj_to_form(obj, *, fields: list[str], defaults: dict | None = None) -> dict:
    """Build a template-friendly dict for form prefilling.

    Many templates in this app expect a `form` dict (typically `request.form`) and
    use `form.get('field')` to populate inputs. On GET edit routes we need to
    provide a dict populated from the existing model instance.
    """
    data: dict = {}
    for f in fields:
        val = getattr(obj, f, None)
        # Convert None to empty string so Jinja value attributes don't show 'None'
        data[f] = "" if val is None else str(val)
    if defaults:
        for k, v in defaults.items():
            if not data.get(k):
                data[k] = v
    return data


# -----------------------------------------------------------------------------
# Helper: normalize phone extension
# -----------------------------------------------------------------------------
def _clean_phone_ext(raw: str | None) -> str | None:
    """Normalize phone extension input.

    Store as a simple short string (digits/letters OK). We keep it separate from
    the phone field in DB.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Strip common prefixes users type.
    s = s.lstrip().lstrip("xX").strip()
    if s.lower().startswith("ext"):
        s = s[3:].strip()
    # Keep it reasonably small.
    return s[:20] or None


# -----------------------------------------------------------------------------
# Helper: validate common contact-ish fields
# -----------------------------------------------------------------------------
def _validate_contactish_fields(
    *,
    subject: str,
    email: str | None = None,
    phone: str | None = None,
    fax: str | None = None,
    postal_code: str | None = None,
) -> bool:
    """Validate common contact-ish fields.

    Returns True if all provided (non-empty) fields are valid.
    Flashes user-facing messages for any invalid fields.

    NOTE: Blank optional fields are allowed.
    """

    ok = True

    if email and not validate_email(email):
        flash(f"{subject} email looks invalid.", "warning")
        ok = False

    if phone and not validate_phone(phone):
        flash(f"{subject} phone looks invalid.", "warning")
        ok = False

    if fax and not validate_phone(fax):
        flash(f"{subject} fax looks invalid.", "warning")
        ok = False

    if postal_code and not validate_postal_code(postal_code):
        flash(f"{subject} postal code looks invalid.", "warning")
        ok = False

    return ok


# -----------------------------------------------------------------------------
# Helper: parse contact role selection from form data
# -----------------------------------------------------------------------------
def _parse_contact_role_from_form(form: dict | None = None) -> tuple[int | None, str | None]:
    """Return (role_id, role_label) from form data.

    Accepts:
      - numeric contact_role_id/role_id values
      - legacy label values posted via fields like role/contact_role

    If a label matches a ContactRole.label, we return its id.
    """

    src = form or request.form

    # tolerate several field names used across templates
    raw = (
        src.get("contact_role_id")
        or src.get("role_id")
        or src.get("contact_role")
        or src.get("role")
        or src.get("contact_role_label")
        or src.get("new_contact_role_id")
        or src.get("add_contact_role_id")
        or src.get("new_contact_role")
        or src.get("add_contact_role")
        or src.get("new_role")
        or src.get("add_role")
        or ""
    )
    raw = (raw or "").strip()
    if not raw:
        return None, None

    # numeric id
    if raw.isdigit():
        role_id = int(raw)
        role_label = None
        try:
            role = ContactRole.query.get(role_id)
            if role and getattr(role, "is_active", True):
                role_label = (getattr(role, "label", None) or "").strip() or None
        except Exception:
            role_label = None
        return role_id, role_label

    # otherwise assume it's a label
    role_label = raw
    role_id = None
    try:
        role = (
            ContactRole.query.filter(ContactRole.label.ilike(role_label))
            .order_by(ContactRole.id.asc())
            .first()
        )
        if role and getattr(role, "is_active", True):
            role_id = role.id
            role_label = (getattr(role, "label", None) or role_label).strip() or role_label
    except Exception:
        pass

    return role_id, role_label


def _contact_role_context(settings):
    """Build template-friendly contact role options.

    Supports both:
      - ContactRole table (preferred)
      - legacy Settings.contact_roles_json (fallback)

    Returns a dict with keys:
      - contact_roles: ORM rows (if available)
      - contact_role_options: list[dict] with {id,label}
      - roles: list[str] of labels (legacy-friendly)
    """

    # Contact role/title options
    # We support both the newer ContactRole table and the legacy Settings.contact_roles_json.
    contact_roles = []
    try:
        contact_roles = (
            ContactRole.query.filter(ContactRole.is_active.is_(True))
            .order_by(ContactRole.sort_order.asc(), ContactRole.label.asc())
            .all()
        )
    except Exception:
        contact_roles = []

    roles_from_settings: list[str] = []
    raw_roles = getattr(settings, "contact_roles_json", None)
    if raw_roles:
        try:
            # expected: JSON array of strings OR array of objects with {label/name}
            import json

            parsed = json.loads(raw_roles)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, str):
                        s = item.strip()
                        if s:
                            roles_from_settings.append(s)
                    elif isinstance(item, dict):
                        s = (item.get("label") or item.get("name") or "").strip()
                        if s:
                            roles_from_settings.append(s)
        except Exception:
            # tolerate non-JSON comma-separated legacy content
            roles_from_settings = [s.strip() for s in str(raw_roles).split(",") if s.strip()]

    # Provide multiple template-friendly shapes because older templates vary.
    # 1) contact_role_options: list of dicts {id,label}
    # 2) contact_roles: list of ORM rows (may have `.id` and `.label`)
    # 3) roles: list of strings (legacy)
    if contact_roles:
        contact_role_options = [
            {"id": cr.id, "label": getattr(cr, "label", "")}
            for cr in contact_roles
            if getattr(cr, "is_active", True)
        ]
        roles = [opt["label"] for opt in contact_role_options if opt.get("label")]
    else:
        contact_role_options = [{"id": None, "label": s} for s in roles_from_settings]
        roles = roles_from_settings

    return {
        "contact_roles": contact_roles,
        "contact_role_options": contact_role_options,
        "roles": roles,
    }


# -----------------------------------------------------------------------------
# Helper: normalize contact form field names across legacy templates
# -----------------------------------------------------------------------------

def _normalize_contact_form(form: dict | None) -> dict:
    """Normalize contact form keys across legacy templates.

    Some templates use `name/title/phone/...` while others use prefixed keys like
    `contact_name/contact_title/...`. When validation fails we re-render the same
    detail page and must preserve whatever the user typed.

    Returns a plain dict with BOTH key styles populated where possible.
    """

    if not form:
        return {}

    # Copy into a plain dict so Jinja rendering is predictable even if `form`
    # is an ImmutableMultiDict.
    data = dict(form)

    def _get(*keys: str) -> str:
        for k in keys:
            v = data.get(k)
            if v is None:
                continue
            # Werkzeug MultiDict values can sometimes be lists; normalize.
            if isinstance(v, list):
                v = v[0] if v else ""
            s = str(v)
            if s is not None:
                return s
        return ""

    # Canonical values (unprefixed)
    name = _get("name", "contact_name")
    title = _get("title", "contact_title")
    phone = _get("phone", "contact_phone")
    phone_ext = _get("phone_ext", "contact_phone_ext")
    fax = _get("fax", "contact_fax")
    email = _get("email", "contact_email")
    notes = _get("notes", "contact_notes")

    # Role fields can be posted as an id or label under several names.
    contact_role_id = _get("contact_role_id", "role_id")
    role_label = _get("contact_role", "role", "contact_role_label")

    # Populate both styles so whichever the template uses will see a value.
    data.setdefault("name", name)
    data.setdefault("contact_name", name)

    data.setdefault("title", title)
    data.setdefault("contact_title", title)

    data.setdefault("phone", phone)
    data.setdefault("contact_phone", phone)

    data.setdefault("phone_ext", phone_ext)
    data.setdefault("contact_phone_ext", phone_ext)

    data.setdefault("fax", fax)
    data.setdefault("contact_fax", fax)

    data.setdefault("email", email)
    data.setdefault("contact_email", email)

    data.setdefault("notes", notes)
    data.setdefault("contact_notes", notes)

    # Keep role fields in sync
    if contact_role_id:
        data.setdefault("contact_role_id", contact_role_id)
        data.setdefault("role_id", contact_role_id)
    if role_label:
        data.setdefault("role", role_label)
        data.setdefault("contact_role", role_label)
        data.setdefault("contact_role_label", role_label)

    # Additional legacy/new-contact key variants
    # Some templates use different prefixes for the "Add Contact" form.
    data.setdefault("new_name", name)
    data.setdefault("new_title", title)
    data.setdefault("new_phone", phone)
    data.setdefault("new_phone_ext", phone_ext)
    data.setdefault("new_fax", fax)
    data.setdefault("new_email", email)
    data.setdefault("new_notes", notes)

    data.setdefault("add_name", name)
    data.setdefault("add_title", title)
    data.setdefault("add_phone", phone)
    data.setdefault("add_phone_ext", phone_ext)
    data.setdefault("add_fax", fax)
    data.setdefault("add_email", email)
    data.setdefault("add_notes", notes)

    # Role variants
    if contact_role_id:
        data.setdefault("new_contact_role_id", contact_role_id)
        data.setdefault("add_contact_role_id", contact_role_id)
    if role_label:
        data.setdefault("new_role", role_label)
        data.setdefault("add_role", role_label)
        data.setdefault("new_contact_role", role_label)
        data.setdefault("add_contact_role", role_label)

    # Extra robustness: some templates use `new_contact_*` / `add_contact_*` field names
    # for the inline "Add Contact" form on detail pages.
    data.setdefault("new_contact_name", name)
    data.setdefault("new_contact_title", title)
    data.setdefault("new_contact_phone", phone)
    data.setdefault("new_contact_phone_ext", phone_ext)
    data.setdefault("new_contact_fax", fax)
    data.setdefault("new_contact_email", email)
    data.setdefault("new_contact_notes", notes)

    data.setdefault("add_contact_name", name)
    data.setdefault("add_contact_title", title)
    data.setdefault("add_contact_phone", phone)
    data.setdefault("add_contact_phone_ext", phone_ext)
    data.setdefault("add_contact_fax", fax)
    data.setdefault("add_contact_email", email)
    data.setdefault("add_contact_notes", notes)

    if contact_role_id:
        data.setdefault("new_contact_contact_role_id", contact_role_id)
        data.setdefault("add_contact_contact_role_id", contact_role_id)
    if role_label:
        data.setdefault("new_contact_role", role_label)
        data.setdefault("add_contact_role", role_label)

    return data

# -----------------------------------------------------------------------------
# Helper: re-render parent detail page with contact form (on validation failure)
# -----------------------------------------------------------------------------
def _render_parent_detail_with_contact_form(
    *,
    parent_type: str,
    parent_id: int,
    edit_contact: Contact | None = None,
    form: dict | None = None,
):
    """Re-render a parent detail page preserving contact form inputs.

    This is used when contact validation fails. Redirecting loses POST body, so
    we render the same detail template and pass `form` (typically `request.form`).

    Templates in this project commonly populate inputs from `form.get(...)`.
    """

    normalized_form = _normalize_contact_form(form)

    settings = _ensure_settings()
    role_ctx = _contact_role_context(settings)

    if parent_type == "carrier":
        with db.session.no_autoflush:
            carrier = Carrier.query.get_or_404(parent_id)
            contacts = _contacts_for("carrier", parent_id).order_by(Contact.name.asc()).all()
        return render_template(
            "carrier_detail.html",
            active_page="carriers",
            settings=settings,
            carrier=carrier,
            contacts=contacts,
            edit_contact=edit_contact,
            form=normalized_form,
            contact_form=normalized_form,
            **role_ctx,
        )

    if parent_type == "employer":
        with db.session.no_autoflush:
            employer = Employer.query.get_or_404(parent_id)
            contacts = _contacts_for("employer", parent_id).order_by(Contact.name.asc()).all()
        return render_template(
            "employer_detail.html",
            active_page="employers",
            settings=settings,
            employer=employer,
            contacts=contacts,
            edit_contact=edit_contact,
            form=normalized_form,
            contact_form=normalized_form,
            **role_ctx,
        )

    if parent_type == "provider":
        with db.session.no_autoflush:
            provider = Provider.query.get_or_404(parent_id)
            contacts = _contacts_for("provider", parent_id).order_by(Contact.name.asc()).all()
        return render_template(
            "provider_detail.html",
            active_page="providers",
            settings=settings,
            provider=provider,
            contacts=contacts,
            edit_contact=edit_contact,
            form=normalized_form,
            contact_form=normalized_form,
            **role_ctx,
        )

    # Unknown parent; fall back safely.
    flash("Unable to validate contact: unknown parent type.", "warning")
    return redirect(url_for("main.claims_list"))


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
# Register an additional endpoint pointing at the same handler.
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
                "carrier_new.html",
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
        if hasattr(carrier, "phone_ext"):
            carrier.phone_ext = _clean_phone_ext(request.form.get("phone_ext"))

        is_valid = _validate_contactish_fields(
            subject="Carrier",
            email=carrier.email,
            phone=carrier.phone,
            fax=carrier.fax,
            postal_code=carrier.postal_code,
        )

        if not is_valid:
            # Stay on the form and preserve inputs; do NOT create a record.
            return render_template(
                "carrier_new.html",
                active_page="carriers",
                carrier=None,
                form=request.form,
            )

        db.session.add(carrier)
        db.session.commit()
        flash("Carrier created.", "success")
        return redirect(url_for("main.carriers_list"))

    return render_template(
        "carrier_new.html",
        active_page="carriers",
        carrier=None,
        form={"state": "ID"},
    )


@bp.route("/carriers/<int:carrier_id>")
def carrier_detail(carrier_id: int):
    settings = _ensure_settings()
    carrier = Carrier.query.get_or_404(carrier_id)
    # Begin: edit_contact logic
    edit_contact_id = request.args.get("edit_contact_id")
    edit_contact = None
    if edit_contact_id and edit_contact_id.isdigit():
        edit_contact = Contact.query.get(int(edit_contact_id))
        if edit_contact and (
            (hasattr(edit_contact, "parent_type") and edit_contact.parent_type == "carrier" and edit_contact.parent_id == carrier_id)
            or (hasattr(edit_contact, "carrier_id") and edit_contact.carrier_id == carrier_id)
        ):
            pass
        else:
            edit_contact = None
    # End: edit_contact logic
    contacts = (
        _contacts_for("carrier", carrier_id)
        .order_by(Contact.name.asc())
        .all()
    )

    role_ctx = _contact_role_context(settings)

    return render_template(
        "carrier_detail.html",
        active_page="carriers",
        settings=settings,
        carrier=carrier,
        contacts=contacts,
        edit_contact=edit_contact,
        **role_ctx,
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
        if hasattr(carrier, "phone_ext"):
            carrier.phone_ext = _clean_phone_ext(request.form.get("phone_ext"))
        carrier.fax = (request.form.get("fax") or "").strip() or None
        carrier.email = (request.form.get("email") or "").strip() or None

        is_valid = _validate_contactish_fields(
            subject="Carrier",
            email=carrier.email,
            phone=carrier.phone,
            fax=carrier.fax,
            postal_code=carrier.postal_code,
        )

        if not is_valid:
            # Do NOT commit invalid data; re-render edit with user inputs preserved.
            return render_template(
                "carrier_edit.html",
                active_page="carriers",
                carrier=carrier,
                form=request.form,
            )

        db.session.commit()
        flash("Carrier updated.", "success")
        return redirect(url_for("main.carrier_detail", carrier_id=carrier.id))

    form = _obj_to_form(
        carrier,
        fields=[
            "name",
            "address1",
            "address2",
            "city",
            "state",
            "postal_code",
            "phone",
            "phone_ext",
            "fax",
            "email",
        ],
        defaults={"state": "ID"},
    )
    return render_template(
        "carrier_edit.html",
        active_page="carriers",
        carrier=carrier,
        form=form,
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
                "employer_new.html",
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
        if hasattr(employer, "phone_ext"):
            employer.phone_ext = _clean_phone_ext(request.form.get("phone_ext"))

        is_valid = _validate_contactish_fields(
            subject="Employer",
            phone=employer.phone,
            fax=employer.fax,
            postal_code=employer.postal_code,
        )

        if not is_valid:
            return render_template(
                "employer_new.html",
                active_page="employers",
                employer=None,
                carriers=carriers,
                form=request.form,
            )

        db.session.add(employer)
        db.session.commit()
        flash("Employer created.", "success")
        return redirect(url_for("main.employers_list"))

    return render_template(
        "employer_new.html",
        active_page="employers",
        employer=None,
        carriers=carriers,
        form={"state": "ID"},
    )


@bp.route("/employers/<int:employer_id>")
def employer_detail(employer_id: int):
    settings = _ensure_settings()
    employer = Employer.query.get_or_404(employer_id)
    # Begin: edit_contact logic
    edit_contact_id = request.args.get("edit_contact_id")
    edit_contact = None
    if edit_contact_id and edit_contact_id.isdigit():
        edit_contact = Contact.query.get(int(edit_contact_id))
        if edit_contact and (
            (hasattr(edit_contact, "parent_type") and edit_contact.parent_type == "employer" and edit_contact.parent_id == employer_id)
            or (hasattr(edit_contact, "employer_id") and edit_contact.employer_id == employer_id)
        ):
            pass
        else:
            edit_contact = None
    # End: edit_contact logic
    contacts = (
        _contacts_for("employer", employer_id)
        .order_by(Contact.name.asc())
        .all()
    )

    role_ctx = _contact_role_context(settings)

    return render_template(
        "employer_detail.html",
        active_page="employers",
        settings=settings,
        employer=employer,
        contacts=contacts,
        edit_contact=edit_contact,
        **role_ctx,
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
        if hasattr(employer, "phone_ext"):
            employer.phone_ext = _clean_phone_ext(request.form.get("phone_ext"))
        employer.fax = (request.form.get("fax") or "").strip() or None

        is_valid = _validate_contactish_fields(
            subject="Employer",
            phone=employer.phone,
            fax=employer.fax,
            postal_code=employer.postal_code,
        )

        if not is_valid:
            return render_template(
                "employer_edit.html",
                active_page="employers",
                employer=employer,
                carriers=carriers,
                form=request.form,
            )

        db.session.commit()
        flash("Employer updated.", "success")
        return redirect(url_for("main.employer_detail", employer_id=employer.id))

    form = _obj_to_form(
        employer,
        fields=[
            "name",
            "address1",
            "address2",
            "city",
            "state",
            "postal_code",
            "phone",
            "phone_ext",
            "fax",
        ],
        defaults={"state": "ID"},
    )
    # carrier_id is a FK; ensure we expose it for the dropdown if the template uses form.get('carrier_id')
    form["carrier_id"] = "" if getattr(employer, "carrier_id", None) is None else str(employer.carrier_id)

    return render_template(
        "employer_edit.html",
        active_page="employers",
        employer=employer,
        carriers=carriers,
        form=form,
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
                "provider_new.html",
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
        )
        if hasattr(provider, "phone_ext"):
            provider.phone_ext = _clean_phone_ext(request.form.get("phone_ext"))

        # Notes is optional / may not exist in older schemas.
        # Some templates historically used `note` instead of `notes`.
        if hasattr(provider, "notes"):
            raw_notes = (request.form.get("notes") or request.form.get("note") or "").strip()
            provider.notes = raw_notes or None

        is_valid = _validate_contactish_fields(
            subject="Provider",
            email=provider.email,
            phone=provider.phone,
            fax=provider.fax,
            postal_code=provider.postal_code,
        )

        if not is_valid:
            return render_template(
                "provider_new.html",
                active_page="providers",
                provider=None,
                form=request.form,
            )

        db.session.add(provider)
        db.session.commit()
        flash("Provider created.", "success")
        return redirect(url_for("main.providers_list"))

    return render_template(
        "provider_new.html",
        active_page="providers",
        provider=None,
        form={"state": "ID"},
    )


@bp.route("/providers/<int:provider_id>")
def provider_detail(provider_id: int):
    settings = _ensure_settings()
    provider = Provider.query.get_or_404(provider_id)
    # Begin: edit_contact logic
    edit_contact_id = request.args.get("edit_contact_id")
    edit_contact = None
    if edit_contact_id and edit_contact_id.isdigit():
        edit_contact = Contact.query.get(int(edit_contact_id))
        if edit_contact and (
            (hasattr(edit_contact, "parent_type") and edit_contact.parent_type == "provider" and edit_contact.parent_id == provider_id)
            or (hasattr(edit_contact, "provider_id") and edit_contact.provider_id == provider_id)
        ):
            pass
        else:
            edit_contact = None
    # End: edit_contact logic
    contacts = (
        _contacts_for("provider", provider_id)
        .order_by(Contact.name.asc())
        .all()
    )

    role_ctx = _contact_role_context(settings)

    return render_template(
        "provider_detail.html",
        active_page="providers",
        settings=settings,
        provider=provider,
        contacts=contacts,
        edit_contact=edit_contact,
        **role_ctx,
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
        if hasattr(provider, "phone_ext"):
            provider.phone_ext = _clean_phone_ext(request.form.get("phone_ext"))
        provider.fax = (request.form.get("fax") or "").strip() or None
        provider.email = (request.form.get("email") or "").strip() or None
        # Notes is optional / may not exist in older schemas.
        # Some templates historically used `note` instead of `notes`.
        if hasattr(provider, "notes"):
            raw_notes = (request.form.get("notes") or request.form.get("note") or "").strip()
            provider.notes = raw_notes or None

        is_valid = _validate_contactish_fields(
            subject="Provider",
            email=provider.email,
            phone=provider.phone,
            fax=provider.fax,
            postal_code=provider.postal_code,
        )

        if not is_valid:
            return render_template(
                "provider_edit.html",
                active_page="providers",
                provider=provider,
                form=request.form,
            )

        db.session.commit()
        flash("Provider updated.", "success")
        return redirect(url_for("main.provider_detail", provider_id=provider.id))

    form = _obj_to_form(
        provider,
        fields=[
            "name",
            "address1",
            "address2",
            "city",
            "state",
            "postal_code",
            "phone",
            "phone_ext",
            "fax",
            "email",
        ],
        defaults={"state": "ID"},
    )
    if hasattr(provider, "notes"):
        val = getattr(provider, "notes", None)
        form["notes"] = "" if val is None else str(val)
        # Back-compat for templates that look for `note`
        form["note"] = form["notes"]

    return render_template(
        "provider_edit.html",
        active_page="providers",
        provider=provider,
        form=form,
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
    # Preserve *all* posted keys, but also populate normalized aliases so templates
    # with legacy/new naming conventions can repopulate fields after validation errors.
    incoming_form = dict(request.form)
    incoming_form = _normalize_contact_form(incoming_form)

    name = (incoming_form.get("name") or "").strip() or None
    title = (incoming_form.get("title") or "").strip() or None

    # Parse role from form (supports id or label)
    role_id, role_label = _parse_contact_role_from_form(incoming_form)

    # If template doesn't post a free-text title, mirror the selected role label into title.
    if not title and role_label:
        title = role_label

    phone = (incoming_form.get("phone") or "").strip() or None
    phone_ext = _clean_phone_ext(incoming_form.get("phone_ext"))
    fax = (incoming_form.get("fax") or "").strip() or None
    email = (incoming_form.get("email") or "").strip() or None
    notes = (incoming_form.get("notes") or "").strip() or None

    # Build kwargs defensively because older schemas may not have all fields.
    contact_kwargs: dict = {}
    if hasattr(Contact, "name"):
        contact_kwargs["name"] = name
    if hasattr(Contact, "phone"):
        contact_kwargs["phone"] = phone
    if hasattr(Contact, "email"):
        contact_kwargs["email"] = email

    contact = Contact(**contact_kwargs)
    if hasattr(contact, "phone_ext"):
        contact.phone_ext = phone_ext

    # Assign parent linkage across schemas
    _assign_contact_parent(contact, parent_type, parent_id)

    # Persist selected role
    if role_id is not None and hasattr(contact, "contact_role_id"):
        contact.contact_role_id = role_id
    # Keep legacy label field populated as well (templates often display contact.role)
    if role_label and hasattr(contact, "role"):
        contact.role = role_label

    # Only set contact.title if explicitly posted
    if hasattr(contact, "title") and incoming_form.get("title") is not None:
        raw_title = (incoming_form.get("title") or "").strip()
        contact.title = raw_title if raw_title != "" else None
    if fax is not None and hasattr(contact, "fax"):
        contact.fax = fax
    if notes is not None and hasattr(contact, "notes"):
        contact.notes = notes

    is_valid = _validate_contactish_fields(
        subject="Contact",
        email=email,
        phone=phone,
        fax=fax,
    )
    if not is_valid:
        # Do not create a contact if validation fails.
        # Defensive rollback: prevents any accidental flushes from sticking.
        db.session.rollback()
        return _render_parent_detail_with_contact_form(
            parent_type=parent_type,
            parent_id=parent_id,
            edit_contact=None,
            form=incoming_form,
        )

    db.session.add(contact)
    db.session.commit()
    flash("Contact added.", "success")

    if parent_type == "carrier" and parent_id:
        return redirect(url_for("main.carrier_detail", carrier_id=parent_id))
    if parent_type == "employer" and parent_id:
        return redirect(url_for("main.employer_detail", employer_id=parent_id))
    if parent_type == "provider" and parent_id:
        return redirect(url_for("main.provider_detail", provider_id=parent_id))

    ref = request.referrer
    if ref:
        return redirect(ref)

    return redirect(url_for("main.claims_list"))


@bp.route(
    "/contacts/<int:contact_id>/update/<string:parent_type>/<int:parent_id>",
    methods=["POST"],
)
def contact_update(contact_id: int, parent_type: str, parent_id: int):
    contact = Contact.query.get_or_404(contact_id)
    incoming_form = _normalize_contact_form(request.form)

    # Parse role from form (supports id or label)
    role_id, role_label = _parse_contact_role_from_form(incoming_form)

    if hasattr(contact, "name"):
        contact.name = (request.form.get("name") or "").strip() or None
    # Persist selected role (supports both FK + legacy label)
    if hasattr(contact, "contact_role_id"):
        contact.contact_role_id = role_id
    if role_label and hasattr(contact, "role"):
        contact.role = role_label

    # Only set contact.title if explicitly posted
    if hasattr(contact, "title") and request.form.get("title") is not None:
        raw_title = (request.form.get("title") or "").strip()
        contact.title = raw_title if raw_title != "" else contact.title
    if hasattr(contact, "phone"):
        contact.phone = (request.form.get("phone") or "").strip() or None
    if hasattr(contact, "phone_ext"):
        contact.phone_ext = _clean_phone_ext(request.form.get("phone_ext"))
    if hasattr(contact, "fax"):
        contact.fax = (request.form.get("fax") or "").strip() or None
    if hasattr(contact, "email"):
        contact.email = (request.form.get("email") or "").strip() or None
    if hasattr(contact, "notes"):
        contact.notes = (request.form.get("notes") or "").strip() or None

    is_valid = _validate_contactish_fields(
        subject="Contact",
        email=getattr(contact, "email", None),
        phone=getattr(contact, "phone", None),
        fax=getattr(contact, "fax", None),
    )

    if not is_valid:
        # Do not commit invalid changes.
        # IMPORTANT: rollback so pending attribute changes cannot be autoflushed
        # during template rendering (queries in the detail view).
        db.session.rollback()
        return _render_parent_detail_with_contact_form(
            parent_type=parent_type,
            parent_id=parent_id,
            edit_contact=contact,
            form=incoming_form,
        )

    db.session.commit()
    flash("Contact updated.", "success")

    if parent_type == "carrier" and parent_id:
        return redirect(url_for("main.carrier_detail", carrier_id=parent_id))
    if parent_type == "employer" and parent_id:
        return redirect(url_for("main.employer_detail", employer_id=parent_id))
    if parent_type == "provider" and parent_id:
        return redirect(url_for("main.provider_detail", provider_id=parent_id))

    ref = request.referrer
    if ref:
        return redirect(ref)

    return redirect(url_for("main.claims_list"))


@bp.route("/contacts/<int:contact_id>/delete", methods=["POST"])
def contact_delete(contact_id: int):
    contact = Contact.query.get_or_404(contact_id)

    # Prefer explicit parent info from the POST (templates can include hidden fields)
    form_parent_type = (request.form.get("parent_type") or "").strip() or None
    form_parent_id_raw = (request.form.get("parent_id") or "").strip() or None
    form_parent_id = int(form_parent_id_raw) if (form_parent_id_raw and form_parent_id_raw.isdigit()) else None

    parent_type = form_parent_type or getattr(contact, "parent_type", None)
    parent_id = form_parent_id or getattr(contact, "parent_id", None)

    # Legacy schema fallback: infer parent from explicit FK columns if present.
    if not parent_type or not parent_id:
        if hasattr(contact, "provider_id") and getattr(contact, "provider_id", None):
            parent_type = parent_type or "provider"
            parent_id = parent_id or getattr(contact, "provider_id")
        elif hasattr(contact, "employer_id") and getattr(contact, "employer_id", None):
            parent_type = parent_type or "employer"
            parent_id = parent_id or getattr(contact, "employer_id")
        elif hasattr(contact, "carrier_id") and getattr(contact, "carrier_id", None):
            parent_type = parent_type or "carrier"
            parent_id = parent_id or getattr(contact, "carrier_id")

    # ------------------------------------------------------------------
    # IMPORTANT: Some claims can reference a Contact (e.g. carrier_contact_id).
    # If we delete the contact without clearing those references, Postgres will
    # raise a FK violation. Our chosen behavior is: auto-clear the reference(s)
    # and proceed with deletion. Users can re-select a replacement later.
    # ------------------------------------------------------------------

    cleared_any = False

    try:
        # Common / known FK on Claim
        if hasattr(Claim, "carrier_contact_id"):
            cleared = (
                db.session.query(Claim)
                .filter(Claim.carrier_contact_id == contact.id)
                .update({Claim.carrier_contact_id: None}, synchronize_session=False)
            )
            if cleared:
                cleared_any = True

        # Future-proofing: if other claim contact FK columns exist, clear them too.
        if hasattr(Claim, "employer_contact_id"):
            cleared = (
                db.session.query(Claim)
                .filter(Claim.employer_contact_id == contact.id)
                .update({Claim.employer_contact_id: None}, synchronize_session=False)
            )
            if cleared:
                cleared_any = True

        if hasattr(Claim, "provider_contact_id"):
            cleared = (
                db.session.query(Claim)
                .filter(Claim.provider_contact_id == contact.id)
                .update({Claim.provider_contact_id: None}, synchronize_session=False)
            )
            if cleared:
                cleared_any = True

        # If any references were cleared, flush so the delete won't violate FKs.
        if cleared_any:
            db.session.flush()

    except Exception:
        # If anything goes sideways, rollback and show a safe message.
        db.session.rollback()
        flash(
            "Could not delete that contact because it is still referenced by one or more claims.",
            "danger",
        )
        # Best-effort: return to where the user was.
        if parent_type == "carrier" and parent_id:
            return redirect(url_for("main.carrier_detail", carrier_id=parent_id))
        if parent_type == "employer" and parent_id:
            return redirect(url_for("main.employer_detail", employer_id=parent_id))
        if parent_type == "provider" and parent_id:
            return redirect(url_for("main.provider_detail", provider_id=parent_id))
        ref = request.referrer
        if ref:
            return redirect(ref)
        return redirect(url_for("main.claims_list"))

    db.session.delete(contact)
    db.session.commit()

    if cleared_any:
        flash("Contact deleted. Any claim references to that contact were cleared.", "success")
    else:
        flash("Contact deleted.", "success")

    # Best-effort: return to where the user was.
    if parent_type == "carrier" and parent_id:
        return redirect(url_for("main.carrier_detail", carrier_id=parent_id))
    if parent_type == "employer" and parent_id:
        return redirect(url_for("main.employer_detail", employer_id=parent_id))
    if parent_type == "provider" and parent_id:
        return redirect(url_for("main.provider_detail", provider_id=parent_id))

    # Fall back to referrer if available, otherwise claims list.
    ref = request.referrer
    if ref:
        return redirect(ref)

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
    if parent_type and _contact_supports_polymorphic():
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
                "phone_ext": getattr(c, "phone_ext", None),
                "fax": getattr(c, "fax", None),
                "parent_type": getattr(c, "parent_type", None),
                "parent_id": getattr(c, "parent_id", None),
                "carrier_id": getattr(c, "carrier_id", None),
                "employer_id": getattr(c, "employer_id", None),
                "provider_id": getattr(c, "provider_id", None),
            }
        )

    return jsonify(payload)


# -----------------------------------------------------------------------------
# Back-compat: Create a new report from Claim Detail
# -----------------------------------------------------------------------------


def _safe_redirect_report_edit(report_id: int, claim_id: int):
    """Redirect to the report edit screen across evolving endpoint names.

    In this app the canonical report routes are claim-scoped:
      /claims/<claim_id>/reports/<report_id>/edit

    So most endpoints require BOTH claim_id and report_id. This helper tries the
    known variants and falls back safely.
    """

    candidates = [
        # Current canonical (per route map)
        ("main.report_edit", {"claim_id": claim_id, "report_id": report_id}),
        ("main.report_detail", {"claim_id": claim_id, "report_id": report_id}),
        ("main.report_print", {"claim_id": claim_id, "report_id": report_id}),

        # If a future split introduces a reports blueprint
        ("reports.edit", {"claim_id": claim_id, "report_id": report_id}),
        ("reports.detail", {"claim_id": claim_id, "report_id": report_id}),

        # Very old shapes (report_id-only). Keep as best-effort.
        ("main.report_edit", {"report_id": report_id}),
        ("main.report_detail", {"report_id": report_id}),
        ("reports.edit", {"report_id": report_id}),
        ("reports.detail", {"report_id": report_id}),

        # Fallbacks
        ("main.claim_detail", {"claim_id": claim_id}),
        ("main.claims_list", {}),
    ]

    for endpoint, kwargs in candidates:
        try:
            return redirect(url_for(endpoint, **kwargs))
        except Exception:
            continue

    return redirect(url_for("main.claims_list"))


@bp.route("/claims/<int:claim_id>/reports/new", methods=["GET", "POST"])
@bp.route("/claims/<int:claim_id>/report/new", methods=["GET", "POST"])  # legacy singular
@bp.route("/claims/<int:claim_id>/reports/create", methods=["GET", "POST"])  # legacy create
def claim_report_new(claim_id: int):
    """Create a new Report row for a claim and redirect to its edit screen.

    This exists because older templates/buttons may POST to claim-scoped routes
    instead of the newer reports module.
    """

    claim = Claim.query.get_or_404(claim_id)

    if Report is None:
        flash("Report model is unavailable; cannot create a report.", "danger")
        return redirect(url_for("main.claim_detail", claim_id=claim_id))

    raw_type = (
        request.form.get("report_type")
        or request.form.get("type")
        or request.args.get("report_type")
        or request.args.get("type")
        or ""
    ).strip().lower()
    # Accept a few common variants
    if raw_type in ("initial", "initial report", "init"):
        report_type = "initial"
    elif raw_type in ("progress", "progress report", "prog"):
        report_type = "progress"
    elif raw_type in ("closure", "close", "closure report"):
        report_type = "closure"
    else:
        flash("Please select a report type before creating a report.", "warning")
        return redirect(url_for("main.claim_detail", claim_id=claim_id))

    today = date.today()

    # Determine DOS defaults.
    # Initial: referral date -> today
    # Progress/Closure: day after last report DOS end -> today (fallback to today)
    dos_start = today
    dos_end = today

    if report_type == "initial":
        ref = getattr(claim, "referral_date", None)
        if isinstance(ref, datetime):
            dos_start = ref.date()
        elif isinstance(ref, date):
            dos_start = ref
        else:
            dos_start = today
    else:
        last = (
            Report.query.filter(Report.claim_id == claim_id)
            .order_by(Report.dos_end.desc().nullslast(), Report.created_at.desc().nullslast(), Report.id.desc())
            .first()
        )
        last_end = getattr(last, "dos_end", None) if last else None
        if isinstance(last_end, datetime):
            last_end = last_end.date()
        if isinstance(last_end, date):
            dos_start = last_end + timedelta(days=1)
        else:
            dos_start = today

    report = Report(
        claim_id=claim_id,
        report_type=report_type,
        dos_start=dos_start,
        dos_end=dos_end,
    )

    # Some schemas include created_at/updated_at defaults; set created_at if present.
    if hasattr(report, "created_at") and getattr(report, "created_at", None) is None:
        report.created_at = datetime.utcnow()

    db.session.add(report)
    db.session.commit()

    flash(f"{report_type.title()} report created.", "success")
    return _safe_redirect_report_edit(report.id, claim_id)


__all__: list[str] = []