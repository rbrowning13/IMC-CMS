"""Settings and configuration routes.

This module was split out of the old monolithic routes.py.

Notes during transition:
- Some helpers may be duplicated temporarily (e.g., settings loader)
  until we consolidate them into app/routes/helpers.py.
"""

from __future__ import annotations

import os
import time
import re
import json

from flask import (
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.utils import secure_filename
from sqlalchemy import func

from ..utils import validation as _validation
from ..extensions import db
from ..models import BarrierOption, BillingActivityCode, Settings, ContactRole

from . import bp


# ---------------------------------------------------------------------
# Validation helpers (permissive + backward-compatible)
# ---------------------------------------------------------------------

def validate_email(value: str) -> bool:
    fn = getattr(_validation, "validate_email", None) or getattr(_validation, "is_valid_email", None)
    if fn is None:
        value = (value or "").strip()
        return (value == "") or ("@" in value)
    return bool(fn(value))


def validate_phone_or_fax(value: str) -> bool:
    fn = getattr(_validation, "validate_phone_or_fax", None) or getattr(_validation, "is_valid_phone_or_fax", None)
    if fn is None:
        digits = "".join(c for c in (value or "") if c.isdigit())
        return (digits == "") or (len(digits) == 10)
    return bool(fn(value))


def validate_postal_code(value: str) -> bool:
    fn = getattr(_validation, "validate_postal_code", None) or getattr(_validation, "is_valid_postal_code", None)
    if fn is None:
        digits = "".join(c for c in (value or "") if c.isdigit())
        return (digits == "") or (len(digits) in (5, 9))
    return bool(fn(value))


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------

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


def _contact_roles_text() -> str:
    """Return active ContactRole names as newline-delimited text."""
    try:
        roles = (
            ContactRole.query.filter_by(is_active=True)
            .order_by(ContactRole.sort_order, ContactRole.name)
            .all()
        )
        return "\n".join(r.name for r in roles if (r.name or "").strip())
    except Exception:
        return ""


def _save_settings_upload(file_storage, kind: str) -> tuple[str | None, str | None]:
    if not file_storage or not getattr(file_storage, "filename", ""):
        return None, None

    filename = secure_filename(file_storage.filename or "")
    _, ext = os.path.splitext(filename)
    ext = (ext or "").lower()

    allowed = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    if ext not in allowed:
        return None, f"{kind.title()} must be an image ({', '.join(sorted(allowed))})."

    uploads_dir = os.path.join(current_app.static_folder, "uploads")
    os.makedirs(uploads_dir, exist_ok=True)

    ts = int(time.time())
    out_name = f"{kind}_{ts}{ext}"
    out_path = os.path.join(uploads_dir, out_name)

    file_storage.save(out_path)
    return f"uploads/{out_name}", None


# ---------------------------------------------------------------------
# Settings main view
# ---------------------------------------------------------------------

@bp.route("/settings", methods=["GET", "POST"])
def settings_view():
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

    def _sync_contact_roles(text: str):
        """Persist textarea roles into ContactRole rows (active + ordered).

        We keep the existing ContactRole model as the source of truth for role dropdowns.
        """
        lines = text.splitlines() if text else []
        roles = [ln.strip() for ln in lines if ln.strip()]

        # Deactivate all existing roles first (soft update)
        try:
            for r in ContactRole.query.all():
                r.is_active = False
        except Exception:
            # If anything goes sideways, don't hard-fail settings save.
            return roles

        # Reactivate / create roles in the order given
        sort_order = 0
        for name in roles:
            sort_order += 10
            existing = (
                ContactRole.query
                .filter(func.lower(ContactRole.name) == func.lower(name))
                .first()
            )
            if existing:
                existing.name = name  # preserve user's exact casing
                existing.sort_order = sort_order
                existing.is_active = True
            else:
                db.session.add(
                    ContactRole(
                        name=name,
                        sort_order=sort_order,
                        is_active=True,
                    )
                )
        return roles

    if request.method == "POST":
        form = request.form

        # Uploads (logo/signature)
        logo_rel_path, logo_err = _save_settings_upload(request.files.get("logo_file"), "logo")
        sig_rel_path, sig_err = _save_settings_upload(request.files.get("signature_file"), "signature")

        if logo_err:
            error = logo_err
        if sig_err and error is None:
            error = sig_err

        if error is None:
            if logo_rel_path and hasattr(settings, "logo_path"):
                settings.logo_path = logo_rel_path
            if sig_rel_path and hasattr(settings, "signature_path"):
                settings.signature_path = sig_rel_path

        # Business identity / contact
        business_name_raw = (form.get("business_name") or "").strip()
        address1_raw = (form.get("address1") or "").strip()
        address2_raw = (form.get("address2") or "").strip()
        city_raw = (form.get("city") or "").strip()
        state_raw = (form.get("state") or "").strip() or "ID"
        postal_code_raw = (form.get("postal_code") or form.get("zip") or "").strip()

        phone_raw = (form.get("phone") or "").strip()
        fax_raw = (form.get("fax") or "").strip()
        email_raw = (form.get("email") or "").strip()
        ein_raw = (form.get("ein") or "").strip()
        rcm_raw = (form.get("responsible_case_manager") or "").strip()

        # UI / docs
        accent_color_raw = (form.get("accent_color") or "").strip()
        report_footer_raw = (form.get("report_footer_text") or "").strip()
        invoice_footer_raw = (form.get("invoice_footer_text") or "").strip()
        documents_root_raw = (form.get("documents_root") or "").strip()

        # AI/privacy toggles (checkboxes: checked = True, missing = False)
        ai_enabled_raw = "ai_enabled" in form
        ai_allow_provider_names_raw = "ai_allow_provider_names" in form

        # Validate contact-ish fields
        if error is None:
            if phone_raw and not validate_phone_or_fax(phone_raw):
                error = "Phone number must have 10 digits."
            elif fax_raw and not validate_phone_or_fax(fax_raw):
                error = "Fax number must have 10 digits."
            elif email_raw and not validate_email(email_raw):
                error = "Email address is invalid."
            elif postal_code_raw and not validate_postal_code(postal_code_raw):
                error = "ZIP code is invalid."

        # Rates
        hourly_rate, err = _parse_float(form.get("hourly_rate"), "Hourly rate")
        if err and error is None:
            error = err

        telephonic_rate, err = _parse_float(form.get("telephonic_rate"), "Telephonic rate")
        if err and error is None:
            error = err

        mileage_rate, err = _parse_float(form.get("mileage_rate"), "Mileage rate")
        if err and error is None:
            error = err

        # Billing/workload
        payment_terms_default, err = _parse_int(form.get("payment_terms_default"), "Default payment terms")
        if err and error is None:
            error = err

        dormant_claim_days, err = _parse_int(form.get("dormant_claim_days"), "Dormant claim days")
        if err and error is None:
            error = err

        target_min_hours, err = _parse_float(form.get("target_min_hours_per_week"), "Target min hours/week")
        if err and error is None:
            error = err

        target_max_hours, err = _parse_float(form.get("target_max_hours_per_week"), "Target max hours/week")
        if err and error is None:
            error = err

        # Report default hours (if present in template/model)
        initial_report_hours, err = _parse_float(form.get("initial_report_hours"), "Initial report hours")
        if err and error is None:
            error = err

        progress_report_hours, err = _parse_float(form.get("progress_report_hours"), "Progress report hours")
        if err and error is None:
            error = err

        closure_report_hours, err = _parse_float(form.get("closure_report_hours"), "Closure report hours")
        if err and error is None:
            error = err

        # Contact roles textarea
        roles_text = form.get("contact_roles") or ""

        if error is None:
            # Identity/contact
            settings.business_name = business_name_raw or settings.business_name
            if hasattr(settings, "address1"):
                settings.address1 = address1_raw
            if hasattr(settings, "address2"):
                settings.address2 = address2_raw
            if hasattr(settings, "city"):
                settings.city = city_raw
            settings.state = state_raw
            if hasattr(settings, "postal_code"):
                settings.postal_code = postal_code_raw
            if hasattr(settings, "phone"):
                settings.phone = phone_raw
            if hasattr(settings, "fax"):
                settings.fax = fax_raw
            if hasattr(settings, "email"):
                settings.email = email_raw
            if hasattr(settings, "ein"):
                settings.ein = ein_raw
            if hasattr(settings, "responsible_case_manager"):
                settings.responsible_case_manager = rcm_raw

            # Rates
            if hasattr(settings, "hourly_rate") and hourly_rate is not None:
                settings.hourly_rate = hourly_rate
            if hasattr(settings, "telephonic_rate") and telephonic_rate is not None:
                settings.telephonic_rate = telephonic_rate
            if hasattr(settings, "mileage_rate") and mileage_rate is not None:
                settings.mileage_rate = mileage_rate

            # Billing/workload
            if hasattr(settings, "payment_terms_default") and payment_terms_default is not None:
                settings.payment_terms_default = payment_terms_default
            if hasattr(settings, "dormant_claim_days") and dormant_claim_days is not None:
                settings.dormant_claim_days = dormant_claim_days
            if hasattr(settings, "target_min_hours_per_week") and target_min_hours is not None:
                settings.target_min_hours_per_week = target_min_hours
            if hasattr(settings, "target_max_hours_per_week") and target_max_hours is not None:
                settings.target_max_hours_per_week = target_max_hours

            # UI/docs
            if hasattr(settings, "accent_color"):
                settings.accent_color = accent_color_raw
            if hasattr(settings, "report_footer_text"):
                settings.report_footer_text = report_footer_raw
            if hasattr(settings, "invoice_footer_text"):
                settings.invoice_footer_text = invoice_footer_raw
            if hasattr(settings, "documents_root") and documents_root_raw:
                settings.documents_root = documents_root_raw
            # AI/privacy toggles
            if hasattr(settings, "ai_enabled"):
                settings.ai_enabled = ai_enabled_raw
            if hasattr(settings, "ai_allow_provider_names"):
                settings.ai_allow_provider_names = ai_allow_provider_names_raw

            # Report defaults
            if hasattr(settings, "initial_report_hours") and initial_report_hours is not None:
                settings.initial_report_hours = initial_report_hours
            if hasattr(settings, "progress_report_hours") and progress_report_hours is not None:
                settings.progress_report_hours = progress_report_hours
            if hasattr(settings, "closure_report_hours") and closure_report_hours is not None:
                settings.closure_report_hours = closure_report_hours

            # Persist contact roles (ContactRole table) + optional json mirror on Settings
            roles = _sync_contact_roles(roles_text)
            if hasattr(settings, "contact_roles_json"):
                settings.contact_roles_json = json.dumps(roles)

            try:
                db.session.commit()
                flash("Settings saved.", "success")
                return redirect(url_for("main.settings_view"))
            except Exception as e:
                db.session.rollback()
                error = f"Could not save settings: {e}"
                flash(error, "danger")

    return render_template(
        "settings.html",
        active_page="settings",
        settings=settings,
        error=error,
        contact_roles_text=_contact_roles_text(),
    )
# ---------------------------------------------------------------------
# Lists & code tables (simple management screens)
# ---------------------------------------------------------------------

@bp.route("/settings/barriers", methods=["GET", "POST"])
def settings_barriers():
    # Minimal CRUD: add + toggle active
    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "add":
            label = (request.form.get("label") or "").strip()
            category = (request.form.get("category") or "").strip() or None
            sort_order_raw = (request.form.get("sort_order") or "").strip()
            try:
                sort_order = int(sort_order_raw) if sort_order_raw else 0
            except ValueError:
                sort_order = 0

            if not label:
                flash("Label is required.", "danger")
            else:
                bo = BarrierOption(
                    label=label,
                    category=category,
                    sort_order=sort_order,
                    is_active=True,
                )
                db.session.add(bo)
                db.session.commit()
                flash("Barrier option added.", "success")
                return redirect(url_for("main.settings_barriers"))

        elif action == "toggle":
            try:
                bo_id = int(request.form.get("id") or "0")
            except ValueError:
                bo_id = 0
            bo = BarrierOption.query.get(bo_id)
            if not bo:
                flash("Barrier option not found.", "danger")
            else:
                bo.is_active = not bool(getattr(bo, "is_active", True))
                db.session.commit()
                flash("Barrier option updated.", "success")
                return redirect(url_for("main.settings_barriers"))

    items = (
        BarrierOption.query.order_by(
            getattr(BarrierOption, "sort_order", 0),
            getattr(BarrierOption, "category", ""),
            getattr(BarrierOption, "label", ""),
        ).all()
    )

    return render_template(
        "settings_barriers.html",
        active_page="settings",
        items=items,
    )


@bp.route("/settings/billables", methods=["GET", "POST"])
def settings_billables():
    # Minimal CRUD: add + toggle active
    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "add":
            code = (request.form.get("code") or "").strip()
            label = (request.form.get("label") or "").strip()
            sort_order_raw = (request.form.get("sort_order") or "").strip()
            try:
                sort_order = int(sort_order_raw) if sort_order_raw else 0
            except ValueError:
                sort_order = 0

            if not code or not label:
                flash("Code and label are required.", "danger")
            else:
                item = BillingActivityCode(
                    code=code,
                    label=label,
                    sort_order=sort_order,
                    is_active=True,
                )
                db.session.add(item)
                db.session.commit()
                flash("Activity code added.", "success")
                return redirect(url_for("main.settings_billables"))

        elif action == "toggle":
            try:
                item_id = int(request.form.get("id") or "0")
            except ValueError:
                item_id = 0
            item = BillingActivityCode.query.get(item_id)
            if not item:
                flash("Activity code not found.", "danger")
            else:
                item.is_active = not bool(getattr(item, "is_active", True))
                db.session.commit()
                flash("Activity code updated.", "success")
                return redirect(url_for("main.settings_billables"))

    items = (
        BillingActivityCode.query.order_by(
            getattr(BillingActivityCode, "sort_order", 0),
            getattr(BillingActivityCode, "code", ""),
        ).all()
    )

    return render_template(
        "settings_billables.html",
        active_page="settings",
        items=items,
    )
