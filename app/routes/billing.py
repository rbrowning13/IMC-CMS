"""Billing / billables routes.

This module intentionally focuses on BillableItem CRUD and related helpers.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import inspect

from flask import current_app, flash, redirect, render_template, request, url_for

from app import db
from app.models import BillableItem, BillingActivityCode, Claim, Invoice, Payment, Settings

from . import bp
try:
    # Prefer the shared canonical helpers when available.
    from .helpers import BILLABLE_ACTIVITY_CHOICES, _billable_is_complete  # type: ignore
except Exception:  # pragma: no cover
    # Fallbacks so the app can boot while helpers are being refactored.
    BILLABLE_ACTIVITY_CHOICES = [
        ("Admin", "Admin"),
        ("Email", "Email"),
        ("Exp", "Exp"),
        ("Fax", "Fax"),
        ("FR", "FR"),
        ("GDL", "GDL"),
        ("LTR", "LTR"),
        ("MR", "MR"),
        ("MTG", "MTG"),
        ("MIL", "MIL"),
        ("REP", "REP"),
        ("RR", "RR"),
        ("TC", "TC"),
        ("TCM", "TCM"),
        ("Text", "Text"),
        ("Travel", "Travel"),
        ("Wait", "Wait"),
        ("NO BILL", "NO BILL"),
    ]

    def _billable_is_complete(activity_code, service_date, quantity):
        """Best-effort completeness rules.

        Mirrors the intent of the legacy app:
        - "NO BILL": requires either a service date or a quantity.
        - All other codes: require both service date and quantity.
        """
        code = (activity_code or "").strip().upper()
        if code == "NO BILL":
            return bool(service_date) or (quantity is not None)
        return bool(service_date) and (quantity is not None)


# --- Invoice math helpers ---

def _safe_float(value) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _get_invoice_math(inv: Invoice, settings: Settings | None = None) -> dict:
    """Return canonical invoice math for UI display.

    All invoice totals shown in Billing + Payment pages MUST come from the same
    canonical calculation used by invoice_detail / invoice_print.

    IMPORTANT:
    - Carrier rate overrides must be respected when present.
    - Settings rates are only a fallback.

    We call the canonical helper using KEYWORD arguments first so we work with
    both positional-arg and keyword-only helper signatures.

    The wrapper normalizes the helper output into three floats used across UI:
      - invoice_total
      - amount_paid
      - balance_due
    """

    # Preferred path: canonical helper
    try:
        compute_invoice_financials = None

        # Preferred import: app.helpers (some refactors moved helpers out of routes)
        try:
            from app.helpers import compute_invoice_financials as _cif  # type: ignore
            compute_invoice_financials = _cif
        except Exception:
            compute_invoice_financials = None

        # Fallback: routes.helpers (historical location)
        if compute_invoice_financials is None:
            try:
                from .helpers import compute_invoice_financials as _cif  # type: ignore
                compute_invoice_financials = _cif
            except Exception:
                compute_invoice_financials = None

        if callable(compute_invoice_financials):
            # First try keyword-only call (works even if helper defines only keyword params)
            try:
                data = compute_invoice_financials(invoice=inv, settings=settings)
            except TypeError:
                # Then try positional (older helper versions)
                try:
                    data = compute_invoice_financials(inv, settings)
                except TypeError:
                    data = compute_invoice_financials(inv)

            if isinstance(data, dict):
                invoice_total = _safe_float(data.get("invoice_total"))
                amount_paid = _safe_float(data.get("amount_paid"))

                balance_due = data.get("balance_due")
                if balance_due is None:
                    balance_due = invoice_total - amount_paid
                balance_due = _safe_float(balance_due)

                return {
                    "invoice_total": invoice_total,
                    "amount_paid": amount_paid,
                    "balance_due": balance_due,
                    "_source": "canonical",
                }

        # If we got here, we couldn't find a canonical helper at all.
        current_app.logger.warning(
            "No callable compute_invoice_financials helper found; falling back to stored totals for invoice_id=%s",
            getattr(inv, "id", None),
        )

    except Exception as e:
        # Fall through to a conservative fallback, but log the root cause.
        current_app.logger.exception(
            "compute_invoice_financials failed; falling back to stored totals for invoice_id=%s: %s",
            getattr(inv, "id", None),
            e,
        )

    # Fallback (should be rare): use stored invoice total + sum of Payment rows.
    invoice_total = _safe_float(getattr(inv, "total_amount", 0) or 0)
    try:
        paid = (
            db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0.0))
            .filter(Payment.invoice_id == inv.id)
            .scalar()
        )
        amount_paid = _safe_float(paid)
    except Exception:
        amount_paid = _safe_float(getattr(inv, "amount_paid", 0) or 0)

    balance_due = invoice_total - amount_paid
    return {
        "invoice_total": invoice_total,
        "amount_paid": amount_paid,
        "balance_due": _safe_float(balance_due),
        "_source": "fallback",
    }


@bp.route("/billing")
def billing_list():
    """List invoices (Billing page) with canonical A/R math + buckets."""

    settings = Settings.query.first()
    invoices = Invoice.query.order_by(Invoice.id.desc()).all()

    # payment_terms_default is now numeric days (stored as string or int depending on data)
    def _terms_days(s: Settings | None) -> int:
        if not s:
            return 30
        raw = getattr(s, "payment_terms_default", None)
        if raw is None:
            return 30
        try:
            return int(str(raw).strip())
        except Exception:
            return 30

    terms_days = _terms_days(settings)
    today = date.today()

    invoice_math_by_id: dict[int, dict] = {}

    # Buckets
    draft: list[Invoice] = []
    sent_current: list[Invoice] = []
    sent_past_due: list[Invoice] = []
    paid: list[Invoice] = []
    void: list[Invoice] = []

    # Metrics
    outstanding_total = 0.0
    past_due_total = 0.0
    sent_total = 0.0
    paid_total = 0.0

    for inv in invoices:
        math = _get_invoice_math(inv, settings)
        invoice_math_by_id[inv.id] = math

        total = float(math.get("invoice_total", 0.0) or 0.0)
        balance = float(math.get("balance_due", 0.0) or 0.0)
        amt_paid = float(math.get("amount_paid", 0.0) or 0.0)

        status = (getattr(inv, "status", None) or "Draft").strip()

        # Treat "Paid" as true if balance is zero, even if status is stale.
        is_paid_by_math = (balance <= 0.00001) and (total > 0)

        # Normalize invoice_date to a date to avoid datetime vs date comparisons.
        inv_date = getattr(inv, "invoice_date", None)
        if inv_date and isinstance(inv_date, datetime):
            inv_date_date = inv_date.date()
        else:
            inv_date_date = inv_date

        due_date = (inv_date_date + timedelta(days=terms_days)) if inv_date_date else None

        # Past due = Sent/unpaid AND due_date passed
        is_past_due = (
            status == "Sent"
            and not is_paid_by_math
            and due_date is not None
            and due_date < today
        )

        # Metrics (ignore void)
        if status != "Void":
            # Outstanding should reflect current balance only
            if balance > 0:
                outstanding_total += balance

            # Totals:
            # - sent_total is "total billed" (all non-void invoices)
            # - paid_total is sum of payments applied
            sent_total += total
            paid_total += amt_paid

            if is_past_due and balance > 0:
                past_due_total += balance

        # Bucketing
        if status == "Void":
            void.append(inv)
        elif status == "Draft":
            draft.append(inv)
        elif is_paid_by_math or status == "Paid":
            paid.append(inv)
        else:
            # Sent-but-unpaid
            if is_past_due:
                sent_past_due.append(inv)
            else:
                sent_current.append(inv)

    return render_template(
        "billing_list.html",
        active_page="billing",
        invoices=invoices,
        invoice_math_by_id=invoice_math_by_id,
        terms_days=terms_days,
        outstanding_total=outstanding_total,
        past_due_total=past_due_total,
        sent_total=sent_total,
        paid_total=paid_total,
        bucket_draft=draft,
        bucket_sent_current=sent_current,
        bucket_sent_past_due=sent_past_due,
        bucket_paid=paid,
        bucket_void=void,
    )


# ---------------------------
# Payments
# ---------------------------


def _parse_mmddyyyy(value: str):
    value = (value or "").strip()
    if not value:
        return None
    # Accept ISO too, just in case.
    try:
        if "-" in value:
            return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        pass
    try:
        return datetime.strptime(value, "%m/%d/%Y").date()
    except ValueError:
        return None



@bp.route("/billing/<int:invoice_id>/payment/<int:payment_id>/edit", methods=["GET"])
def payment_edit(invoice_id: int, payment_id: int):
    """Compatibility route: redirect legacy edit URLs to the unified payment form."""
    return_to = (request.args.get("return_to") or "").strip()
    return redirect(
        url_for(
            "main.payment_new",
            invoice_id=invoice_id,
            payment_id=payment_id,
            return_to=return_to,
        )
    )

@bp.route("/billing/<int:invoice_id>/payment/new", methods=["GET"])
def payment_new(invoice_id: int):
    """Render the Record Payment form (create or edit)."""

    inv = Invoice.query.get_or_404(invoice_id)
    settings = Settings.query.first()

    math = _get_invoice_math(inv, settings)

    payment = None
    payment_id_raw = (request.args.get("payment_id") or "").strip()
    if payment_id_raw:
        try:
            payment_id = int(payment_id_raw)
        except ValueError:
            payment_id = None

        if payment_id:
            payment = Payment.query.get_or_404(payment_id)
            if payment.invoice_id != inv.id:
                flash("That payment does not belong to this invoice.", "warning")
                return redirect(url_for("main.invoice_detail", invoice_id=inv.id))

    return_to = (request.args.get("return_to") or "").strip()
    if not return_to:
        return_to = url_for("main.invoice_detail", invoice_id=inv.id)

    return render_template(
        "payment_new.html",
        active_page="billing",
        invoice=inv,
        payment=payment,
        # Canonical totals for this invoice (must respect carrier-rate overrides when present)
        invoice_math=math,
        # Back-compat fields (templates may still reference these)
        invoice_total=math["invoice_total"],
        amount_paid=math["amount_paid"],
        balance_due=math["balance_due"],
        return_to=return_to,
    )


# New payment_create endpoint to handle payment creation (POST)
@bp.route("/billing/payment/create", methods=["POST"])
def payment_create():
    """Create a new payment for an invoice."""

    invoice_id_raw = (request.form.get("invoice_id") or "").strip()
    if not invoice_id_raw:
        flash("Missing invoice id for payment.", "danger")
        return redirect(url_for("main.billing_list"))

    try:
        invoice_id = int(invoice_id_raw)
    except ValueError:
        flash("Invalid invoice id for payment.", "danger")
        return redirect(url_for("main.billing_list"))

    inv = Invoice.query.get_or_404(invoice_id)

    payment_id_raw = (request.form.get("payment_id") or "").strip()
    payment = None

    if payment_id_raw:
        try:
            payment_id = int(payment_id_raw)
        except ValueError:
            payment_id = None

        if payment_id:
            payment = Payment.query.get_or_404(payment_id)
            if payment.invoice_id != inv.id:
                flash("That payment does not belong to this invoice.", "warning")
                return redirect(url_for("main.invoice_detail", invoice_id=inv.id))

    return_to = (request.form.get("return_to") or "").strip()
    if not return_to:
        return_to = url_for("main.invoice_detail", invoice_id=inv.id)

    amount_raw = (request.form.get("amount") or request.form.get("payment_amount") or "").strip()
    method = (request.form.get("method") or request.form.get("payment_method") or "").strip() or None
    reference = (request.form.get("reference") or request.form.get("payment_reference") or "").strip() or None
    notes = (request.form.get("notes") or request.form.get("payment_notes") or "").strip() or None
    paid_date = _parse_mmddyyyy(
        request.form.get("paid_date")
        or request.form.get("payment_date")
        or request.form.get("date")
        or request.form.get("payment_paid_date")
        or ""
    )

    if paid_date is None:
        paid_date = datetime.utcnow().date()

    if not amount_raw:
        flash("Payment amount is required.", "danger")
        return redirect(
            url_for(
                "main.payment_new",
                invoice_id=inv.id,
                payment_id=(payment.id if payment else None),
                return_to=return_to,
            )
        )

    try:
        amount = float(amount_raw)
    except ValueError:
        flash("Payment amount must be a number.", "danger")
        return redirect(
            url_for(
                "main.payment_new",
                invoice_id=inv.id,
                payment_id=(payment.id if payment else None),
                return_to=return_to,
            )
        )

    # Create or update the Payment row.
    p = payment or Payment()

    # Required linkage (for new payments)
    if payment is None:
        if hasattr(p, "invoice_id"):
            p.invoice_id = inv.id
        elif hasattr(p, "invoice"):
            p.invoice = inv

    # Fields
    if hasattr(p, "amount"):
        p.amount = amount

    # Date field naming has varied across iterations (paid_date vs payment_date vs date).
    # Set whichever exists so edits both load and persist correctly.
    if hasattr(p, "paid_date"):
        p.paid_date = paid_date
    if hasattr(p, "payment_date"):
        p.payment_date = paid_date
    if hasattr(p, "date"):
        p.date = paid_date

    if hasattr(p, "method"):
        p.method = method
    if hasattr(p, "payment_method"):
        p.payment_method = method

    if hasattr(p, "reference"):
        p.reference = reference
    if hasattr(p, "payment_reference"):
        p.payment_reference = reference

    if hasattr(p, "notes"):
        p.notes = notes
    if hasattr(p, "payment_notes"):
        p.payment_notes = notes

    if payment is None:
        db.session.add(p)

    db.session.commit()
    flash("Payment updated." if payment else "Payment recorded.", "success")
    return redirect(return_to)


@bp.route("/billing/payment/<int:payment_id>/delete", methods=["POST"])
def payment_delete(payment_id: int):
    """Delete a payment."""

    payment = Payment.query.get_or_404(payment_id)
    invoice_id = getattr(payment, "invoice_id", None)

    return_to = (request.args.get("return_to") or request.form.get("return_to") or "").strip()
    if not return_to:
        if invoice_id:
            return_to = url_for("main.invoice_detail", invoice_id=invoice_id)
        else:
            return_to = url_for("main.billing_list")

    db.session.delete(payment)
    db.session.commit()
    flash("Payment deleted.", "success")
    return redirect(return_to)


@bp.route("/claims/<int:claim_id>/billable/<int:item_id>/edit", methods=["GET", "POST"])
def billable_edit(claim_id, item_id):
    """Edit a billable item for a claim."""

    claim = Claim.query.get_or_404(claim_id)
    item = BillableItem.query.filter_by(id=item_id, claim_id=claim.id).first_or_404()

    # Build billable activity choices from the BillingActivityCode table.
    db_codes = (
        BillingActivityCode.query.filter_by(is_active=True)
        .order_by(BillingActivityCode.sort_order, BillingActivityCode.code)
        .all()
    )
    if db_codes:
        billable_activity_choices = [(c.code, c.label or c.code) for c in db_codes]
    else:
        billable_activity_choices = BILLABLE_ACTIVITY_CHOICES

    def _parse_billable_date(value: str):
        value = (value or "").strip()
        if not value:
            return None
        # Try ISO first
        try:
            if "-" in value:
                return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            pass
        # Then MM/DD/YYYY
        try:
            return datetime.strptime(value, "%m/%d/%Y").date()
        except ValueError:
            return None

    error = None

    if request.method == "POST":
        raw_date_value = (
            (request.form.get("service_date") or "").strip()
            or (request.form.get("date") or "").strip()
            or (request.form.get("date_of_service") or "").strip()
        )
        service_date_parsed = _parse_billable_date(raw_date_value)

        activity_code = (request.form.get("activity_code") or "").strip()
        qty_raw = (request.form.get("quantity") or "").strip()
        quantity = float(qty_raw) if qty_raw else None

        raw_description = (request.form.get("description") or "").strip()
        description = raw_description if raw_description else None

        notes_raw = (request.form.get("notes") or "").strip()
        notes = notes_raw if notes_raw else None

        # If description is still empty, fall back to the human label for this activity code.
        if not description and activity_code:
            label = None
            for code, label_text in billable_activity_choices:
                if code == activity_code:
                    label = label_text
                    break
            description = label or activity_code

        if not activity_code:
            error = "Activity code is required."
        else:
            is_complete = _billable_is_complete(activity_code, service_date_parsed, quantity)

            item.activity_code = activity_code
            item.date_of_service = service_date_parsed
            item.quantity = quantity
            item.description = description
            item.notes = notes
            item.is_complete = is_complete

            db.session.commit()
            return redirect(url_for("main.claim_detail", claim_id=claim.id))

    return render_template(
        "billable_edit.html",
        active_page="claims",
        claim=claim,
        item=item,
        billable_activity_choices=billable_activity_choices,
        error=error,
    )


@bp.route("/claims/<int:claim_id>/billable/<int:item_id>/delete", methods=["GET", "POST"])
def billable_delete(claim_id, item_id):
    """Delete a billable item from a claim.

    If the item is already invoiced, we block deletion to avoid data loss.
    """

    claim = Claim.query.get_or_404(claim_id)
    item = BillableItem.query.filter_by(id=item_id, claim_id=claim.id).first_or_404()

    # If your model uses a different relationship/field, adjust this check.
    if getattr(item, "invoice_id", None):
        flash("That billable is already linked to an invoice and cannot be deleted.", "warning")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    if request.method == "POST":
        db.session.delete(item)
        db.session.commit()
        flash("Billable deleted.", "success")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    return render_template(
        "confirm_delete.html",
        active_page="claims",
        title="Delete billable",
        message="Are you sure you want to delete this billable item?",
        cancel_url=url_for("main.claim_detail", claim_id=claim.id),
        confirm_url=url_for("main.billable_delete", claim_id=claim.id, item_id=item.id),
    )