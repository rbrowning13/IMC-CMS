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

# ---------------------------------------------------------------------
# Settings main view (redirect to General section)
# ---------------------------------------------------------------------

@bp.route("/settings")
def settings_view():
    return redirect(url_for("main.settings_general"))


@bp.route("/settings/general", methods=["GET", "POST"])
def settings_general():
    settings = _ensure_settings()
    error = None

    if request.method == "POST":
        form = request.form

        business_name_raw = (form.get("business_name") or "").strip()
        address1_raw = (form.get("address1") or "").strip()
        address2_raw = (form.get("address2") or "").strip()
        city_raw = (form.get("city") or "").strip()
        state_raw = (form.get("state") or "").strip() or "ID"
        postal_code_raw = (form.get("postal_code") or "").strip()
        phone_raw = (form.get("phone") or "").strip()
        fax_raw = (form.get("fax") or "").strip()
        email_raw = (form.get("email") or "").strip()
        ein_raw = (form.get("ein") or "").strip()
        rcm_raw = (form.get("responsible_case_manager") or "").strip()

        if phone_raw and not validate_phone_or_fax(phone_raw):
            error = "Phone number must have 10 digits."
        elif fax_raw and not validate_phone_or_fax(fax_raw):
            error = "Fax number must have 10 digits."
        elif email_raw and not validate_email(email_raw):
            error = "Email address is invalid."
        elif postal_code_raw and not validate_postal_code(postal_code_raw):
            error = "ZIP code is invalid."

        if error is None:
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

            try:
                db.session.commit()
                flash("General settings saved.", "success")
                return redirect(url_for("main.settings_general"))
            except Exception as e:
                db.session.rollback()
                error = f"Could not save settings: {e}"
                flash(error, "danger")

    return render_template(
        "settings/general.html",
        settings=settings,
        active_section="general",
        error=error,
    )

# ---------------------------------------------------------------------
# Advanced settings route (placeholder)
# ---------------------------------------------------------------------

@bp.route("/settings/advanced", methods=["GET", "POST"])
def settings_advanced():
    settings = _ensure_settings()
    flash("Advanced section not implemented yet.", "info")
    return render_template(
        "settings/general.html",
        settings=settings,
        active_section="advanced",
        error=None,
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
        "settings/settings_barriers.html",
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
        "settings/settings_billables.html",
        active_page="settings",
        items=items,
    )


# ---------------------------------------------------------------------
# Lists landing route (redirect to barriers for now)
# ---------------------------------------------------------------------

@bp.route("/settings/lists", methods=["GET", "POST"])
def settings_lists():
    if request.method == "POST":
        list_type = (request.form.get("list_type") or "").strip()
        action = (request.form.get("action") or "").strip()

        # --------------------
        # CONTACT ROLES
        # --------------------
        if list_type == "roles":
            if action == "add":
                name = (request.form.get("name") or "").strip()
                sort_order_raw = (request.form.get("sort_order") or "").strip()
                try:
                    sort_order = int(sort_order_raw) if sort_order_raw else 0
                except ValueError:
                    sort_order = 0

                if not name:
                    flash("Role name is required.", "danger")
                else:
                    role = ContactRole(name=name, sort_order=sort_order, is_active=True)
                    db.session.add(role)
                    db.session.commit()
                    flash("Role added.", "success")
                    return redirect(url_for("main.settings_lists"))

            elif action == "toggle":
                try:
                    role_id = int(request.form.get("id") or "0")
                except ValueError:
                    role_id = 0
                role = ContactRole.query.get(role_id)
                if role:
                    role.is_active = not bool(getattr(role, "is_active", True))
                    db.session.commit()
                    flash("Role updated.", "success")
                    return redirect(url_for("main.settings_lists"))

        # --------------------
        # BARRIERS
        # --------------------
        elif list_type == "barriers":
            if action == "add":
                label = (request.form.get("label") or "").strip()
                category = (request.form.get("category") or "").strip() or None
                sort_order_raw = (request.form.get("sort_order") or "").strip()
                try:
                    sort_order = int(sort_order_raw) if sort_order_raw else 0
                except ValueError:
                    sort_order = 0

                if not label:
                    flash("Barrier label is required.", "danger")
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
                    return redirect(url_for("main.settings_lists"))

            elif action == "toggle":
                try:
                    bo_id = int(request.form.get("id") or "0")
                except ValueError:
                    bo_id = 0
                bo = BarrierOption.query.get(bo_id)
                if bo:
                    bo.is_active = not bool(getattr(bo, "is_active", True))
                    db.session.commit()
                    flash("Barrier option updated.", "success")
                    return redirect(url_for("main.settings_lists"))

        # --------------------
        # BILLABLE ACTIVITY CODES
        # --------------------
        elif list_type == "billables":
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
                    return redirect(url_for("main.settings_lists"))

            elif action == "toggle":
                try:
                    item_id = int(request.form.get("id") or "0")
                except ValueError:
                    item_id = 0
                item = BillingActivityCode.query.get(item_id)
                if item:
                    item.is_active = not bool(getattr(item, "is_active", True))
                    db.session.commit()
                    flash("Activity code updated.", "success")
                    return redirect(url_for("main.settings_lists"))

    roles = ContactRole.query.order_by(ContactRole.sort_order, ContactRole.name).all()
    barriers = BarrierOption.query.order_by(
        getattr(BarrierOption, "sort_order", 0),
        getattr(BarrierOption, "label", ""),
    ).all()
    billables = BillingActivityCode.query.order_by(
        getattr(BillingActivityCode, "sort_order", 0),
        getattr(BillingActivityCode, "code", ""),
    ).all()

    return render_template(
        "settings/lists.html",
        active_section="lists",
        roles=roles,
        barriers=barriers,
        billables=billables,
    )

# ---------------------------------------------------------------------
# File Storage settings route (placeholder)
# ---------------------------------------------------------------------

@bp.route("/settings/storage", methods=["GET", "POST"])
def settings_storage():
    settings = _ensure_settings()
    error = None

    if request.method == "POST":
        documents_root_raw = (request.form.get("documents_root") or "").strip()

        if not documents_root_raw:
            error = "Documents root folder cannot be empty."
            flash(error, "danger")
        else:
            settings.documents_root = documents_root_raw
            try:
                db.session.commit()
                flash("Storage settings updated.", "success")
                return redirect(url_for("main.settings_storage"))
            except Exception as e:
                db.session.rollback()
                error = f"Could not save storage settings: {e}"
                flash(error, "danger")

    return render_template(
        "settings/storage.html",
        settings=settings,
        active_section="storage",
        error=error,
    )

# ---------------------------------------------------------------------
# AI settings route (placeholder)
# ---------------------------------------------------------------------

@bp.route("/settings/ai", methods=["GET", "POST"])
def settings_ai():
    settings = _ensure_settings()
    error = None

    if request.method == "POST":
        try:
            clarity_enabled = request.form.get("clarity_enabled") == "on"
            clarity_model = (request.form.get("clarity_model") or "").strip()
            clarity_temperature_raw = (request.form.get("clarity_temperature") or "").strip()
            clarity_summary_prompt = (request.form.get("clarity_summary_prompt") or "").strip()
            clarity_plan_prompt = (request.form.get("clarity_plan_prompt") or "").strip()

            # Optional float parsing
            clarity_temperature = None
            if clarity_temperature_raw:
                clarity_temperature = float(clarity_temperature_raw)

            # Only set attributes if they exist (safe against older schema)
            if hasattr(settings, "clarity_enabled"):
                settings.clarity_enabled = clarity_enabled
            if hasattr(settings, "clarity_model"):
                settings.clarity_model = clarity_model
            if hasattr(settings, "clarity_temperature"):
                settings.clarity_temperature = clarity_temperature
            if hasattr(settings, "clarity_summary_prompt"):
                settings.clarity_summary_prompt = clarity_summary_prompt
            if hasattr(settings, "clarity_plan_prompt"):
                settings.clarity_plan_prompt = clarity_plan_prompt

            db.session.commit()
            flash("AI settings saved.", "success")
            return redirect(url_for("main.settings_ai"))

        except Exception as e:
            db.session.rollback()
            error = f"Could not save AI settings: {e}"
            flash(error, "danger")

    return render_template(
        "settings/ai.html",
        settings=settings,
        active_section="ai",
        error=error,
    )

# ---------------------------------------------------------------------
# Email settings route (placeholder)
# ---------------------------------------------------------------------

@bp.route("/settings/email", methods=["GET", "POST"])
def settings_email():
    import smtplib
    from email.message import EmailMessage

    settings = _ensure_settings()
    error = None

    if request.method == "POST":
        action = (request.form.get("action") or "save").strip()

        try:
            # Read form values
            smtp_host = (request.form.get("smtp_host") or "").strip()
            smtp_port_raw = (request.form.get("smtp_port") or "").strip()
            smtp_encryption = (request.form.get("smtp_encryption") or "").strip() or None
            smtp_username = (request.form.get("smtp_username") or "").strip()
            smtp_password = (request.form.get("smtp_password") or "").strip()
            email_from = (request.form.get("email_from") or "").strip()

            smtp_port = int(smtp_port_raw) if smtp_port_raw else None

            # SAVE SETTINGS
            if action == "save":
                if hasattr(settings, "smtp_host"):
                    settings.smtp_host = smtp_host
                if hasattr(settings, "smtp_port"):
                    settings.smtp_port = smtp_port
                if hasattr(settings, "smtp_encryption"):
                    settings.smtp_encryption = smtp_encryption
                if hasattr(settings, "smtp_username"):
                    settings.smtp_username = smtp_username
                if hasattr(settings, "smtp_password"):
                    settings.smtp_password = smtp_password
                if hasattr(settings, "email_from"):
                    settings.email_from = email_from

                # Email template fields
                report_subj = (request.form.get("report_email_subject_template") or "").strip()
                report_body = (request.form.get("report_email_body_template") or "").strip()
                invoice_subj = (request.form.get("invoice_email_subject_template") or "").strip()
                invoice_body = (request.form.get("invoice_email_body_template") or "").strip()
                combo_subj = (request.form.get("report_invoice_email_subject_template") or "").strip()
                combo_body = (request.form.get("report_invoice_email_body_template") or "").strip()

                if hasattr(settings, "report_email_subject_template"):
                    settings.report_email_subject_template = report_subj
                if hasattr(settings, "report_email_body_template"):
                    settings.report_email_body_template = report_body
                if hasattr(settings, "invoice_email_subject_template"):
                    settings.invoice_email_subject_template = invoice_subj
                if hasattr(settings, "invoice_email_body_template"):
                    settings.invoice_email_body_template = invoice_body
                if hasattr(settings, "report_invoice_email_subject_template"):
                    settings.report_invoice_email_subject_template = combo_subj
                if hasattr(settings, "report_invoice_email_body_template"):
                    settings.report_invoice_email_body_template = combo_body

                # Email signature
                signature_raw = (request.form.get("email_signature") or "").strip()
                if hasattr(settings, "email_signature"):
                    settings.email_signature = signature_raw

                db.session.commit()
                flash("Email settings saved.", "success")
                return redirect(url_for("main.settings_email"))

            # TEST EMAIL
            elif action == "test":
                test_to = (request.form.get("test_email_to") or "").strip()
                if not test_to:
                    flash("Please enter a test recipient email address.", "danger")
                elif not smtp_host or not smtp_port:
                    flash("SMTP host and port must be configured before sending a test.", "danger")
                else:
                    msg = EmailMessage()
                    msg["Subject"] = "Impact CMS Test Email"
                    msg["From"] = email_from or smtp_username
                    msg["To"] = test_to
                    msg.set_content("This is a test email from Impact CMS.")

                    if smtp_encryption == "ssl":
                        server = smtplib.SMTP_SSL(smtp_host, smtp_port)
                    else:
                        server = smtplib.SMTP(smtp_host, smtp_port)
                        if smtp_encryption == "tls":
                            server.starttls()

                    if smtp_username:
                        server.login(smtp_username, smtp_password)

                    server.send_message(msg)
                    server.quit()

                    flash("Test email sent successfully.", "success")

        except Exception as e:
            db.session.rollback()
            error = f"Email error: {e}"
            flash(error, "danger")


    return render_template(
        "settings/email.html",
        settings=settings,
        active_section="email",
        error=error,
    )

# ---------------------------------------------------------------------
# UI & Documents settings route
# ---------------------------------------------------------------------

@bp.route("/settings/ui", methods=["GET", "POST"])
def settings_ui():
    settings = _ensure_settings()
    error = None

    if request.method == "POST":
        form = request.form

        accent_color_raw = (form.get("accent_color") or "").strip()
        report_footer_raw = (form.get("report_footer_text") or "").strip()
        invoice_footer_raw = (form.get("invoice_footer_text") or "").strip()

        # Handle logo upload
        logo_file = request.files.get("logo_file")
        logo_path, logo_error = _save_settings_upload(logo_file, "logo")
        if logo_error:
            error = logo_error

        # Handle signature upload
        signature_file = request.files.get("signature_file")
        sig_path, sig_error = _save_settings_upload(signature_file, "signature")
        if sig_error and not error:
            error = sig_error

        if error is None:
            if hasattr(settings, "accent_color"):
                settings.accent_color = accent_color_raw

            if hasattr(settings, "report_footer_text"):
                settings.report_footer_text = report_footer_raw

            if hasattr(settings, "invoice_footer_text"):
                settings.invoice_footer_text = invoice_footer_raw

            if logo_path:
                settings.logo_path = logo_path

            if sig_path:
                settings.signature_path = sig_path

            try:
                db.session.commit()
                flash("UI & Documents updated successfully.", "success")
                return redirect(url_for("main.settings_ui"))
            except Exception as e:
                db.session.rollback()
                error = f"Could not save UI settings: {e}"
                flash(error, "danger")
        else:
            flash(error, "danger")

    return render_template(
        "settings/ui.html",
        settings=settings,
        active_section="ui",
        error=error,
    )

# ---------------------------------------------------------------------
# Rates settings route
# ---------------------------------------------------------------------

@bp.route("/settings/rates", methods=["GET", "POST"])
def settings_rates():
    settings = _ensure_settings()
    error = None

    if request.method == "POST":
        form = request.form

        def parse_float(name):
            raw = (form.get(name) or "").strip()
            if raw == "":
                return None
            try:
                return float(raw)
            except ValueError:
                raise ValueError(f"Invalid numeric value for {name.replace('_', ' ').title()}.")

        try:
            hourly = parse_float("hourly_rate")
            telephonic = parse_float("telephonic_rate")
            mileage = parse_float("mileage_rate")
            initial_hours = parse_float("initial_report_hours")
            progress_hours = parse_float("progress_report_hours")
            closure_hours = parse_float("closure_report_hours")

            min_hours = parse_float("target_min_hours_per_week")
            max_hours = parse_float("target_max_hours_per_week")

            if hourly is not None:
                settings.hourly_rate = hourly
            if telephonic is not None:
                settings.telephonic_rate = telephonic
            if mileage is not None:
                settings.mileage_rate = mileage

            if hasattr(settings, "initial_report_hours") and initial_hours is not None:
                settings.initial_report_hours = initial_hours
            if hasattr(settings, "progress_report_hours") and progress_hours is not None:
                settings.progress_report_hours = progress_hours
            if hasattr(settings, "closure_report_hours") and closure_hours is not None:
                settings.closure_report_hours = closure_hours

            if hasattr(settings, "target_min_hours_per_week") and min_hours is not None:
                settings.target_min_hours_per_week = min_hours
            if hasattr(settings, "target_max_hours_per_week") and max_hours is not None:
                settings.target_max_hours_per_week = max_hours

            db.session.commit()
            flash("Rates updated successfully.", "success")
            return redirect(url_for("main.settings_rates"))

        except ValueError as ve:
            error = str(ve)
            flash(error, "danger")
        except Exception as e:
            db.session.rollback()
            error = f"Could not save rates: {e}"
            flash(error, "danger")

    return render_template(
        "settings/rates.html",
        settings=settings,
        active_section="rates",
        error=error,
    )
