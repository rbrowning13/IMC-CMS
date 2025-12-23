"""Settings and configuration routes.

This module was split out of the old monolithic routes.py.

Notes during transition:
- Some helpers may be duplicated temporarily (e.g., settings loader)
  until we consolidate them into app/routes/helpers.py.
"""

from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for

from sqlalchemy import func

from ..utils import validation as _validation


def validate_email(value: str) -> bool:
    fn = getattr(_validation, "validate_email", None) or getattr(_validation, "is_valid_email", None)
    if fn is None:
        # permissive fallback: only fail on obvious missing @ when populated
        value = (value or "").strip()
        return (value == "") or ("@" in value)
    return bool(fn(value))


def validate_phone_or_fax(value: str) -> bool:
    fn = getattr(_validation, "validate_phone_or_fax", None) or getattr(_validation, "is_valid_phone_or_fax", None)
    if fn is None:
        # permissive fallback: allow blanks; otherwise require 10 digits
        digits = "".join([c for c in (value or "") if c.isdigit()])
        return (digits == "") or (len(digits) == 10)
    return bool(fn(value))


def validate_postal_code(value: str) -> bool:
    fn = getattr(_validation, "validate_postal_code", None) or getattr(_validation, "is_valid_postal_code", None)
    if fn is None:
        # permissive fallback: allow blanks; otherwise require 5 or 9 digits
        digits = "".join([c for c in (value or "") if c.isdigit()])
        return (digits == "") or (len(digits) in (5, 9))
    return bool(fn(value))

from ..extensions import db
from ..models import BarrierOption, BillingActivityCode, Settings, ContactRole

from . import bp


# ---- helpers (temporary duplicates; will move to routes/helpers.py) ----


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


# Helper for Contact Roles textarea
def _contact_roles_text() -> str:
    """Return active ContactRole names as newline-delimited text for the Settings textarea."""
    try:
        roles = (
            ContactRole.query.filter_by(is_active=True)
            .order_by(ContactRole.sort_order, ContactRole.name)
            .all()
        )
        return "\n".join([r.name for r in roles if (r.name or "").strip()])
    except Exception:
        # If the ContactRole table/model isn't available yet, keep Settings usable.
        return ""


@bp.route("/settings", methods=["GET", "POST"])
def settings_view():
    """View/edit app Settings."""
    settings = _ensure_settings()
    error = None

    def _parse_float(raw: str, label: str):
        raw = (raw or "").strip()
        if raw == "":
            return None, None
        try:
            return float(raw), None
        except ValueError:
            return None, f"{label} must be a number."

    def _parse_int(raw: str, label: str):
        raw = (raw or "").strip()
        if raw == "":
            return None, None
        try:
            return int(raw), None
        except ValueError:
            return None, f"{label} must be a whole number."

    if request.method == "POST":
        # Always read raw form values first so we can re-render without losing user input.
        form = request.form

        # Text fields (blank means user wants to clear; we store None when possible)
        business_name_raw = (form.get("business_name") or "").strip()
        state_raw = (form.get("state") or "").strip() or "ID"

        address1_raw = (form.get("address1") or "").strip()
        address2_raw = (form.get("address2") or "").strip()
        city_raw = (form.get("city") or "").strip()
        postal_code_raw = (form.get("postal_code") or form.get("zip") or "").strip()

        phone_raw = (form.get("phone") or "").strip()
        fax_raw = (form.get("fax") or "").strip()
        email_raw = (form.get("email") or "").strip()

        # Validate contact fields (blank is allowed; only validate when populated)
        if error is None:
            if phone_raw and not validate_phone_or_fax(phone_raw):
                error = "Phone number must have 10 digits."
            elif fax_raw and not validate_phone_or_fax(fax_raw):
                error = "Fax number must have 10 digits."
            elif email_raw and not validate_email(email_raw):
                error = "Email address is invalid."
            elif postal_code_raw and not validate_postal_code(postal_code_raw):
                error = "ZIP code is invalid."

        # DB column is responsible_case_manager; accept a few legacy/template field names
        case_manager_raw = (
            form.get("responsible_case_manager")
            or form.get("case_manager")
            or form.get("case_manager_name")
            or ""
        ).strip()

        # DB column is `ein`; accept legacy/template field names
        ein_tax_id_raw = (
            form.get("ein")
            or form.get("ein_tax_id")
            or form.get("ein_tax_id_raw")
            or ""
        ).strip()

        report_footer_text_raw = (form.get("report_footer_text") or "").strip()

        invoice_footer_text_raw = (form.get("invoice_footer_text") or "").strip()
        accent_color_raw = (form.get("accent_color") or form.get("accent_color_hex") or "").strip()
        # DB column is payment_terms_default; accept legacy/template field name
        default_payment_terms_raw = (
            form.get("payment_terms_default")
            or form.get("default_payment_terms")
            or ""
        ).strip()

        # Targets (some templates use slightly different field names)
        target_min_hours_raw = (
            form.get("target_min_hours_week")
            or form.get("target_min_hours_per_week")
            or form.get("target_min_hours")
            or ""
        ).strip()
        target_max_hours_raw = (
            form.get("target_max_hours_week")
            or form.get("target_max_hours_per_week")
            or form.get("target_max_hours")
            or ""
        ).strip()

        # Documents root (optional; may not exist in schema)
        documents_root_raw = (form.get("documents_root") or "").strip()

        # Contact Roles textarea value
        contact_roles_text_raw = (form.get("contact_roles") or form.get("contact_roles_text") or "").strip()

        # Numbers
        hourly_rate, err = _parse_float(form.get("hourly_rate"), "Hourly rate")
        if error is None and err:
            error = err

        telephonic_rate, err = _parse_float(form.get("telephonic_rate"), "Telephonic rate")
        if error is None and err:
            error = err

        mileage_rate, err = _parse_float(form.get("mileage_rate"), "Mileage rate")
        if error is None and err:
            error = err

        dormant_claim_days, err = _parse_int(form.get("dormant_claim_days"), "Dormant claim days")
        if error is None and err:
            error = err

        target_min_hours_week, err = _parse_float(target_min_hours_raw, "Target min hours/week")
        if error is None and err:
            error = err

        target_max_hours_week, err = _parse_float(target_max_hours_raw, "Target max hours/week")
        if error is None and err:
            error = err

        # Apply updates to the in-memory object first so templates can always echo what the user typed.
        # Required-ish fields
        if hasattr(settings, "business_name"):
            # Business name: do NOT allow clearing to blank; keep old value if user submits blank.
            settings.business_name = business_name_raw or settings.business_name

        if hasattr(settings, "state"):
            settings.state = state_raw or "ID"

        # Optional text fields: blank -> empty string (user clearing the field)
        for attr, raw in (
            ("address1", address1_raw),
            ("address2", address2_raw),
            ("city", city_raw),
            ("postal_code", postal_code_raw),
            ("phone", phone_raw),
            ("fax", fax_raw),
            ("email", email_raw),
            # Settings table columns
            ("responsible_case_manager", case_manager_raw),
            ("ein", ein_tax_id_raw),
            ("payment_terms_default", default_payment_terms_raw),

            # Back-compat / older model attrs (harmless if absent)
            ("case_manager", case_manager_raw),
            ("ein_tax_id", ein_tax_id_raw),
            ("default_payment_terms", default_payment_terms_raw),

            ("report_footer_text", report_footer_text_raw),
            ("invoice_footer_text", invoice_footer_text_raw),
            ("accent_color", accent_color_raw),
            ("documents_root", documents_root_raw),
        ):
            if hasattr(settings, attr):
                setattr(settings, attr, raw)

        # Numeric fields: if blank, attempt to clear (set None). If DB rejects NULL, we'll catch on commit.
        if hasattr(settings, "hourly_rate"):
            settings.hourly_rate = hourly_rate
        if hasattr(settings, "telephonic_rate"):
            settings.telephonic_rate = telephonic_rate
        if hasattr(settings, "mileage_rate"):
            settings.mileage_rate = mileage_rate
        if hasattr(settings, "dormant_claim_days"):
            settings.dormant_claim_days = dormant_claim_days

        # Targets: support multiple historical/alternate column names.
        if target_min_hours_week is not None:
            if hasattr(settings, "target_min_hours_week"):
                settings.target_min_hours_week = target_min_hours_week
            elif hasattr(settings, "target_min_hours_per_week"):
                settings.target_min_hours_per_week = target_min_hours_week
            elif hasattr(settings, "target_min_hours"):
                settings.target_min_hours = target_min_hours_week

        if target_max_hours_week is not None:
            if hasattr(settings, "target_max_hours_week"):
                settings.target_max_hours_week = target_max_hours_week
            elif hasattr(settings, "target_max_hours_per_week"):
                settings.target_max_hours_per_week = target_max_hours_week
            elif hasattr(settings, "target_max_hours"):
                settings.target_max_hours = target_max_hours_week

        # Contact Roles textarea (stored in ContactRole table). One role per line.
        # We sync the active set to match the textarea contents.
        if "contact_roles" in form or "contact_roles_text" in form:
            lines = [ln.strip() for ln in (contact_roles_text_raw or "").splitlines()]
            desired = [ln for ln in lines if ln]

            # Build lookup of existing roles by lowercase name.
            existing_roles = ContactRole.query.all()
            by_lc = {((r.name or "").strip().lower()): r for r in existing_roles if (r.name or "").strip()}

            desired_lc = set([d.lower() for d in desired])

            # Deactivate anything not present.
            for r in existing_roles:
                r_name = (r.name or "").strip()
                if not r_name:
                    continue
                if r_name.lower() not in desired_lc:
                    r.is_active = False

            # Upsert desired in order.
            sort_base = 10
            for idx, name in enumerate(desired):
                key = name.lower()
                role = by_lc.get(key)
                if role is None:
                    role = ContactRole(name=name, sort_order=(idx + 1) * sort_base, is_active=True)
                    db.session.add(role)
                    by_lc[key] = role
                else:
                    role.name = name  # normalize casing
                    role.is_active = True
                    role.sort_order = (idx + 1) * sort_base

        if error is None:
            try:
                db.session.commit()
                flash("Settings saved.", "success")
                return redirect(url_for("main.settings_view"))
            except Exception as e:
                db.session.rollback()
                error = f"Could not save settings: {e}"
                flash(error, "danger")

        # Validation/commit error: re-render with the entered values still present.
        # Ensure the user sees the error even if the template doesn't render the `error` variable.
        if error:
            flash(str(error), "danger")
        return render_template(
            "settings.html",
            active_page="settings",
            settings=settings,
            error=error,
            contact_roles_text=contact_roles_text_raw,
        )

    return render_template(
        "settings.html",
        active_page="settings",
        settings=settings,
        error=error,
        contact_roles_text=_contact_roles_text(),
    )


# ---- Settings: Barrier Options management ----


@bp.route("/settings/barriers", methods=["GET", "POST"])
def settings_barriers():
    """Manage BarrierOption entries (used in report barriers checklist)."""
    settings = _ensure_settings()  # reuse for header / business name

    # Auto-seed default barrier options if empty
    if BarrierOption.query.count() == 0:
        default_barriers = [
            ("General", "Depression / PTSD / Psychosocial", 10),
            ("General", "Smoker", 20),
            ("General", "Treatment Noncompliance", 30),
            ("General", "Diabetes", 40),
            ("General", "Frequently Missing Work", 50),
            ("General", "Hypertension", 60),
            ("General", "Substance Abuse History", 70),
            ("General", "Pain Management", 80),
            ("General", "Legal Representation", 90),
            ("General", "Surgery or Recent Hospital Stay", 100),
            ("General", "Late Injury Reporting", 110),
        ]
        for category, label, sort_order in default_barriers:
            db.session.add(
                BarrierOption(
                    category=category,
                    label=label,
                    sort_order=sort_order,
                    is_active=True,
                )
            )
        db.session.commit()

    error = None

    if request.method == "POST":
        label = (request.form.get("label") or "").strip()
        category = (request.form.get("category") or "").strip() or None
        sort_order_raw = (request.form.get("sort_order") or "").strip()

        sort_order = None
        if sort_order_raw:
            try:
                sort_order = int(sort_order_raw)
            except ValueError:
                sort_order = None

        if not label:
            error = "Label is required for a barrier."
        else:
            if sort_order is None:
                sort_order = 999

            opt = BarrierOption(
                label=label,
                category=category,
                sort_order=sort_order,
                is_active=True,
            )
            db.session.add(opt)
            db.session.commit()
            flash("Barrier added.", "success")
            return redirect(url_for("main.settings_barriers"))

    options = (
        BarrierOption.query
        .order_by(
            BarrierOption.category.nullsfirst(),
            BarrierOption.sort_order,
            BarrierOption.label,
        )
        .all()
    )

    return render_template(
        "settings_barriers.html",
        active_page="settings",
        settings=settings,
        options=options,
        error=error,
    )


@bp.route("/settings/barriers/<int:barrier_id>/edit", methods=["GET", "POST"])
def settings_barrier_edit(barrier_id: int):
    """Edit an existing BarrierOption."""
    settings = _ensure_settings()
    barrier = BarrierOption.query.get_or_404(barrier_id)
    error = None

    if request.method == "POST":
        label = (request.form.get("label") or "").strip()
        category = (request.form.get("category") or "").strip() or None
        sort_order_raw = (request.form.get("sort_order") or "").strip()
        is_active_raw = (request.form.get("is_active") or "").strip().lower()

        sort_order = None
        if sort_order_raw:
            try:
                sort_order = int(sort_order_raw)
            except ValueError:
                sort_order = None

        if not label:
            error = "Label is required for a barrier."
        else:
            barrier.label = label
            barrier.category = category
            if sort_order is not None:
                barrier.sort_order = sort_order

            barrier.is_active = is_active_raw in ("on", "true", "1", "yes")

            db.session.commit()
            flash("Barrier updated.", "success")
            return redirect(url_for("main.settings_barriers"))

    return render_template(
        "settings_barrier_form.html",
        active_page="settings",
        settings=settings,
        barrier=barrier,
        error=error,
    )


@bp.route("/settings/barriers/<int:barrier_id>/toggle", methods=["POST"])
def settings_barrier_toggle(barrier_id: int):
    """Quick toggle for a barrier's active flag from the list view."""
    barrier = BarrierOption.query.get_or_404(barrier_id)
    barrier.is_active = not bool(barrier.is_active)
    db.session.commit()
    flash("Barrier status updated.", "success")
    return redirect(url_for("main.settings_barriers"))


# ---- Settings: Billing activity codes (billables dropdown) ----


@bp.route("/settings/billables", methods=["GET", "POST"])
def settings_billables():
    """Manage BillingActivityCode entries used for billable items."""
    settings = _ensure_settings()
    error = None

    def _parse_int(raw: str, label: str):
        raw = (raw or "").strip()
        if raw == "":
            return None, None
        try:
            return int(raw), None
        except ValueError:
            return None, f"{label} must be a whole number."

    # Auto-seed default billing activity codes if empty
    if BillingActivityCode.query.count() == 0:
        defaults = [
            ("Admin", "Admin", 10),
            ("Email", "Email", 20),
            ("Exp", "Expense", 30),
            ("Fax", "Fax", 40),
            ("FR", "File Review", 50),
            ("GDL", "Guideline", 60),
            ("LTR", "Letter", 70),
            ("MR", "Medical Records", 80),
            ("MTG", "Meeting", 90),
            ("MIL", "Mileage", 100),
            ("REP", "Report", 110),
            ("RR", "Record Review", 120),
            ("TC", "Telephone Call", 130),
            ("TCM", "Telephonic Case Management", 140),
            ("Text", "Text", 150),
            ("Travel", "Travel", 160),
            ("Wait", "Wait", 170),
            ("NO BILL", "NO BILL", 999),
            ("MedRes", "Medical Research", 180),
            ("VisitPrep", "Visit Prep", 190),
            ("FCM", "Field Case Management", 200),
        ]
        for code, label, sort_order in defaults:
            db.session.add(
                BillingActivityCode(
                    code=code,
                    label=label,
                    sort_order=sort_order,
                    is_active=True,
                )
            )
        db.session.commit()

    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        label_raw = (request.form.get("label") or "").strip()

        # DB constraint: label is NOT NULL. If user leaves it blank, default label to the code.
        label = label_raw or code
        sort_order, sort_err = _parse_int(request.form.get("sort_order"), "Sort order")

        if not code:
            error = "Code is required."
        elif len(code) > 20:
            error = "Code must be 20 characters or fewer."
        elif sort_err:
            error = sort_err
        else:
            existing = BillingActivityCode.query.filter_by(code=code).first()
            if existing:
                error = "That billing code already exists."
            else:
                if sort_order is None:
                    sort_order = 999

                opt = BillingActivityCode(
                    code=code,
                    label=label,
                    sort_order=sort_order,
                    is_active=True,
                )
                db.session.add(opt)
                db.session.commit()
                flash("Billing code added.", "success")
                return redirect(url_for("main.settings_billables"))

    codes = (
        BillingActivityCode.query
        .order_by(BillingActivityCode.sort_order, BillingActivityCode.code)
        .all()
    )

    return render_template(
        "settings_billables.html",
        active_page="settings",
        settings=settings,
        codes=codes,
        error=error,
    )


@bp.route("/settings/billables/<int:code_id>/edit", methods=["GET", "POST"])
def settings_billable_edit(code_id: int):
    """Edit a BillingActivityCode."""
    settings = _ensure_settings()
    code_obj = BillingActivityCode.query.get_or_404(code_id)
    error = None

    def _parse_int(raw: str, label: str):
        raw = (raw or "").strip()
        if raw == "":
            return None, None
        try:
            return int(raw), None
        except ValueError:
            return None, f"{label} must be a whole number."

    if request.method == "POST":
        new_code = (request.form.get("code") or "").strip()
        label_raw = (request.form.get("label") or "").strip()

        # DB constraint: label is NOT NULL. If user leaves it blank, default label to the code.
        label = label_raw or new_code
        sort_order, sort_err = _parse_int(request.form.get("sort_order"), "Sort order")
        is_active_raw = (request.form.get("is_active") or "").strip().lower()

        if not new_code:
            error = "Code is required."
        elif len(new_code) > 20:
            error = "Code must be 20 characters or fewer."
        elif sort_err:
            error = sort_err
        else:
            # Prevent duplicate codes if the user changes the code string
            if new_code != code_obj.code:
                exists = BillingActivityCode.query.filter_by(code=new_code).first()
                if exists:
                    error = "That billing code already exists."

        if error is None:
            code_obj.code = new_code
            code_obj.label = label
            if sort_order is not None:
                code_obj.sort_order = sort_order
            code_obj.is_active = is_active_raw in ("on", "true", "1", "yes")

            db.session.commit()
            flash("Billing code updated.", "success")
            return redirect(url_for("main.settings_billables"))

    return render_template(
        "settings_billable_form.html",
        active_page="settings",
        settings=settings,
        code_obj=code_obj,
        error=error,
    )


@bp.route("/settings/billables/<int:code_id>/toggle", methods=["POST"])
def settings_billable_toggle(code_id: int):
    """Quick toggle for a BillingActivityCode's active flag from the list view."""
    code_obj = BillingActivityCode.query.get_or_404(code_id)
    code_obj.is_active = not bool(code_obj.is_active)
    db.session.commit()
    flash("Billing code status updated.", "success")
    return redirect(url_for("main.settings_billables"))


# ---- Settings: Contact roles (used for carrier/employer/provider contacts) ----


@bp.route("/settings/contact-roles", methods=["GET", "POST"])
def settings_contact_roles():
    """Manage ContactRole entries used for contact role dropdowns."""
    settings = _ensure_settings()
    error = None

    def _parse_int(raw: str, label: str):
        raw = (raw or "").strip()
        if raw == "":
            return None, None
        try:
            return int(raw), None
        except ValueError:
            return None, f"{label} must be a whole number."

    # Auto-seed a small default set if empty.
    if ContactRole.query.count() == 0:
        defaults = [
            ("Adjuster", 10),
            ("HR", 20),
            ("Supervisor", 30),
            ("Nurse Case Manager", 40),
            ("Attorney", 50),
            ("Other", 999),
        ]
        for name, sort_order in defaults:
            db.session.add(ContactRole(name=name, sort_order=sort_order, is_active=True))
        db.session.commit()

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        sort_order, sort_err = _parse_int(request.form.get("sort_order"), "Sort order")

        if not name:
            error = "Role name is required."
        elif sort_err:
            error = sort_err
        else:
            existing = ContactRole.query.filter_by(name=name).first()
            if existing:
                error = "That role already exists."
            else:
                if sort_order is None:
                    sort_order = 999
                role = ContactRole(name=name, sort_order=sort_order, is_active=True)
                db.session.add(role)
                db.session.commit()
                flash("Contact role added.", "success")
                return redirect(url_for("main.settings_contact_roles"))

    roles = (
        ContactRole.query
        .order_by(ContactRole.sort_order, ContactRole.name)
        .all()
    )

    return render_template(
        "settings_contact_roles.html",
        active_page="settings",
        settings=settings,
        roles=roles,
        error=error,
    )


@bp.route("/settings/contact-roles/<int:role_id>/edit", methods=["GET", "POST"])
def settings_contact_role_edit(role_id: int):
    """Edit a ContactRole."""
    settings = _ensure_settings()
    role = ContactRole.query.get_or_404(role_id)
    error = None

    def _parse_int(raw: str, label: str):
        raw = (raw or "").strip()
        if raw == "":
            return None, None
        try:
            return int(raw), None
        except ValueError:
            return None, f"{label} must be a whole number."

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        sort_order, sort_err = _parse_int(request.form.get("sort_order"), "Sort order")
        is_active_raw = (request.form.get("is_active") or "").strip().lower()

        if not name:
            error = "Role name is required."
        elif sort_err:
            error = sort_err
        else:
            # Prevent duplicates when renaming
            if name != role.name:
                exists = ContactRole.query.filter_by(name=name).first()
                if exists:
                    error = "That role already exists."

        if error is None:
            role.name = name
            if sort_order is not None:
                role.sort_order = sort_order
            role.is_active = is_active_raw in ("on", "true", "1", "yes")

            db.session.commit()
            flash("Contact role updated.", "success")
            return redirect(url_for("main.settings_contact_roles"))

    return render_template(
        "settings_contact_role_form.html",
        active_page="settings",
        settings=settings,
        role=role,
        error=error,
    )


@bp.route("/settings/contact-roles/<int:role_id>/toggle", methods=["POST"])
def settings_contact_role_toggle(role_id: int):
    """Quick toggle for a ContactRole's active flag from the list view."""
    role = ContactRole.query.get_or_404(role_id)
    role.is_active = not bool(role.is_active)
    db.session.commit()
    flash("Contact role status updated.", "success")
    return redirect(url_for("main.settings_contact_roles"))