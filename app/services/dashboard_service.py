

"""Dashboard aggregation utilities.

This module intentionally keeps dashboard calculations out of routes/templates.

Design goals:
- Safe defaults: tolerate schema drift (missing optional columns) without crashing.
- Keep computations reasonably efficient (SQL aggregation where possible).
- Return simple dicts/lists ready for Jinja templates or JSON.

NOTE: This service does not render HTML. Routes/templates decide presentation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, case, func, or_, literal
from sqlalchemy import Numeric, cast
import statistics

# For timezone conversion
from app.models import to_system_timezone


# -----------------------------------------------------------------------------
#  Period handling
# -----------------------------------------------------------------------------

def get_period_bounds(period: str, *, today: Optional[date] = None) -> Tuple[date, date]:
    """Return (start_date, end_date_inclusive) for supported dashboard periods.

    period: one of 'WEEK', 'MONTH', '3M', 'YEAR', etc. Unknown values default to MONTH (30D rolling).
    """
    today = today or date.today()
    p = (period or "MONTH").strip().upper()

    if p in ("YEAR", "YTD"):
        start = date(today.year, 1, 1)
        end = today
    elif p in ("12M", "ROLLING_YEAR"):
        start = today - timedelta(days=364)
        end = today
    elif p in ("6M", "180D"):
        start = today - timedelta(days=179)
        end = today
    elif p in ("3M", "90D"):
        start = today - timedelta(days=89)
        end = today
    elif p in ("WEEK", "7D"):
        start = today - timedelta(days=6)
        end = today
    else:  # MONTH default (30D rolling)
        start = today - timedelta(days=29)
        end = today

    return start, end


# -----------------------------------------------------------------------------
#  Safe model attribute helpers
# -----------------------------------------------------------------------------

def _has_col(model_cls: Any, col_name: str) -> bool:
    return hasattr(model_cls, col_name)


def _first_attr(obj: Any, names: Iterable[str], default: Any = None) -> Any:
    for n in names:
        if hasattr(obj, n):
            v = getattr(obj, n)
            if v is not None:
                return v
    return default


def _billable_service_date_attr(BillableItem: Any):
    """Best-effort locate the service date attribute on BillableItem."""
    for name in ("date_of_service", "service_date", "date"):
        if hasattr(BillableItem, name):
            return getattr(BillableItem, name)
    return None


def _report_due_attr(Report: Any):
    for name in ("next_report_due", "next_due_date", "report_due_date"):
        if hasattr(Report, name):
            return getattr(Report, name)
    return None


def _report_type_attr(Report: Any):
    for name in ("report_type", "type"):
        if hasattr(Report, name):
            return getattr(Report, name)
    return None


def _report_display_number(report_obj: Any) -> Optional[str]:
    """Return the 'human' report number used in Claim Detail if present.

    We can't assume a single schema across installs, so try common names.
    """
    v = _first_attr(report_obj, ("report_number", "display_number", "sequence_number", "seq", "number"), None)
    if v is None:
        return None
    return str(v)


def _invoice_status_attr(Invoice: Any):
    return getattr(Invoice, "status", None)


def _invoice_date_attr(Invoice: Any):
    for name in ("invoice_date", "sent_date", "date_sent"):
        if hasattr(Invoice, name):
            return getattr(Invoice, name)
    return None


def _invoice_total_attr(Invoice: Any):
    for name in ("total_amount", "amount_total", "total"):
        if hasattr(Invoice, name):
            return getattr(Invoice, name)
    return None


def _billable_amount_expr(BillableItem: Any):
    """Best-effort SQL expression for a billable's monetary value.

    Preference:
    - explicit amount/total field if present
    - else quantity * rate if present
    - else 0
    """
    if hasattr(BillableItem, "amount"):
        return func.coalesce(getattr(BillableItem, "amount"), 0)
    if hasattr(BillableItem, "total"):
        return func.coalesce(getattr(BillableItem, "total"), 0)
    if hasattr(BillableItem, "line_total"):
        return func.coalesce(getattr(BillableItem, "line_total"), 0)
    # Fall back to quantity * rate (common pattern)
    qty = getattr(BillableItem, "quantity", None)
    rate = getattr(BillableItem, "rate", None)
    if qty is not None and rate is not None:
        return func.coalesce(qty, 0) * func.coalesce(rate, 0)
    from sqlalchemy import Numeric, cast
    return cast(0, Numeric)


def _claim_last_name_expr(Claim: Any):
    """Best-effort last-name column for Claim."""
    for name in ("claimant_last_name", "last_name", "patient_last_name"):
        if hasattr(Claim, name):
            return getattr(Claim, name)
    return None


def _claim_number_expr(Claim: Any):
    for name in ("claim_number", "number", "claim_no"):
        if hasattr(Claim, name):
            return getattr(Claim, name)
    return None


# -----------------------------------------------------------------------------
#  Core dashboard queries
# -----------------------------------------------------------------------------

@dataclass
class DueReportRow:
    report_id: int
    claim_id: int
    last_name: str
    first_name: str
    claim_number: str
    report_type: str
    report_number: str
    due_date: date


@dataclass
class AppointmentRow:
    report_id: int
    claim_id: int
    last_name: str
    claim_number: str
    provider_name: str
    appt_dt: datetime
    notes: str


def get_reports_due(*, days_ahead: int = 7, period: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
    from app.models import Claim, Report, db

    today = date.today()
    end = today + timedelta(days=days_ahead)
    # If period provided, override today/end with period bounds
    if period is not None:
        start_period, end_period = get_period_bounds(period)
        today = start_period
        end = end_period

    if not hasattr(Claim, "next_report_due"):
        return {"overdue": [], "next_7": []}

    due_col = Claim.next_report_due

    query_columns = [
        Claim.id.label("claim_id"),
        Claim.claimant_last_name.label("last_name"),
        Claim.claimant_first_name.label("first_name"),
        Claim.claim_number.label("claim_number"),
        due_col.label("due_date"),
    ]

    q = (
        db.session.query(*query_columns)
        .filter(due_col.isnot(None))
    )

    overdue_rows = (
        q.filter(due_col < today)
        .order_by(due_col.asc())
        .all()
    )

    next_rows = (
        q.filter(due_col >= today)
        .filter(due_col <= end)
        .order_by(due_col.asc())
        .all()
    )

    def _to_row_dict(r) -> Dict[str, Any]:
        due_date_val = r.due_date
        due_date_fmt = due_date_val.strftime("%m/%d/%Y") if due_date_val else ""

        return {
            "report_id": None,
            "claim_id": int(r.claim_id),
            "last_name": (r.last_name or "").strip(),
            "first_name": (r.first_name or "").strip(),
            "claim_number": (r.claim_number or "").strip(),
            "report_type": "",
            "report_number": "",
            "due_date": due_date_fmt,
        }

    return {
        "overdue": [_to_row_dict(r) for r in overdue_rows],
        "next_7": [_to_row_dict(r) for r in next_rows],
    }


def get_upcoming_appointments(*, days_ahead: int = 7, period: Optional[str] = None) -> List[Dict[str, Any]]:
    from app.models import Claim, Report, db
    from sqlalchemy import cast, String

    today = date.today()
    window_end = today + timedelta(days=days_ahead)

    has_next_appt = hasattr(Report, "next_appt_datetime")
    has_initial_next_appt = hasattr(Report, "initial_next_appt_datetime")
    if not (has_next_appt or has_initial_next_appt):
        return []

    def _format_datetime(dt: datetime):
        if not dt:
            return "", ""
        return (
            dt.strftime("%m/%d/%Y"),
            dt.strftime("%-I:%M %p") if hasattr(dt, "strftime") else "",
        )

    out: List[Dict[str, Any]] = []
    seen_report_ids = set()

    # ------------------------------------------------------------------
    # Progress-style appointments (next_appt_datetime)
    # ------------------------------------------------------------------
    if has_next_appt:
        provider_col = getattr(Report, "next_appt_provider_name", None)
        notes_col = getattr(Report, "next_appt_notes", None)

        q = (
            db.session.query(
                Report.id.label("report_id"),
                Report.claim_id.label("claim_id"),
                Claim.claimant_last_name.label("last_name"),
                Claim.claimant_first_name.label("first_name"),
                Claim.claim_number.label("claim_number"),
                Report.next_appt_datetime.label("appt_dt"),
                provider_col.label("provider_name") if provider_col else cast("", String).label("provider_name"),
                notes_col.label("notes") if notes_col else cast("", String).label("notes"),
            )
            .join(Claim, Claim.id == Report.claim_id)
            .filter(Report.next_appt_datetime.isnot(None))
            .order_by(Report.next_appt_datetime.asc())
        )

        rows = q.all()

        for r in rows:
            appt_dt = r.appt_dt
            if not appt_dt:
                continue
            appt_date = appt_dt.date()
            if not (today <= appt_date <= window_end):
                continue

            formatted_date, formatted_time = _format_datetime(appt_dt)

            out.append(
                {
                    "report_id": int(r.report_id),
                    "claim_id": int(r.claim_id),
                    "claim_display": f"{(r.last_name or '').strip()}, {(r.first_name or '').strip()} – {(r.claim_number or '').strip()}",
                    "provider_name": (r.provider_name or "").strip(),
                    "formatted_date": formatted_date,
                    "formatted_time": formatted_time,
                    "notes": (r.notes or "").strip(),
                    "appt_dt_obj": appt_dt,
                }
            )
            seen_report_ids.add(r.report_id)

    # ------------------------------------------------------------------
    # Initial report appointments (initial_next_appt_datetime)
    # ------------------------------------------------------------------
    if has_initial_next_appt:
        provider_col = getattr(Report, "initial_next_appt_provider_name", None)
        notes_col = getattr(Report, "initial_next_appt_notes", None)

        q = (
            db.session.query(
                Report.id.label("report_id"),
                Report.claim_id.label("claim_id"),
                Claim.claimant_last_name.label("last_name"),
                Claim.claimant_first_name.label("first_name"),
                Claim.claim_number.label("claim_number"),
                Report.initial_next_appt_datetime.label("appt_dt"),
                provider_col.label("provider_name") if provider_col else cast("", String).label("provider_name"),
                notes_col.label("notes") if notes_col else cast("", String).label("notes"),
            )
            .join(Claim, Claim.id == Report.claim_id)
            .filter(Report.initial_next_appt_datetime.isnot(None))
            .order_by(Report.initial_next_appt_datetime.asc())
        )

        rows = q.all()

        for r in rows:
            if r.report_id in seen_report_ids:
                continue

            appt_dt = r.appt_dt
            if not appt_dt:
                continue
            appt_date = appt_dt.date()
            if not (today <= appt_date <= window_end):
                continue

            formatted_date, formatted_time = _format_datetime(appt_dt)

            out.append(
                {
                    "report_id": int(r.report_id),
                    "claim_id": int(r.claim_id),
                    "claim_display": f"{(r.last_name or '').strip()}, {(r.first_name or '').strip()} – {(r.claim_number or '').strip()}",
                    "provider_name": (r.provider_name or "").strip(),
                    "formatted_date": formatted_date,
                    "formatted_time": formatted_time,
                    "notes": (r.notes or "").strip(),
                    "appt_dt_obj": appt_dt,
                }
            )
            seen_report_ids.add(r.report_id)

    # Final sort by actual datetime
    out.sort(key=lambda x: x.get("appt_dt_obj") or datetime.max)

    # Remove internal datetime object before returning
    for row in out:
        row.pop("appt_dt_obj", None)

    return out


def get_money_position(period: Optional[str] = None) -> Dict[str, float]:
    """Where the money sits: open invoices + uninvoiced billables (estimated)."""
    from app.models import BillableItem, Invoice, db

    inv_total_col = _invoice_total_attr(Invoice)
    inv_date_col = _invoice_date_attr(Invoice)
    inv_status_col = _invoice_status_attr(Invoice)

    open_invoices_total = 0.0
    if inv_total_col is not None:
        q = db.session.query(func.coalesce(func.sum(inv_total_col), 0))
        # "Open" = not Paid/Void
        if inv_status_col is not None:
            q = q.filter(func.upper(inv_status_col).notin_(["PAID", "VOID"]))
        # If period is provided, apply date filtering
        if period is not None:
            start, end = get_period_bounds(period)
            if inv_date_col is not None:
                q = q.filter(inv_date_col.isnot(None)).filter(inv_date_col >= start).filter(inv_date_col <= end)
        open_invoices_total = float(q.scalar() or 0)

    # -------------------------------------------------------------
    # Uninvoiced billables (quantity * global billing rate)
    # Excludes NO BILL and incomplete items
    # -------------------------------------------------------------
    service_date = _billable_service_date_attr(BillableItem)
    qty_col = getattr(BillableItem, "quantity", None)
    code_col = getattr(BillableItem, "activity_code", None)
    is_complete_col = getattr(BillableItem, "is_complete", None)

    uninvoiced_total = 0.0

    if qty_col is not None:
        from app.models import Settings
        settings = db.session.query(Settings).first()
        billing_rate = float(getattr(settings, "hourly_rate", 0.0) or 0.0)

        code_u = func.upper(func.coalesce(code_col, "")) if code_col is not None else None

        qty_sum_expr = func.coalesce(func.sum(func.coalesce(qty_col, 0)), 0)

        q2 = db.session.query(qty_sum_expr) \
            .filter(BillableItem.invoice_id.is_(None))

        # Exclude NO BILL
        if code_u is not None:
            q2 = q2.filter(code_u != "NO BILL")

        # Exclude incomplete items
        if is_complete_col is not None:
            q2 = q2.filter(is_complete_col.is_(True))

        # Apply period filtering
        if period is not None and service_date is not None:
            start, end = get_period_bounds(period)
            q2 = q2.filter(service_date.isnot(None)) \
                   .filter(service_date >= start) \
                   .filter(service_date <= end)

        total_qty = float(q2.scalar() or 0.0)
        uninvoiced_total = total_qty * billing_rate

    total = float(open_invoices_total + uninvoiced_total)

    return {
        "open_invoices": open_invoices_total,
        "uninvoiced_billables": uninvoiced_total,
        "total": total,
        "open_invoices_pct": (open_invoices_total / total * 100.0) if total else 0.0,
        "uninvoiced_billables_pct": (uninvoiced_total / total * 100.0) if total else 0.0,
    }


def get_billable_breakdown_by_code(period: str = "MTD", *, limit: int = 12) -> List[Dict[str, Any]]:
    """Return [{code, total, pct}] grouped by BillableItem.activity_code.

    - Excludes NO BILL
    - Excludes incomplete items
    - Uses quantity * Settings.hourly_rate
    - Respects selected period based on date_of_service
    """

    from app.models import BillableItem, Settings, db

    start, end = get_period_bounds(period)

    service_date = _billable_service_date_attr(BillableItem)
    qty_col = getattr(BillableItem, "quantity", None)
    code_col = getattr(BillableItem, "activity_code", None)
    is_complete_col = getattr(BillableItem, "is_complete", None)

    if qty_col is None or code_col is None:
        return []

    # Get billing rate
    settings = db.session.query(Settings).first()
    hourly_rate = float(getattr(settings, "hourly_rate", 0.0) or 0.0)

    code_u = func.upper(func.coalesce(code_col, ""))

    qty_expr = func.coalesce(qty_col, 0)

    code_expr = func.coalesce(func.nullif(func.trim(code_col), ""), "(none)").label("code")
    qty_sum_expr = func.coalesce(func.sum(qty_expr), 0).label("total_qty")

    q = db.session.query(
        code_expr,
        qty_sum_expr,
    )

    # Only uninvoiced items
    q = q.filter(BillableItem.invoice_id.is_(None))

    # Exclude NO BILL
    q = q.filter(code_u != "NO BILL")

    # Exclude incomplete
    if is_complete_col is not None:
        q = q.filter(is_complete_col.is_(True))

    # Apply period filter
    if service_date is not None:
        q = (
            q.filter(service_date.isnot(None))
             .filter(service_date >= start)
             .filter(service_date <= end)
        )

    q = q.group_by(code_expr).order_by(func.sum(qty_expr).desc())

    rows = q.all()
    total_all = float(sum(float((r.total_qty or 0) * hourly_rate) for r in rows))

    top = rows[: max(0, int(limit))]
    remainder = rows[max(0, int(limit)) :]

    out: List[Dict[str, Any]] = []

    for r in top:
        total_qty = float(r.total_qty or 0)
        t = total_qty * hourly_rate
        out.append(
            {
                "code": (r.code or "").strip(),
                "total": t,
                "pct": (t / total_all * 100.0) if total_all else 0.0,
            }
        )

    if remainder:
        rem_total = float(sum(float((r.total_qty or 0) * hourly_rate) for r in remainder))
        out.append(
            {
                "code": "Other",
                "total": rem_total,
                "pct": (rem_total / total_all * 100.0) if total_all else 0.0,
            }
        )

    return out


def get_productivity_metrics(period: str = "MTD") -> Dict[str, float]:
    """Productivity totals: hours, miles, expenses (best-effort).

    This uses billable items, not invoices.
    """
    from app.models import BillableItem, db

    start, end = get_period_bounds(period)

    service_date = _billable_service_date_attr(BillableItem)
    code_col = getattr(BillableItem, "activity_code", None)
    qty_col = getattr(BillableItem, "quantity", None)

    if code_col is None or qty_col is None:
        return {"hours": 0.0, "miles": 0.0, "expenses": 0.0, "total_units": 0.0}

    # Heuristic classification by activity_code
    code_u = func.upper(func.coalesce(code_col, ""))

    miles_case = case(
        (code_u.in_(["MIL", "MILE", "MILES"]), func.coalesce(qty_col, 0)),
        else_=0,
    )

    # Expenses may be captured as quantity or amount depending on schema.
    # If there's an amount field, prefer that.
    if hasattr(BillableItem, "amount"):
        exp_val = func.coalesce(getattr(BillableItem, "amount"), 0)
    else:
        exp_val = func.coalesce(qty_col, 0)

    exp_case = case(
        (code_u.in_(["EXP", "EXPENSE", "EXPENSES"]), exp_val),
        else_=0,
    )

    # Everything else with quantity counts as "hours" (including Travel, Admin, etc.)
    hours_case = case(
        (
            and_(
                ~code_u.in_(["MIL", "MILE", "MILES", "EXP", "EXPENSE", "EXPENSES", "NO BILL"]),
                qty_col.isnot(None),
            ),
            func.coalesce(qty_col, 0),
        ),
        else_=0,
    )

    q = db.session.query(
        func.coalesce(func.sum(hours_case), 0).label("hours"),
        func.coalesce(func.sum(miles_case), 0).label("miles"),
        func.coalesce(func.sum(exp_case), 0).label("expenses"),
    )

    if service_date is not None:
        q = q.filter(service_date.isnot(None)).filter(service_date >= start).filter(service_date <= end)

    r = q.one()
    hours = float(r.hours or 0)
    miles = float(r.miles or 0)
    expenses = float(r.expenses or 0)

    return {
        "hours": hours,
        "miles": miles,
        "expenses": expenses,
        "total_units": float(hours + miles + expenses),
    }


def get_revenue_metrics(period: str = "MTD") -> Dict[str, float]:
    """Revenue metrics based on invoices (not billables)."""
    from app.models import Invoice, db

    start, end = get_period_bounds(period)

    total_col = _invoice_total_attr(Invoice)
    date_col = _invoice_date_attr(Invoice)
    status_col = _invoice_status_attr(Invoice)
    # Fallback to created_at if invoice_date missing or NULL
    created_col = getattr(Invoice, "created_at", None)

    if total_col is None:
        return {"invoiced_total": 0.0, "open_total": 0.0, "paid_total": 0.0}

    # Base query scoped to period if invoice_date exists
    base = db.session.query(func.coalesce(func.sum(total_col), 0))

    if date_col is not None:
        base = base.filter(
            or_(
                and_(date_col.isnot(None), date_col >= start, date_col <= end),
                and_(
                    date_col.is_(None),
                    created_col.isnot(None),
                    func.date(created_col) >= start,
                    func.date(created_col) <= end,
                ),
            )
        )
    elif created_col is not None:
        base = (
            base.filter(created_col.isnot(None))
                .filter(func.date(created_col) >= start)
                .filter(func.date(created_col) <= end)
        )

    invoiced_total = float(base.scalar() or 0.0)

    # Open = not PAID and not VOID (if status exists)
    if status_col is not None:
        open_total = float(
            base.filter(func.upper(status_col).notin_(["PAID", "VOID"])).scalar() or 0.0
        )
        paid_total = float(
            base.filter(func.upper(status_col) == "PAID").scalar() or 0.0
        )
    else:
        # If no status column, treat all invoiced as open
        open_total = invoiced_total
        paid_total = 0.0

    return {
        "invoiced_total": invoiced_total,
        "open_total": open_total,
        "paid_total": paid_total,
    }


def get_monthly_revenue_trend(months: int = 6) -> List[Dict[str, Any]]:
    """Return last N months of invoiced revenue totals.

    Output:
    [{"month": "YYYY-MM", "total": float}]
    """
    from app.models import Invoice, db

    today = date.today()
    start_month = date(today.year, today.month, 1)

    # Build month list going backward
    months_list: List[date] = []
    y = start_month.year
    m = start_month.month

    for _ in range(months):
        months_list.append(date(y, m, 1))
        if m == 1:
            m = 12
            y -= 1
        else:
            m -= 1

    months_list = sorted(months_list)

    total_col = _invoice_total_attr(Invoice)
    date_col = _invoice_date_attr(Invoice)

    results: List[Dict[str, Any]] = []

    for month_start in months_list:
        if month_start.month == 12:
            next_month = date(month_start.year + 1, 1, 1)
        else:
            next_month = date(month_start.year, month_start.month + 1, 1)

        if total_col is None or date_col is None:
            month_total = 0.0
        else:
            month_total = float(
                db.session.query(func.coalesce(func.sum(total_col), 0))
                .filter(date_col.isnot(None))
                .filter(date_col >= month_start)
                .filter(date_col < next_month)
                .scalar()
                or 0.0
            )

        results.append(
            {
                "month": month_start.strftime("%Y-%m"),
                "total": month_total,
            }
        )

    return results


# -----------------------------------------------------------------------------
#  New: Flexible revenue trend for week, month, 3M, year
# -----------------------------------------------------------------------------
from typing import Any, Dict, List

# -----------------------------------------------------------------------------
#  Active Claim Trend
# -----------------------------------------------------------------------------
def get_active_claim_trend(period: str = "MTD") -> List[Dict[str, Any]]:
    """
    Active claim trend using opened_at / closed_at.

    Active on a given date = 
        opened_at <= bucket_date
        AND (closed_at IS NULL OR closed_at > bucket_date)
    """

    from app.models import Claim, db

    start, end = get_period_bounds(period)
    p = (period or "MONTH").strip().upper()

    opened_col = getattr(Claim, "opened_at", None)
    closed_col = getattr(Claim, "closed_at", None)

    if opened_col is None:
        return []

    results: List[Dict[str, Any]] = []

    # --- WEEK → daily buckets ---
    if p in ("WEEK", "7D"):
        current = start
        while current <= end:
            q = db.session.query(func.count(Claim.id)).filter(
                opened_col <= current
            )

            if closed_col is not None:
                q = q.filter(
                    or_(
                        closed_col.is_(None),
                        closed_col > current
                    )
                )

            count = int(q.scalar() or 0)

            results.append({
                "label": current.strftime("%a"),
                "count": count,
            })

            current += timedelta(days=1)

    # --- Longer ranges → monthly buckets ---
    else:
        current = date(start.year, start.month, 1)

        while current <= end:
            q = db.session.query(func.count(Claim.id)).filter(
                opened_col <= current
            )

            if closed_col is not None:
                q = q.filter(
                    or_(
                        closed_col.is_(None),
                        closed_col > current
                    )
                )

            count = int(q.scalar() or 0)

            results.append({
                "label": current.strftime("%b"),
                "count": count,
            })

            if current.month == 12:
                current = date(current.year + 1, 1, 1)
            else:
                current = date(current.year, current.month + 1, 1)

    return results
def get_open_invoice_aging() -> Dict[str, float]:
    """
    Return aging buckets for open invoices (not PAID/VOID),
    based on invoice_date.

    Buckets:
    - current (0–30 days)
    - 31–60
    - 61–90
    - 90+
    """

    from app.models import Invoice, db

    total_col = _invoice_total_attr(Invoice)
    date_col = _invoice_date_attr(Invoice)
    status_col = _invoice_status_attr(Invoice)

    # If we have no total column at all, we cannot compute aging
    if total_col is None:
        return {
            "current": 0.0,
            "31_60": 0.0,
            "61_90": 0.0,
            "90_plus": 0.0,
        }

    # Fallback to created_at if invoice_date is missing
    created_col = getattr(Invoice, "created_at", None)

    today = date.today()

    # Prefer invoice_date, fallback to created_at
    if date_col is not None:
        date_expr = date_col
    elif created_col is not None:
        date_expr = func.date(created_col)
    else:
        return {
            "current": 0.0,
            "31_60": 0.0,
            "61_90": 0.0,
            "90_plus": 0.0,
        }

    base = db.session.query(
        total_col.label("amount"),
        date_expr.label("invoice_date"),
    )

    # Only SENT invoices count toward A/R aging
    # Exclude Draft, Paid, and Void
    if status_col is not None:
        base = base.filter(func.upper(status_col) == "SENT")

    base = base.filter(date_expr.isnot(None))

    rows = base.all()

    buckets = {
        "current": 0.0,
        "31_60": 0.0,
        "61_90": 0.0,
        "90_plus": 0.0,
    }

    for r in rows:
        amount = float(r.amount or 0.0)
        age_days = (today - r.invoice_date).days

        if age_days <= 30:
            buckets["current"] += amount
        elif age_days <= 60:
            buckets["31_60"] += amount
        elif age_days <= 90:
            buckets["61_90"] += amount
        else:
            buckets["90_plus"] += amount

    return buckets

def get_revenue_trend_for_period(period: str = "MTD") -> List[Dict[str, Any]]:
    """Flexible revenue trend for week, month, 3M, year."""

    from app.models import Invoice, db

    start, end = get_period_bounds(period)
    total_col = _invoice_total_attr(Invoice)
    date_col = _invoice_date_attr(Invoice)
    created_col = getattr(Invoice, "created_at", None)

    if total_col is None or date_col is None:
        return []

    p = (period or "MONTH").strip().upper()
    results: List[Dict[str, Any]] = []

    if p in ("WEEK", "7D"):
        # Daily buckets for last 7 days
        for i in range(7):
            day = start + timedelta(days=i)
            next_day = day + timedelta(days=1)
            total = float(
                db.session.query(func.coalesce(func.sum(total_col), 0))
                .filter(
                    or_(
                        and_(date_col.isnot(None), date_col >= day, date_col < next_day),
                        and_(date_col.is_(None), created_col.isnot(None), created_col >= day, created_col < next_day)
                    )
                )
                .scalar() or 0.0
            )
            results.append({
                "label": day.strftime("%a"),
                "date": day.strftime("%m/%d/%Y"),
                "total": total,
            })

    elif p in ("3M", "90D", "6M", "180D", "12M", "ROLLING_YEAR", "YEAR", "YTD"):
        # Monthly buckets
        current = date(start.year, start.month, 1)
        while current <= end:
            if current.month == 12:
                next_month = date(current.year + 1, 1, 1)
            else:
                next_month = date(current.year, current.month + 1, 1)

            total = float(
                db.session.query(func.coalesce(func.sum(total_col), 0))
                .filter(
                    or_(
                        and_(date_col.isnot(None), date_col >= current, date_col < next_month),
                        and_(date_col.is_(None), created_col.isnot(None), created_col >= current, created_col < next_month)
                    )
                )
                .scalar() or 0.0
            )

            results.append({
                "label": current.strftime("%b"),
                "date": current.strftime("%m/%d/%Y"),
                "total": total,
            })

            current = next_month

    else:
        # Default: weekly buckets for last 30 days
        current = start
        while current <= end:
            week_end = min(current + timedelta(days=7), end + timedelta(days=1))
            total = float(
                db.session.query(func.coalesce(func.sum(total_col), 0))
                .filter(
                    or_(
                        and_(date_col.isnot(None), date_col >= current, date_col < week_end),
                        and_(date_col.is_(None), created_col.isnot(None), created_col >= current, created_col < week_end)
                    )
                )
                .scalar() or 0.0
            )
            results.append({
                "label": current.strftime("%m/%d"),
                "date": current.strftime("%m/%d/%Y"),
                "total": total,
            })
            current = week_end

    return results


def build_dashboard_context(
    revenue_overview_period: str = "MTD",
    billable_breakdown_period: str = "MTD",
    productivity_period: str = "MTD",
    revenue_health_period: str = "MTD",
    active_claim_trend_period: str = "MTD",
    reports_period: str = "MTD",
    appts_period: str = "MTD"
) -> Dict[str, Any]:
    """
    Entrypoint for routes: returns everything the dashboard needs, with per-module period filtering.
    Each live dashboard module receives its own period argument, so dropdowns can control them separately.
    """

    # 1. Revenue Overview module
    revenue = get_revenue_metrics(revenue_overview_period)
    money = get_money_position(period=revenue_overview_period)

    # ------------------------------------------------------------------
    # Revenue Overview per-period totals (for donut selector)
    # ------------------------------------------------------------------
    _rev_week = get_money_position(period="WEEK")
    _rev_month = get_money_position(period="MONTH")
    _rev_quarter = get_money_position(period="3M")
    _rev_six_month = get_money_position(period="6M")
    _rev_rolling_year = get_money_position(period="12M")
    _rev_year = get_money_position(period="YEAR")

    # 2. Billable Breakdown module
    breakdown = get_billable_breakdown_by_code(billable_breakdown_period)

    # Billable Breakdown per-period datasets (for donut selector)
    _bb_week = get_billable_breakdown_by_code("WEEK")
    _bb_month = get_billable_breakdown_by_code("MONTH")
    _bb_quarter = get_billable_breakdown_by_code("3M")
    _bb_six_month = get_billable_breakdown_by_code("6M")
    _bb_rolling_year = get_billable_breakdown_by_code("12M")
    _bb_year = get_billable_breakdown_by_code("YEAR")

    # 3. Productivity module
    productivity = get_productivity_metrics(productivity_period)

    # ---------------------------------------------------------
    # Rolling 30-Day Productivity (Target Band Model)
    # ---------------------------------------------------------
    try:
        from app.models import Settings, db as _db_prod

        settings_prod = _db_prod.session.query(Settings).first()

        weekly_min = float(getattr(settings_prod, "target_min_hours_per_week", 0.0) or 0.0)
        weekly_max = float(getattr(settings_prod, "target_max_hours_per_week", 0.0) or 0.0)

    except Exception:
        weekly_min = 0.0
        weekly_max = 0.0

    # Convert weekly targets to rolling 30-day equivalents
    month_factor = 30.0 / 7.0
    rolling_min = weekly_min * month_factor
    rolling_max = weekly_max * month_factor
    rolling_ceiling = rolling_max * 2.0 if rolling_max > 0 else 0.0

    # Compute rolling 30-day actual hours
    from app.models import BillableItem as _BillableItemProd, db as _db_prod2
    service_date_prod = _billable_service_date_attr(_BillableItemProd)
    qty_col_prod = getattr(_BillableItemProd, "quantity", None)
    code_col_prod = getattr(_BillableItemProd, "activity_code", None)

    rolling_hours = 0.0

    if qty_col_prod is not None and service_date_prod is not None:
        start_30, end_30 = get_period_bounds("MONTH")  # 30-day rolling
        code_u_prod = func.upper(func.coalesce(code_col_prod, ""))
        hours_case_prod = case(
            (
                and_(
                    ~code_u_prod.in_(["MIL", "MILE", "MILES", "EXP", "EXPENSE", "EXPENSES", "NO BILL"]),
                    qty_col_prod.isnot(None),
                ),
                func.coalesce(qty_col_prod, 0),
            ),
            else_=0,
        )

        rolling_hours = float(
            _db_prod2.session.query(func.coalesce(func.sum(hours_case_prod), 0))
            .filter(service_date_prod.isnot(None))
            .filter(service_date_prod >= start_30)
            .filter(service_date_prod <= end_30)
            .scalar()
            or 0.0
        )

    # ---------------------------------------------------------
    # Rolling Utilization Calculations (Capped + Display)
    # ---------------------------------------------------------
    avg_weekly_hours = (
        float(rolling_hours) / (30.0 / 7.0)
        if rolling_hours and rolling_hours > 0
        else 0.0
    )

    # Avoid divide-by-zero
    if weekly_max > 0:
        raw_util_percent = (avg_weekly_hours / weekly_max) * 100.0
    else:
        raw_util_percent = 0.0

    # Cap gauge display at 100% (needle cannot exceed full scale)
    capped_util_percent = min(max(raw_util_percent, 0.0), 100.0)

    # Track true overage separately (for text display if desired)
    over_capacity_percent = max(raw_util_percent - 100.0, 0.0)

    target_band_label = f"{weekly_min:.0f}–{weekly_max:.0f} hrs/week"

    # 4. Revenue Health module (Revenue per Active Claim vs Target)
    try:
        from app.models import Settings, Claim, db as _db_rh

        settings = _db_rh.session.query(Settings).first()
        target_per_claim = float(getattr(settings, "target_revenue_per_claim", 0.0) or 0.0)

        # Active claims (not CLOSED if status exists)
        if hasattr(Claim, "status"):
            active_claims = (
                _db_rh.session.query(func.count(Claim.id))
                .filter(func.upper(Claim.status) != "CLOSED")
                .scalar()
                or 0
            )
        else:
            active_claims = _db_rh.session.query(func.count(Claim.id)).scalar() or 0

        # Recompute revenue for the revenue_health_period (period-aware gauge)
        revenue_for_health = get_revenue_metrics(revenue_health_period)
        revenue_per_claim_health = (
            (float(revenue_for_health.get("invoiced_total", 0.0)) / active_claims)
            if active_claims > 0
            else 0.0
        )

        revenue_health_percent = (
            min(max((revenue_per_claim_health / target_per_claim) * 100.0, 0.0), 200.0)
            if target_per_claim > 0
            else 0.0
        )

    except Exception:
        revenue_health_percent = 0.0

    # Revenue Health per-period datasets (for gauge selector)
    def _compute_revenue_health_for(period_key: str):
        try:
            from app.models import Settings, Claim, db as _db_local
            settings_local = _db_local.session.query(Settings).first()
            target_local = float(getattr(settings_local, "target_revenue_per_claim", 0.0) or 0.0)

            if hasattr(Claim, "status"):
                active_local = (
                    _db_local.session.query(func.count(Claim.id))
                    .filter(func.upper(Claim.status) != "CLOSED")
                    .scalar()
                    or 0
                )
            else:
                active_local = _db_local.session.query(func.count(Claim.id)).scalar() or 0

            rev_local = get_revenue_metrics(period_key)
            revenue_per_claim_local = (
                (float(rev_local.get("invoiced_total", 0.0)) / active_local)
                if active_local > 0
                else 0.0
            )

            percent_local = (
                min(max((revenue_per_claim_local / target_local) * 100.0, 0.0), 200.0)
                if target_local > 0
                else 0.0
            )

            return percent_local
        except Exception:
            return 0.0

    _rh_week = _compute_revenue_health_for("WEEK")
    _rh_month = _compute_revenue_health_for("MONTH")
    _rh_quarter = _compute_revenue_health_for("3M")
    _rh_six_month = _compute_revenue_health_for("6M")
    _rh_rolling_year = _compute_revenue_health_for("12M")
    _rh_year = _compute_revenue_health_for("YEAR")

    # 5. Active Claim Trend module
    active_claim_trend = get_active_claim_trend(active_claim_trend_period)

    # Active Claim Trend per-period datasets (for chart selector)
    _act_week = get_active_claim_trend("WEEK")
    _act_month = get_active_claim_trend("MONTH")
    _act_quarter = get_active_claim_trend("3M")
    _act_six_month = get_active_claim_trend("6M")
    _act_rolling_year = get_active_claim_trend("12M")
    _act_year = get_active_claim_trend("YEAR")

    # Revenue Trends (for charts, always provide all periods for flexibility)
    revenue_trend_week = get_revenue_trend_for_period("WEEK")
    revenue_trend_month = get_revenue_trend_for_period("MONTH")
    revenue_trend_quarter = get_revenue_trend_for_period("3M")
    revenue_trend_six_month = get_revenue_trend_for_period("6M")
    revenue_trend_rolling_year = get_revenue_trend_for_period("12M")
    revenue_trend_year = get_revenue_trend_for_period("YEAR")

    # 6. Open Invoice Aging
    aging = get_open_invoice_aging()

    # Reporting + Scheduling (appointments and reports_due: these use their own periods)
    reports_due = get_reports_due(days_ahead=7, period=reports_period)
    appointments = get_upcoming_appointments(days_ahead=7, period=appts_period)


    # ---------------------------------------------------------
    # System
    # Active Claims (Global + Period for active_claim_trend_period)
    try:
        from app.models import Claim, Invoice as _Invoice, BillableItem as _BillableItem, db as _db
        # ---- Global Active Claims ----
        if hasattr(Claim, "status"):
            active_global_count = (
                _db.session.query(func.count(Claim.id))
                .filter(func.upper(Claim.status) != "CLOSED")
                .scalar()
                or 0
            )
        else:
            active_global_count = _db.session.query(func.count(Claim.id)).scalar() or 0
        # ---- Period Active Claims ----
        start_period, end_period = get_period_bounds(active_claim_trend_period)
        period_claim_ids = set()
        # Claims with invoices in period
        inv_date_col2 = _invoice_date_attr(_Invoice)
        if inv_date_col2 is not None:
            inv_rows = (
                _db.session.query(_Invoice.claim_id)
                .filter(inv_date_col2.isnot(None))
                .filter(inv_date_col2 >= start_period)
                .filter(inv_date_col2 <= end_period)
                .distinct()
                .all()
            )
            period_claim_ids.update(r.claim_id for r in inv_rows if r.claim_id)
        # Claims with billables in period
        service_date2 = _billable_service_date_attr(_BillableItem)
        if service_date2 is not None:
            bill_rows = (
                _db.session.query(_BillableItem.claim_id)
                .filter(service_date2.isnot(None))
                .filter(service_date2 >= start_period)
                .filter(service_date2 <= end_period)
                .distinct()
                .all()
            )
            period_claim_ids.update(r.claim_id for r in bill_rows if r.claim_id)
        active_period_count = len(period_claim_ids)
    except Exception:
        active_global_count = 0
        active_period_count = 0

    # ---------------------------------------------------------
    # Claim Economics (Compact KPI Grid)
    # ---------------------------------------------------------
    try:
        # Rolling 30-day revenue (invoice-based, month = 30D rolling)
        rolling_revenue_30 = float(
            get_revenue_metrics("MONTH").get("invoiced_total", 0.0) or 0.0
        )

        # Revenue run rate (annualized from rolling 30-day revenue)
        revenue_run_rate = rolling_revenue_30 * 12.0

        # Revenue per hour (based on selected productivity period hours)
        total_hours_period = float(productivity.get("hours", 0.0) or 0.0)
        revenue_per_hour = (
            (float(revenue.get("invoiced_total", 0.0)) / total_hours_period)
            if total_hours_period > 0
            else 0.0
        )

        # Avg revenue per claim (rolling 12 months)
        try:
            from app.models import Invoice as _InvAvg, db as _db_avg

            inv_total_col_avg = _invoice_total_attr(_InvAvg)
            inv_date_col_avg = _invoice_date_attr(_InvAvg)
            created_col_avg = getattr(_InvAvg, "created_at", None)

            start_12m, end_12m = get_period_bounds("12M")

            if inv_total_col_avg is not None:
                # Use invoice_date if present, fallback to created_at
                if inv_date_col_avg is not None:
                    date_filter_expr = inv_date_col_avg
                elif created_col_avg is not None:
                    date_filter_expr = func.date(created_col_avg)
                else:
                    date_filter_expr = None

                if date_filter_expr is not None:
                    # Total revenue last 12 months
                    rev_12m = float(
                        _db_avg.session.query(func.coalesce(func.sum(inv_total_col_avg), 0))
                        .filter(date_filter_expr.isnot(None))
                        .filter(date_filter_expr >= start_12m)
                        .filter(date_filter_expr <= end_12m)
                        .scalar()
                        or 0.0
                    )

                    # Distinct claims with invoices in last 12 months
                    claim_count_12m = (
                        _db_avg.session.query(func.count(func.distinct(_InvAvg.claim_id)))
                        .filter(date_filter_expr.isnot(None))
                        .filter(date_filter_expr >= start_12m)
                        .filter(date_filter_expr <= end_12m)
                        .scalar()
                        or 0
                    )

                    avg_revenue_per_claim_12m = (
                        (rev_12m / claim_count_12m)
                        if claim_count_12m > 0
                        else 0.0
                    )
                else:
                    avg_revenue_per_claim_12m = 0.0
            else:
                avg_revenue_per_claim_12m = 0.0

        except Exception:
            avg_revenue_per_claim_12m = 0.0

        # --- Collections Health (Open Invoice Aging) ---
        aging_data = get_open_invoice_aging()
        open_current = float(aging_data.get("current", 0.0) or 0.0)
        open_31_60 = float(aging_data.get("31_60", 0.0) or 0.0)
        open_61_90 = float(aging_data.get("61_90", 0.0) or 0.0)
        open_90_plus = float(aging_data.get("90_plus", 0.0) or 0.0)

        total_open_invoices = (
            open_current + open_31_60 + open_61_90 + open_90_plus
        )

        over_60_total = open_61_90 + open_90_plus

        percent_over_60 = (
            (over_60_total / total_open_invoices) * 100.0
            if total_open_invoices > 0
            else 0.0
        )

        claim_economics = {
            # Revenue Engine
            "rolling_30_day_revenue": rolling_revenue_30,
            "revenue_run_rate": revenue_run_rate,
            "revenue_per_hour": revenue_per_hour,
            "avg_revenue_per_claim_12m": avg_revenue_per_claim_12m,
            "active_claims": int(active_global_count or 0),

            # Collections Health
            "open_invoices_total": total_open_invoices,
            "open_current": open_current,
            "open_31_60": open_31_60,
            "open_61_90": open_61_90,
            "open_90_plus": open_90_plus,
            "percent_over_60": percent_over_60,
        }

    except Exception:
        claim_economics = {
            "rolling_30_day_revenue": 0.0,
            "revenue_run_rate": 0.0,
            "revenue_per_hour": 0.0,
            "avg_revenue_per_claim_12m": 0.0,
            "active_claims": 0,

            "open_invoices_total": 0.0,
            "open_current": 0.0,
            "open_31_60": 0.0,
            "open_61_90": 0.0,
            "open_90_plus": 0.0,
            "percent_over_60": 0.0,
        }

    # Revenue per claim (for Revenue Overview period)
    try:
        from app.models import Invoice as _Invoice2, db as _db2
        inv_date_col3 = _invoice_date_attr(_Invoice2)
        claim_count_query = _db2.session.query(func.count(func.distinct(_Invoice2.claim_id)))
        if inv_date_col3 is not None:
            start_r, end_r = get_period_bounds(revenue_overview_period)
            claim_count_query = (
                claim_count_query
                .filter(inv_date_col3.isnot(None))
                .filter(inv_date_col3 >= start_r)
                .filter(inv_date_col3 <= end_r)
            )
        active_claims_count = claim_count_query.scalar() or 0
    except Exception:
        active_claims_count = 0
    invoiced_total = float(revenue.get("invoiced_total", 0.0) or 0.0)
    revenue_per_claim = (
        (invoiced_total / active_claims_count)
        if active_claims_count > 0
        else 0.0
    )

    # Revenue per claim SD (for Revenue Overview period)
    revenue_sd = 0.0
    try:
        from app.models import Invoice as _Invoice3, db as _db3
        inv_total_col2 = _invoice_total_attr(_Invoice3)
        inv_date_col4 = _invoice_date_attr(_Invoice3)
        if inv_total_col2 is not None:
            q = (
                _db3.session.query(
                    _Invoice3.claim_id,
                    func.coalesce(func.sum(inv_total_col2), 0).label("claim_total"),
                )
                .group_by(_Invoice3.claim_id)
            )
            if inv_date_col4 is not None:
                start_r, end_r = get_period_bounds(revenue_overview_period)
                q = (
                    q.filter(inv_date_col4.isnot(None))
                     .filter(inv_date_col4 >= start_r)
                     .filter(inv_date_col4 <= end_r)
                )
            rows = q.all()
            values = [float(r.claim_total or 0.0) for r in rows if r.claim_total is not None]
            if len(values) > 1:
                revenue_sd = float(statistics.stdev(values))
    except Exception:
        revenue_sd = 0.0

    # Productivity percent (for Productivity period)
    try:
        from app.models import Settings, db as _db4
        settings = _db4.session.query(Settings).first()
        weekly_capacity = float(getattr(settings, "target_max_hours_per_week", 40.0) or 40.0)
    except Exception:
        weekly_capacity = 40.0
    # Period-aware baseline using exact period length
    start_p, end_p = get_period_bounds(productivity_period)
    days_in_period = (end_p - start_p).days + 1
    weeks_in_period = days_in_period / 7.0
    baseline_hours = weekly_capacity * weeks_in_period
    hours = float(productivity.get("hours", 0.0) or 0.0)
    productivity_percent = min(
        max((hours / baseline_hours) * 100.0, 0.0),
        150.0,
    )

    # Hours per claim SD (for Productivity period)
    hours_sd = 0.0
    try:
        from app.models import BillableItem as _BillableItem2, db as _db5
        service_date3 = _billable_service_date_attr(_BillableItem2)
        qty_col = getattr(_BillableItem2, "quantity", None)
        claim_fk = getattr(_BillableItem2, "claim_id", None)
        if qty_col is not None and claim_fk is not None:
            q = (
                _db5.session.query(
                    claim_fk,
                    func.coalesce(func.sum(qty_col), 0).label("claim_hours"),
                )
                .group_by(claim_fk)
            )
            if service_date3 is not None:
                start, end = get_period_bounds(productivity_period)
                q = (
                    q.filter(service_date3.isnot(None))
                     .filter(service_date3 >= start)
                     .filter(service_date3 <= end)
                )
            rows = q.all()
            values = [float(r.claim_hours or 0.0) for r in rows if r.claim_hours is not None]
            if len(values) > 1:
                hours_sd = float(statistics.stdev(values))
    except Exception:
        hours_sd = 0.0

    # System Health (no composite gauge)
    system_health = get_system_health()

    # Dates for current Revenue Overview period
    start_current, end_current = get_period_bounds(revenue_overview_period)

    return {
        # Revenue Overview Donut Data (per-period open + uninvoiced)
        "revenue_week_open": float(_rev_week.get("open_invoices", 0.0) or 0.0),
        "revenue_week_uninvoiced": float(_rev_week.get("uninvoiced_billables", 0.0) or 0.0),

        "revenue_month_open": float(_rev_month.get("open_invoices", 0.0) or 0.0),
        "revenue_month_uninvoiced": float(_rev_month.get("uninvoiced_billables", 0.0) or 0.0),

        "revenue_quarter_open": float(_rev_quarter.get("open_invoices", 0.0) or 0.0),
        "revenue_quarter_uninvoiced": float(_rev_quarter.get("uninvoiced_billables", 0.0) or 0.0),

        "revenue_six_month_open": float(_rev_six_month.get("open_invoices", 0.0) or 0.0),
        "revenue_six_month_uninvoiced": float(_rev_six_month.get("uninvoiced_billables", 0.0) or 0.0),

        "revenue_rolling_year_open": float(_rev_rolling_year.get("open_invoices", 0.0) or 0.0),
        "revenue_rolling_year_uninvoiced": float(_rev_rolling_year.get("uninvoiced_billables", 0.0) or 0.0),

        "revenue_year_open": float(_rev_year.get("open_invoices", 0.0) or 0.0),
        "revenue_year_uninvoiced": float(_rev_year.get("uninvoiced_billables", 0.0) or 0.0),
        # Per-module period keys (frontend can wire dropdowns to these)
        "revenue_overview_period": (revenue_overview_period or "MTD").upper(),
        "billable_breakdown_period": (billable_breakdown_period or "MTD").upper(),
        "productivity_period": (productivity_period or "MTD").upper(),
        "revenue_health_period": (revenue_health_period or "MTD").upper(),
        "active_claim_trend_period": (active_claim_trend_period or "MTD").upper(),
        "reports_period": (reports_period or "MTD").upper(),
        "appts_period": (appts_period or "MTD").upper(),
        "period_start": start_current.strftime("%m/%d/%Y"),
        "period_end": end_current.strftime("%m/%d/%Y"),

        # Reporting + Scheduling
        "reports_due": reports_due,
        "appointments": appointments,

        # Financial Position
        "money": money,
        "invoice_aging": aging,
        # Billable Breakdown Donut Data (per-period)
        "billing_breakdown_week": _bb_week,
        "billing_breakdown_month": _bb_month,
        "billing_breakdown_quarter": _bb_quarter,
        "billing_breakdown_six_month": _bb_six_month,
        "billing_breakdown_rolling_year": _bb_rolling_year,
        "billing_breakdown_year": _bb_year,
        "breakdown_by_code": breakdown,
        "revenue": revenue,
        "revenue_trend_week": revenue_trend_week,
        "revenue_trend_month": revenue_trend_month,
        "revenue_trend_quarter": revenue_trend_quarter,
        "revenue_trend_six_month": revenue_trend_six_month,
        "revenue_trend_rolling_year": revenue_trend_rolling_year,
        "revenue_trend_year": revenue_trend_year,
        "active_claim_trend": active_claim_trend,
        "active_claim_trend_week": _act_week,
        "active_claim_trend_month": _act_month,
        "active_claim_trend_quarter": _act_quarter,
        "active_claim_trend_six_month": _act_six_month,
        "active_claim_trend_rolling_year": _act_rolling_year,
        "active_claim_trend_year": _act_year,
        "revenue_per_claim": revenue_per_claim,
        "revenue_per_claim_sd": revenue_sd,
        "claim_economics": claim_economics,

        # Claims
        "active_claims_global": int(active_global_count),
        "active_claims_period": int(active_period_count),

        # Productivity
        "productivity": productivity,
        "hours_per_claim_sd": hours_sd,
        "rolling_30_day_hours": rolling_hours,
        "rolling_min_hours": rolling_min,
        "rolling_max_hours": rolling_max,
        "rolling_ceiling_hours": rolling_ceiling,
        "productivity_rolling": {
            "hours": float(rolling_hours or 0.0),
            "avg_weekly_hours": float(avg_weekly_hours or 0.0),
            "min_weekly_hours": float(weekly_min or 0.0),
            "max_weekly_hours": float(weekly_max or 0.0),
            "utilization_percent_raw": float(raw_util_percent or 0.0),
            "utilization_percent_capped": float(capped_util_percent or 0.0),
            "over_capacity_percent": float(over_capacity_percent or 0.0),
            "target_band_label": target_band_label,
            "avg_hours_per_claim": (
                float(rolling_hours / active_global_count)
                if active_global_count > 0
                else 0.0
            ),
        },

        # Revenue Health
        "revenue_health_week": _rh_week,
        "revenue_health_month": _rh_month,
        "revenue_health_quarter": _rh_quarter,
        "revenue_health_six_month": _rh_six_month,
        "revenue_health_rolling_year": _rh_rolling_year,
        "revenue_health_year": _rh_year,
        "revenue_health_percent": revenue_health_percent,

        # System
        "system_health": system_health,
    }


# -----------------------------------------------------------------------------
#  Optional health checks (safe stubs)
# -----------------------------------------------------------------------------

def get_system_health() -> Dict[str, Any]:
    """Best-effort health snapshot with disk + backup placeholders."""
    import shutil
    from pathlib import Path

    out: Dict[str, Any] = {
        "db_ok": False,
        "db_error": None,
        "disk_free_percent": 1.0,
        "backup_recent": True,
    }

    # --- DB check ---
    try:
        from app.models import db
        db.session.execute(func.now())
        out["db_ok"] = True
    except Exception as e:
        out["db_ok"] = False
        out["db_error"] = str(e)

    # --- Disk space check (documents folder if available) ---
    try:
        base_path = Path(".")
        total, used, free = shutil.disk_usage(base_path)
        free_pct = free / total if total > 0 else 1.0
        out["disk_free_percent"] = float(free_pct)
    except Exception:
        out["disk_free_percent"] = 1.0

    # --- Backup freshness placeholder ---
    # TODO: Replace with real backup timestamp check later
    out["backup_recent"] = True

    return out