"""Settings and configuration routes.

This module was split out of the old monolithic routes.py.

Notes during transition:
- Some helpers may be duplicated temporarily (e.g., settings loader)
  until we consolidate them into app/routes/helpers.py.
"""

from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for

from ..extensions import db
from ..models import BarrierOption, BillingActivityCode, Settings

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


@bp.route("/settings", methods=["GET", "POST"])
def settings_view():
    """View/edit app Settings."""
    settings = _ensure_settings()
    error = None

    if request.method == "POST":
        business_name = (request.form.get("business_name") or "").strip() or None
        state = (request.form.get("state") or "").strip() or None

        def _parse_float_field(field_name: str, label: str):
            raw = (request.form.get(field_name) or "").strip()
            if raw == "":
                return None
            try:
                return float(raw)
            except ValueError:
                return f"{label} must be a number.", None

        def _parse_int_field(field_name: str, label: str):
            raw = (request.form.get(field_name) or "").strip()
            if raw == "":
                return None
            try:
                return int(raw)
            except ValueError:
                return f"{label} must be a whole number.", None

        # Parse numeric fields with validation
        hourly_rate = None
        telephonic_rate = None
        mileage_rate = None
        dormant_claim_days = None

        if error is None:
            parsed = _parse_float_field("hourly_rate", "Hourly rate")
            if isinstance(parsed, tuple):
                error, hourly_rate = parsed
            else:
                hourly_rate = parsed

        if error is None:
            parsed = _parse_float_field("telephonic_rate", "Telephonic rate")
            if isinstance(parsed, tuple):
                error, telephonic_rate = parsed
            else:
                telephonic_rate = parsed

        if error is None:
            parsed = _parse_float_field("mileage_rate", "Mileage rate")
            if isinstance(parsed, tuple):
                error, mileage_rate = parsed
            else:
                mileage_rate = parsed

        if error is None:
            parsed = _parse_int_field("dormant_claim_days", "Dormant claim days")
            if isinstance(parsed, tuple):
                error, dormant_claim_days = parsed
            else:
                dormant_claim_days = parsed

        report_footer_text = (request.form.get("report_footer_text") or "").strip() or None

        # Apply
        if business_name:
            settings.business_name = business_name
        if state:
            settings.state = state

        if hourly_rate is not None:
            settings.hourly_rate = hourly_rate
        if telephonic_rate is not None:
            settings.telephonic_rate = telephonic_rate
        if mileage_rate is not None:
            settings.mileage_rate = mileage_rate

        if dormant_claim_days is not None:
            settings.dormant_claim_days = dormant_claim_days

        settings.report_footer_text = report_footer_text

        if error is None:
            db.session.commit()
            flash("Settings saved.", "success")
            return redirect(url_for("main.settings_view"))

        # Validation error: re-render the form with the entered values and message
        return render_template(
            "settings.html",
            active_page="settings",
            settings=settings,
            error=error,
        )

    return render_template(
        "settings.html",
        active_page="settings",
        settings=settings,
        error=error,
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
        code = (request.form.get("code") or "").strip()
        label = (request.form.get("label") or "").strip() or None
        rate, rate_err = _parse_float(request.form.get("rate"), "Rate")
        sort_order, sort_err = _parse_int(request.form.get("sort_order"), "Sort order")

        if not code:
            error = "Code is required."
        elif rate_err:
            error = rate_err
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
                    rate=rate,
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
        new_code = (request.form.get("code") or "").strip()
        label = (request.form.get("label") or "").strip() or None
        rate, rate_err = _parse_float(request.form.get("rate"), "Rate")
        sort_order, sort_err = _parse_int(request.form.get("sort_order"), "Sort order")
        is_active_raw = (request.form.get("is_active") or "").strip().lower()

        if not new_code:
            error = "Code is required."
        elif rate_err:
            error = rate_err
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
            if rate is not None:
                code_obj.rate = rate
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