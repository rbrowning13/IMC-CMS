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

    if request.method == "POST":
        form = request.form

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

        business_name_raw = (form.get("business_name") or "").strip()
        state_raw = (form.get("state") or "").strip() or "ID"

        phone_raw = (form.get("phone") or "").strip()
        fax_raw = (form.get("fax") or "").strip()
        email_raw = (form.get("email") or "").strip()
        postal_code_raw = (form.get("postal_code") or form.get("zip") or "").strip()

        if error is None:
            if phone_raw and not validate_phone_or_fax(phone_raw):
                error = "Phone number must have 10 digits."
            elif fax_raw and not validate_phone_or_fax(fax_raw):
                error = "Fax number must have 10 digits."
            elif email_raw and not validate_email(email_raw):
                error = "Email address is invalid."
            elif postal_code_raw and not validate_postal_code(postal_code_raw):
                error = "ZIP code is invalid."

        hourly_rate, err = _parse_float(form.get("hourly_rate"), "Hourly rate")
        if err and error is None:
            error = err

        telephonic_rate, err = _parse_float(form.get("telephonic_rate"), "Telephonic rate")
        if err and error is None:
            error = err

        mileage_rate, err = _parse_float(form.get("mileage_rate"), "Mileage rate")
        if err and error is None:
            error = err

        if error is None:
            settings.business_name = business_name_raw or settings.business_name
            settings.state = state_raw

            if hasattr(settings, "hourly_rate"):
                settings.hourly_rate = hourly_rate
            if hasattr(settings, "telephonic_rate"):
                settings.telephonic_rate = telephonic_rate
            if hasattr(settings, "mileage_rate"):
                settings.mileage_rate = mileage_rate

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
