"""Analysis / dashboard routes.

This module intentionally keeps calculations centralized so templates can be
simple and the business logic is not duplicated across pages.

Primary goals (v1):
- Claim counts + stale/dormant claim detection (uses Settings.dormant_claim_days)
- Workload snapshot (billable hours over last 30 days vs target range)
- Accounts receivable snapshot (open invoices) + breakdown by carrier

Future goals (template can be extended without reworking the core queries):
- Uninvoiced billables (WIP) totals + aging buckets
- Unpaid invoices vs open invoices
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from flask import render_template
from flask import current_app
from sqlalchemy import text, func

from .. import db
from ..models import BillableItem, Carrier, Claim, Invoice, Payment, Settings


# ---------------------------------------------------------------------
# Helpers (safe getters)
# ---------------------------------------------------------------------

def _now_utc_naive() -> datetime:
    # App uses naive datetimes in a few places; keep this consistent.
    return datetime.now()


def _as_date(d: Any) -> Optional[date]:
    if d is None:
        return None
    if isinstance(d, date) and not isinstance(d, datetime):
        return d
    if isinstance(d, datetime):
        return d.date()
    # Strings sometimes sneak in from older data
    try:
        return datetime.fromisoformat(str(d)).date()
    except Exception:
        return None


def _as_dt(d: Any) -> Optional[datetime]:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d
    if isinstance(d, date):
        # assume midnight
        return datetime.combine(d, datetime.min.time())
    try:
        return datetime.fromisoformat(str(d))
    except Exception:
        return None


def _safe_get(obj: Any, *names: str, default: Any = None) -> Any:
    for n in names:
        if hasattr(obj, n):
            val = getattr(obj, n)
            if val is not None:
                return val
    return default


def _claim_open_filter():
    """Best-effort filter for open claims.

    Different project phases used different fields; keep this resilient.
    """
    # Prefer explicit boolean/closed_at if present.
    if hasattr(Claim, "is_closed"):
        return Claim.is_closed.is_(False)
    if hasattr(Claim, "closed_at"):
        return Claim.closed_at.is_(None)
    if hasattr(Claim, "status"):
        # Common patterns: 'open'/'closed' or 'active'/'closed'
        return Claim.status.in_(["open", "active", "OPEN", "ACTIVE"])
    # fallback: assume all are open
    return True


def _billable_is_hours(item: BillableItem) -> bool:
    # In this system, most billables represent time in hours.
    # Mileage/expenses may be stored differently; we guard with activity code.
    code = (_safe_get(item, "activity_code", default="") or "").strip().upper()
    return code not in {"MIL", "EXP"}


def _billable_hours(item: BillableItem) -> float:
    # Prefer quantity, then hours, then duration
    qty = _safe_get(item, "quantity", "hours", "duration", default=0.0)
    try:
        return float(qty or 0.0)
    except Exception:
        return 0.0


def _billable_service_date(item: BillableItem) -> Optional[date]:
    return _as_date(_safe_get(item, "service_date", "date", "performed_on", "created_at"))


def _invoice_is_open(inv: Invoice) -> bool:
    # Best-effort: prefer explicit flags, then paid_at/payment_received.
    if hasattr(inv, "is_paid"):
        try:
            return not bool(inv.is_paid)
        except Exception:
            pass
    paid_dt = _safe_get(inv, "paid_at", "paid_date", "payment_received_at", "payment_received_date")
    if paid_dt:
        return False
    status = (_safe_get(inv, "status", default="") or "").strip().lower()
    if status in {"paid", "void", "cancelled", "canceled"}:
        return False
    # default: treat as open
    return True


def _invoice_total_amount(inv: Invoice) -> float:
    """Return the best-available invoice total as a float."""
    val = _safe_get(inv, "total_amount", "total", "amount", "total_due", "total_cents", default=0.0)
    try:
        return float(val or 0.0)
    except Exception:
        return 0.0


def _invoice_paid_amount(inv: Invoice) -> float:
    """Sum Payment.amount for this invoice."""
    inv_id = getattr(inv, "id", None)
    if not inv_id:
        return 0.0
    try:
        paid = (
            db.session.query(func.coalesce(func.sum(Payment.amount), 0))
            .filter(Payment.invoice_id == inv_id)
            .scalar()
        )
        return float(paid or 0.0)
    except Exception:
        return 0.0


def _invoice_outstanding_amount(inv: Invoice) -> float:
    """Invoice outstanding = total - paid, never below zero."""
    total = _invoice_total_amount(inv)
    paid = _invoice_paid_amount(inv)
    try:
        return max(float(total) - float(paid), 0.0)
    except Exception:
        return max(total - paid, 0.0)


# ---------------------------------------------------------------------
# Data objects
# ---------------------------------------------------------------------


@dataclass
class CarrierARRow:
    carrier_name: str
    total_outstanding: float


# ---------------------------------------------------------------------
# Core calculations
# ---------------------------------------------------------------------


def _get_settings() -> Settings:
    settings = Settings.query.first()
    if settings is None:
        settings = Settings(business_name="Impact Medical Consulting")
        db.session.add(settings)
        db.session.commit()
    return settings


def _claim_age_days(claim: Claim, today: date) -> Optional[int]:
    # Prefer referral_date, then created_at, then doi.
    start = _as_date(_safe_get(claim, "referral_date", "created_at", "doi"))
    if not start:
        return None
    return max(0, (today - start).days)


def _claim_last_activity_date(claim: Claim) -> Optional[date]:
    # Prefer last_activity_at, then updated_at, then newest billable date.
    d = _as_date(_safe_get(claim, "last_activity_at", "updated_at"))
    if d:
        return d

    # Try derive from billables (safe, but slightly heavier)
    try:
        q = BillableItem.query.filter(BillableItem.claim_id == claim.id)
        # newest by service_date/created_at
        if hasattr(BillableItem, "service_date"):
            q = q.order_by(BillableItem.service_date.desc().nullslast())
        elif hasattr(BillableItem, "created_at"):
            q = q.order_by(BillableItem.created_at.desc())
        last = q.first()
        if last:
            return _billable_service_date(last)
    except Exception:
        pass
    return None


def _hours_last_n_days(n_days: int, today_dt: datetime) -> float:
    start_dt = today_dt - timedelta(days=n_days)
    start_date = start_dt.date()

    # We intentionally keep this resilient: older/dev schemas have used different
    # column names and sometimes store dates in unexpected ways.
    q = BillableItem.query

    # Best-effort DB-side filtering (fast path)
    date_filtered = False
    if hasattr(BillableItem, "service_date"):
        try:
            q = q.filter(BillableItem.service_date >= start_date)
            date_filtered = True
        except Exception:
            pass
    elif hasattr(BillableItem, "date"):
        try:
            q = q.filter(BillableItem.date >= start_date)
            date_filtered = True
        except Exception:
            pass
    elif hasattr(BillableItem, "performed_on"):
        try:
            q = q.filter(BillableItem.performed_on >= start_date)
            date_filtered = True
        except Exception:
            pass
    elif hasattr(BillableItem, "created_at"):
        try:
            q = q.filter(BillableItem.created_at >= start_dt)
            date_filtered = True
        except Exception:
            pass

    # Only count billables that represent time and are not NO BILL.
    if hasattr(BillableItem, "activity_code"):
        q = q.filter(BillableItem.activity_code.isnot(None))

    items: List[BillableItem] = list(q.all())

    # Safety net: if DB-side date filtering is not available/reliable (or the
    # dataset is small), filter in Python using the same helper we use elsewhere.
    if not date_filtered:
        filtered: List[BillableItem] = []
        for it in items:
            d = _billable_service_date(it)
            if d and d >= start_date:
                filtered.append(it)
            else:
                # fallback to created_at if present
                ca = _as_dt(_safe_get(it, "created_at"))
                if ca and ca >= start_dt:
                    filtered.append(it)
        items = filtered

    total = 0.0
    for it in items:
        code = (_safe_get(it, "activity_code", default="") or "").strip().upper()
        if code == "NO BILL":
            continue
        if not _billable_is_hours(it):
            continue
        total += _billable_hours(it)

    return float(total)


def _open_invoice_rows() -> List[Invoice]:
    invs = list(Invoice.query.all())
    out: List[Invoice] = []
    for inv in invs:
        if not _invoice_is_open(inv):
            continue
        if _invoice_outstanding_amount(inv) <= 0:
            continue
        out.append(inv)
    return out


def _carrier_name_for_claim(claim: Claim) -> str:
    # Prefer relationship; fallback to stored text.
    try:
        carrier = getattr(claim, "carrier", None)
        if carrier and hasattr(carrier, "name"):
            return carrier.name or "(No Carrier)"
    except Exception:
        pass
    return (_safe_get(claim, "carrier_name", default=None) or "(No Carrier)")


def _ar_by_carrier(open_invoices: List[Invoice]) -> List[CarrierARRow]:
    buckets: Dict[str, float] = {}

    for inv in open_invoices:
        # Prefer relationship chain: invoice -> claim -> carrier
        carrier_name = None
        try:
            claim = getattr(inv, "claim", None)
            if claim is not None:
                carrier_name = _carrier_name_for_claim(claim)
        except Exception:
            carrier_name = None

        if not carrier_name:
            # Some schemas store carrier_id on invoice
            try:
                carrier_id = getattr(inv, "carrier_id", None)
                if carrier_id:
                    c = Carrier.query.get(carrier_id)
                    if c and hasattr(c, "name"):
                        carrier_name = c.name
            except Exception:
                carrier_name = None

        carrier_name = carrier_name or "(Unknown Carrier)"

        amt = _invoice_outstanding_amount(inv)
        if amt <= 0:
            continue
        buckets[carrier_name] = buckets.get(carrier_name, 0.0) + amt

    rows = [CarrierARRow(carrier_name=k, total_outstanding=v) for k, v in buckets.items()]
    rows.sort(key=lambda r: r.total_outstanding, reverse=True)
    return rows


# ---------------------------------------------------------------------
# Error capture helpers
# ---------------------------------------------------------------------

def _try(label: str, fn, default):
    """Run fn() and capture exceptions for display/logging."""
    try:
        return fn(), None
    except Exception as e:
        # Log full stack trace to server logs, but keep UI message short.
        try:
            current_app.logger.exception("Analysis error in %s", label)
        except Exception:
            pass
        return default, f"{label}: {type(e).__name__}: {e}"


def _table_count_sql(table_name: str) -> int:
    """Count rows via SQL as a fallback diagnostic.

    We quote the identifier to reduce failures from reserved words/plurals.
    `table_name` must come from trusted sources (model metadata), not user input.
    """
    try:
        if not table_name:
            return 0
        res = db.session.execute(text(f'SELECT COUNT(*) FROM "{table_name}"'))
        return int(res.scalar() or 0)
    except Exception:
        return 0


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------


def analysis_index():
    settings = _get_settings()

    analysis_errors: List[str] = []

    today_dt = _now_utc_naive()
    today = today_dt.date()

    # ---- Claims snapshot ----
    total_claims, err = _try("Claim.query.count", lambda: int(Claim.query.count()), 0)
    if err:
        analysis_errors.append(err)

    open_claims, err = _try(
        "Claim.query.filter(open).all",
        lambda: list(Claim.query.filter(_claim_open_filter()).all()),
        [],
    )
    if err:
        analysis_errors.append(err)

    active_claims_count = len(open_claims)

    # Average open claim age
    ages = [a for a in (_claim_age_days(c, today) for c in open_claims) if a is not None]
    avg_open_claim_age_days: Optional[int]
    if ages:
        avg_open_claim_age_days = int(round(sum(ages) / len(ages)))
    else:
        avg_open_claim_age_days = None

    # Stale/dormant claims
    dormant_days = int(getattr(settings, "dormant_claim_days", 60) or 60)
    stale_cutoff = today - timedelta(days=dormant_days)

    stale_claims_count = 0
    for c in open_claims:
        last_activity = _claim_last_activity_date(c)
        if last_activity is None:
            # If we truly don't know, treat as stale only if claim itself is old.
            age = _claim_age_days(c, today)
            if age is not None and age >= dormant_days:
                stale_claims_count += 1
        else:
            if last_activity <= stale_cutoff:
                stale_claims_count += 1

    # ---- Workload snapshot ----
    hours_last_30_days = _hours_last_n_days(30, today_dt)

    # Targets are stored weekly; convert to 30-day equivalents.
    target_min_week = float(getattr(settings, "target_min_hours_per_week", 0.0) or 0.0)
    target_max_week = float(getattr(settings, "target_max_hours_per_week", 0.0) or 0.0)

    # 30 days ≈ 30/7 weeks
    factor = 30.0 / 7.0
    hours_target_min_30 = round(target_min_week * factor, 1) if target_min_week else 0.0
    hours_target_max_30 = round(target_max_week * factor, 1) if target_max_week else 0.0

    # ---- Accounts receivable (invoice-based v1) ----
    open_invoice_list = _open_invoice_rows()
    open_invoices = len(open_invoice_list)

    total_invoices, err = _try("Invoice.query.count", lambda: int(Invoice.query.count()), 0)
    if err:
        analysis_errors.append(err)

    total_outstanding_ar = round(sum(_invoice_outstanding_amount(i) for i in open_invoice_list), 2)
    ar_by_carrier = _ar_by_carrier(open_invoice_list)

    # ---- Uninvoiced billables (WIP) placeholders (backend-ready) ----
    # Template may not show these yet, but keep them available.
    uninvoiced_billables_total = 0.0
    uninvoiced_billables_count = 0

    def _compute_uninvoiced():
        q = BillableItem.query
        # unpaid/uninvoiced is typically invoice_id is null
        if hasattr(BillableItem, "invoice_id"):
            q = q.filter(BillableItem.invoice_id.is_(None))
        # completed flag if present
        if hasattr(BillableItem, "is_complete"):
            q = q.filter(BillableItem.is_complete.is_(True))
        # skip NO BILL
        if hasattr(BillableItem, "activity_code"):
            q = q.filter(BillableItem.activity_code != "NO BILL")

        items = list(q.all())
        count = len(items)

        # NOTE: Many systems compute money at invoice time. Until BillableItem stores
        # rate/amount-at-entry, we expose counts + hours as safe.
        hours = round(sum(_billable_hours(i) for i in items if _billable_is_hours(i)), 2)
        return count, hours

    (uninvoiced_billables_count, uninvoiced_billables_total), err = _try(
        "BillableItem uninvoiced query",
        _compute_uninvoiced,
        (0, 0.0),
    )
    if err:
        analysis_errors.append(err)

    # ---- A/R aging (invoice-based, simple buckets) ----
    # The template expects:
    #   current  -> 0–30 days
    #   days_31_60, days_61_90, days_90_plus
    ar_aging = {
        # Preferred keys
        "current": 0.0,      # 0–30 days
        "days_31_60": 0.0,
        "days_61_90": 0.0,
        "days_90_plus": 0.0,

        # Back-compat / alias keys (some templates/older code used these)
        "days_1_30": 0.0,
        "over_90": 0.0,
    }

    for inv in open_invoice_list:
        amount = _invoice_outstanding_amount(inv)
        if amount <= 0:
            continue

        # Prefer explicit invoice date; fallback to created/issued timestamps.
        inv_date = _as_date(_safe_get(inv, "invoice_date", "created_at", "issued_at"))
        if not inv_date:
            # If we don't know, treat as current so the dollars don't disappear.
            ar_aging["current"] += amount
            ar_aging["days_1_30"] += amount
            continue

        age_days = (today - inv_date).days

        # Bucket according to what the UI labels show.
        # current = 0–30 days
        if age_days <= 30:
            ar_aging["current"] += amount
            ar_aging["days_1_30"] += amount
        elif age_days <= 60:
            ar_aging["days_31_60"] += amount
        elif age_days <= 90:
            ar_aging["days_61_90"] += amount
        else:
            ar_aging["days_90_plus"] += amount
            ar_aging["over_90"] += amount

    # ---- Diagnostics (helps when values are unexpectedly all zeros) ----
    diagnostics = {
        "db_uri_set": bool(current_app.config.get("SQLALCHEMY_DATABASE_URI")),
        "claim_table": getattr(getattr(Claim, "__table__", None), "name", None) or getattr(Claim, "__tablename__", "claims"),
        "billable_table": getattr(getattr(BillableItem, "__table__", None), "name", None) or getattr(BillableItem, "__tablename__", "billable_items"),
        "invoice_table": getattr(getattr(Invoice, "__table__", None), "name", None) or getattr(Invoice, "__tablename__", "invoices"),
    }

    diagnostics.update(
        {
            "claims_table_count_sql": _table_count_sql(diagnostics["claim_table"]),
            "billables_table_count_sql": _table_count_sql(diagnostics["billable_table"]),
            "invoices_table_count_sql": _table_count_sql(diagnostics["invoice_table"]),
        }
    )

    return render_template(
        "analysis.html",
        active_page="analysis",
        settings=settings,
        # claims
        active_claims_count=active_claims_count,
        total_claims=total_claims,
        avg_open_claim_age_days=avg_open_claim_age_days,
        stale_claims_count=stale_claims_count,
        # workload
        hours_last_30_days=round(hours_last_30_days, 1),
        hours_target_min_30=hours_target_min_30,
        hours_target_max_30=hours_target_max_30,
        # A/R
        total_outstanding_ar=total_outstanding_ar,
        open_invoices=open_invoices,
        total_invoices=total_invoices,
        ar_by_carrier=ar_by_carrier,
        ar_aging=ar_aging,
        # WIP placeholders
        uninvoiced_billables_count=uninvoiced_billables_count,
        uninvoiced_billable_hours=uninvoiced_billables_total,
        analysis_errors=analysis_errors,
        diagnostics=diagnostics,
    )