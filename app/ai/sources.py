"""
Deterministic data access and aggregation helpers.

Authoritative sources of truth for AI answers.
No conversation state. No frames. No LLM behavior.
"""

from typing import Any, Dict, List, Optional, Tuple


# -------------------------------------------------------------------
# Utility helpers
# -------------------------------------------------------------------

def _claim_has_attr(obj: Any, name: str) -> bool:
    try:
        getattr(obj, name)
        return True
    except Exception:
        return False


def _get_first_attr(obj: Any, names: List[str]) -> Any:
    for name in names:
        try:
            val = getattr(obj, name, None)
            if val is not None:
                return val
        except Exception:
            continue
    return None


def _money(amount: Any) -> str:
    try:
        val = float(amount)
        return f"${val:,.2f}"
    except Exception:
        return "$0.00"


# -------------------------------------------------------------------
# Claim helpers
# -------------------------------------------------------------------

def _claim_open_closed_filter(model: Any, scope: str):
    """Return a SQLAlchemy filter expression (or None) for open/closed where possible."""
    if _claim_has_attr(model, "is_closed"):
        if scope == "open":
            return getattr(model, "is_closed") == False  # noqa: E712
        if scope == "closed":
            return getattr(model, "is_closed") == True  # noqa: E712
        return None

    if _claim_has_attr(model, "closed_at"):
        if scope == "open":
            return getattr(model, "closed_at") == None  # noqa: E711
        if scope == "closed":
            return getattr(model, "closed_at") != None  # noqa: E711
        return None

    if _claim_has_attr(model, "status"):
        status_col = getattr(model, "status")
        if scope == "open":
            return status_col.ilike("%open%") | status_col.ilike("%active%")
        if scope == "closed":
            return status_col.ilike("%closed%") | status_col.ilike("%inactive%")
        return None

    return None


def answer_claim_count(*, scope: str, db: Any, ClaimModel: Any) -> Tuple[int, str]:
    if scope == "both":
        return db.session.query(ClaimModel).count(), "open + closed"

    filt = _claim_open_closed_filter(ClaimModel, scope)
    if filt is None:
        return db.session.query(ClaimModel).count(), "claims"

    return db.session.query(ClaimModel).filter(filt).count(), f"{scope} claims"


def answer_claim_field(*, db: Any, ClaimModel: Any, claim_id: int, field: str) -> Optional[str]:
    try:
        claim = db.session.get(ClaimModel, claim_id)
    except Exception:
        claim = None

    if claim is None:
        return None

    field_map = {
        "dob": ["dob", "date_of_birth"],
        "doi": ["doi", "date_of_injury"],
        "claim_state": ["claim_state", "state"],
        "adjuster": ["adjuster", "adjuster_name"],
        "phone": ["adjuster_phone", "phone"],
        "email": ["email"],
    }

    attrs = field_map.get(field)
    if not attrs:
        return None

    val = _get_first_attr(claim, attrs)
    if val is None or str(val).strip() == "":
        return None

    return str(val)


def answer_claim_summary(
    *,
    db: Any,
    ClaimModel: Any,
    claim_id: int,
    InvoiceModel: Any = None,
    BillableItemModel: Any = None,
) -> str:
    try:
        claim = db.session.get(ClaimModel, claim_id)
    except Exception:
        claim = None

    if claim is None:
        return "Claim not found."

    lines = []

    claimant_name = _get_first_attr(claim, ["claimant_name", "claimant"])
    if not claimant_name:
        last = _get_first_attr(claim, ["claimant_last_name"])
        first = _get_first_attr(claim, ["claimant_first_name"])
        if last or first:
            claimant_name = f"{first or ''} {last or ''}".strip()

    if claimant_name:
        lines.append(f"Claimant: {claimant_name}")

    claim_number = _get_first_attr(claim, ["claim_number", "claim_no", "number"])
    if claim_number:
        lines.append(f"Claim Number: {claim_number}")

    status = None
    if _claim_has_attr(claim, "is_closed"):
        try:
            status = "closed" if claim.is_closed else "open"
        except Exception:
            pass
    elif _claim_has_attr(claim, "closed_at"):
        status = "closed" if claim.closed_at else "open"
    elif _claim_has_attr(claim, "status"):
        status = str(getattr(claim, "status"))

    if status:
        lines.append(f"Status: {status}")

    if InvoiceModel is not None:
        billing = answer_outstanding_billing(db=db, InvoiceModel=InvoiceModel, claim_id=claim_id)
        lines.append(f"Invoices: {billing['count']} outstanding totaling {_money(billing['total'])}")

    if BillableItemModel is not None:
        q = db.session.query(BillableItemModel)
        filt = _billable_claim_filter(BillableItemModel, claim_id)
        if filt is not None:
            q = q.filter(filt)
        lines.append(f"Billable Items: {q.count()}")

    return "\n".join(lines)


# -------------------------------------------------------------------
# Invoice helpers
# -------------------------------------------------------------------

def _invoice_is_paid_filter(model: Any):
    if _claim_has_attr(model, "is_paid"):
        return getattr(model, "is_paid") == True  # noqa: E712
    if _claim_has_attr(model, "paid_at"):
        return getattr(model, "paid_at") != None  # noqa: E711
    if _claim_has_attr(model, "status"):
        return getattr(model, "status").ilike("%paid%")
    return None


def _invoice_claim_filter(model: Any, claim_id: Any):
    if _claim_has_attr(model, "claim_id"):
        return getattr(model, "claim_id") == claim_id
    if _claim_has_attr(model, "claim"):
        try:
            return getattr(model, "claim").has(id=claim_id)
        except Exception:
            return None
    return None


def _invoice_total_expr(model: Any):
    for attr in ["balance_due", "amount_due", "total_amount", "total", "amount"]:
        if _claim_has_attr(model, attr):
            return getattr(model, attr)
    return None


def answer_invoice_status_breakdown(*, db: Any, InvoiceModel: Any) -> Dict[str, Any]:
    from sqlalchemy import not_

    q = db.session.query(InvoiceModel)

    paid_filter = _invoice_is_paid_filter(InvoiceModel)
    if paid_filter is not None:
        q = q.filter(not_(paid_filter))

    unpaid = q.count()

    paid_q = db.session.query(InvoiceModel)
    if paid_filter is not None:
        paid_q = paid_q.filter(paid_filter)

    paid = paid_q.count()

    return {"paid": paid, "unpaid": unpaid}


def answer_outstanding_billing(*, db: Any, InvoiceModel: Any, claim_id: Optional[int] = None) -> Dict[str, Any]:
    from sqlalchemy import func, not_

    q = db.session.query(InvoiceModel)

    if claim_id is not None:
        filt = _invoice_claim_filter(InvoiceModel, claim_id)
        if filt is not None:
            q = q.filter(filt)

    paid_filter = _invoice_is_paid_filter(InvoiceModel)
    if paid_filter is not None:
        q = q.filter(not_(paid_filter))

    count = q.count()

    total_expr = _invoice_total_expr(InvoiceModel)
    total = 0.0
    if total_expr is not None:
        total = q.with_entities(func.coalesce(func.sum(total_expr), 0)).scalar() or 0.0

    return {"count": count, "total": float(total), "label": "outstanding invoices"}


# -------------------------------------------------------------------
# Billable helpers
# -------------------------------------------------------------------

def _billable_claim_filter(model: Any, claim_id: Any):
    if _claim_has_attr(model, "claim_id"):
        return getattr(model, "claim_id") == claim_id
    if _claim_has_attr(model, "claim"):
        try:
            return getattr(model, "claim").has(id=claim_id)
        except Exception:
            return None
    return None


def _billable_is_invoiced_filter(model: Any):
    if _claim_has_attr(model, "is_invoiced"):
        return getattr(model, "is_invoiced") == True  # noqa: E712
    if _claim_has_attr(model, "invoice_id"):
        return getattr(model, "invoice_id") != None  # noqa: E711
    return None


def _billable_qty_expr(model: Any):
    for attr in ["quantity", "qty", "hours", "units", "amount"]:
        if _claim_has_attr(model, attr):
            return getattr(model, attr)
    return None


def answer_billables_totals(*, db: Any, BillableItemModel: Any, claim_id: Optional[int] = None) -> Dict[str, float]:
    q = db.session.query(BillableItemModel)
    if claim_id is not None:
        filt = _billable_claim_filter(BillableItemModel, claim_id)
        if filt is not None:
            q = q.filter(filt)

    hours = miles = exp_dollars = no_bill_hours = 0.0

    for b in q.all():
        activity = str(_get_first_attr(b, ["activity", "activity_code", "code"]) or "").upper()
        qty = _get_first_attr(b, ["quantity", "qty", "hours", "units", "amount"])
        try:
            qty = float(qty)
        except Exception:
            continue

        if activity == "EXP":
            exp_dollars += qty
        elif activity == "MIL":
            miles += qty
        elif activity in {"NO BILL", "NOBILL"}:
            no_bill_hours += qty
        else:
            hours += qty

    return {
        "hours": hours,
        "miles": miles,
        "exp_dollars": exp_dollars,
        "no_bill_hours": no_bill_hours,
    }


def answer_billables_summary(*, db: Any, BillableItemModel: Any, claim_id: Optional[int] = None) -> Dict[str, Any]:
    from sqlalchemy import func

    q = db.session.query(BillableItemModel)
    if claim_id is not None:
        filt = _billable_claim_filter(BillableItemModel, claim_id)
        if filt is not None:
            q = q.filter(filt)

    total_count = q.count()

    invoiced_filt = _billable_is_invoiced_filter(BillableItemModel)
    uninvoiced_q = q.filter(~invoiced_filt) if invoiced_filt is not None else q

    uninvoiced_count = uninvoiced_q.count()

    qty_expr = _billable_qty_expr(BillableItemModel)
    uninvoiced_qty = (
        float(uninvoiced_q.with_entities(func.coalesce(func.sum(qty_expr), 0)).scalar() or 0.0)
        if qty_expr is not None
        else None
    )

    return {
        "total_count": total_count,
        "uninvoiced_count": uninvoiced_count,
        "uninvoiced_qty": uninvoiced_qty,
    }


def answer_uninvoiced_billables_value(*, db: Any, BillableItemModel: Any, claim_id: Optional[int] = None) -> float:
    from sqlalchemy import func

    q = db.session.query(BillableItemModel)
    if claim_id is not None:
        filt = _billable_claim_filter(BillableItemModel, claim_id)
        if filt is not None:
            q = q.filter(filt)

    invoiced_filt = _billable_is_invoiced_filter(BillableItemModel)
    if invoiced_filt is not None:
        q = q.filter(~invoiced_filt)

    qty_expr = _billable_qty_expr(BillableItemModel)
    if qty_expr is None:
        return 0.0

    return float(q.with_entities(func.coalesce(func.sum(qty_expr), 0)).scalar() or 0.0)


def list_uninvoiced_billables(
    *,
    db: Any,
    BillableItemModel: Any,
    claim_id: Optional[int] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    q = db.session.query(BillableItemModel)
    if claim_id is not None:
        filt = _billable_claim_filter(BillableItemModel, claim_id)
        if filt is not None:
            q = q.filter(filt)

    invoiced_filt = _billable_is_invoiced_filter(BillableItemModel)
    if invoiced_filt is not None:
        q = q.filter(~invoiced_filt)

    rows = q.limit(limit).all()
    return [
        {
            "dos": _get_first_attr(b, ["service_date", "dos", "date"]),
            "activity": _get_first_attr(b, ["activity", "activity_code", "code"]),
            "qty": _get_first_attr(b, ["quantity", "qty", "hours", "units", "amount"]),
            "description": _get_first_attr(b, ["description", "short_description"]),
            "notes": _get_first_attr(b, ["notes"]),
        }
        for b in rows
    ]


def derive_billable_mix(*, db: Any, BillableItemModel: Any, claim_id: Optional[int] = None) -> Dict[str, float]:
    totals = answer_billables_totals(db=db, BillableItemModel=BillableItemModel, claim_id=claim_id)
    mix = {}
    total_hours = totals.get("hours", 0.0)
    if total_hours:
        for k, v in totals.items():
            mix[k] = (v / total_hours) if total_hours else 0.0
    return mix


def compute_system_billable_totals(*, db: Any, BillableItemModel: Any) -> Dict[str, float]:
    return answer_billables_totals(db=db, BillableItemModel=BillableItemModel)


def compare_claim_to_system(
    *,
    db: Any,
    BillableItemModel: Any,
    claim_id: int,
) -> Dict[str, Any]:
    claim_totals = answer_billables_totals(db=db, BillableItemModel=BillableItemModel, claim_id=claim_id)
    system_totals = answer_billables_totals(db=db, BillableItemModel=BillableItemModel)
    return {"claim": claim_totals, "system": system_totals}


def top_claims_by_uninvoiced_hours(
    *,
    db: Any,
    BillableItemModel: Any,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    from sqlalchemy import func

    q = db.session.query(BillableItemModel)
    invoiced_filt = _billable_is_invoiced_filter(BillableItemModel)
    if invoiced_filt is not None:
        q = q.filter(~invoiced_filt)

    qty_expr = _billable_qty_expr(BillableItemModel)
    if qty_expr is None:
        return []

    rows = (
        q.with_entities(
            getattr(BillableItemModel, "claim_id"),
            func.sum(qty_expr).label("hours"),
        )
        .group_by(getattr(BillableItemModel, "claim_id"))
        .order_by(func.sum(qty_expr).desc())
        .limit(limit)
        .all()
    )

    return [{"claim_id": r[0], "hours": float(r[1] or 0.0)} for r in rows]


# -------------------------------------------------------------------
# Report helpers
# -------------------------------------------------------------------

def answer_latest_report_work_status(*, db: Any, ReportModel: Any, claim_id: int) -> Optional[str]:
    q = db.session.query(ReportModel)
    if _claim_has_attr(ReportModel, "claim_id"):
        q = q.filter(getattr(ReportModel, "claim_id") == claim_id)
    rpt = q.order_by(getattr(ReportModel, "id").desc()).first()
    if not rpt:
        return None
    return str(_get_first_attr(rpt, ["work_status", "work_status_text"]) or "").strip()



# -------------------------------------------------------------------
# System helpers
# -------------------------------------------------------------------


# ----------------------------
# System snapshot helpers
# ----------------------------

# Workload overview helper for system-wide billable analysis
def answer_workload_overview(
    *,
    db: Any,
    BillableItemModel: Any,
    SettingsModel: Any = None,
) -> str:
    """
    High-level workload analysis based on billables over time.
    Intended for system-wide (non-claim) exploratory questions.
    """
    from sqlalchemy import func
    from datetime import date, timedelta

    lines = []

    # ----------------------------
    # Raw billable totals
    # ----------------------------
    totals = answer_billables_totals(db=db, BillableItemModel=BillableItemModel)
    hours = totals.get("hours", 0.0)
    no_bill = totals.get("no_bill_hours", 0.0)

    lines.append(
        f"Total billable hours recorded: {hours:.1f} "
        f"({no_bill:.1f} non-billable hours)"
    )

    # ----------------------------
    # Time-based averages
    # ----------------------------
    q = db.session.query(BillableItemModel)
    date_col = _get_first_attr(BillableItemModel, ["service_date", "dos", "date"])

    if date_col is not None:
        today = date.today()
        last_7 = today - timedelta(days=7)
        last_30 = today - timedelta(days=30)

        qty_expr = _billable_qty_expr(BillableItemModel)

        last_7_hours = (
            q.filter(date_col >= last_7)
            .with_entities(func.coalesce(func.sum(qty_expr), 0))
            .scalar()
            or 0.0
        )

        last_30_hours = (
            q.filter(date_col >= last_30)
            .with_entities(func.coalesce(func.sum(qty_expr), 0))
            .scalar()
            or 0.0
        )

        avg_daily = last_30_hours / 30 if last_30_hours else 0.0
        avg_weekly = last_7_hours

        lines.append(
            f"Recent workload: ~{avg_daily:.1f} hrs/day "
            f"(~{avg_weekly:.1f} hrs last 7 days)"
        )

    # ----------------------------
    # Compare to workload targets
    # ----------------------------
    if SettingsModel is not None:
        try:
            settings = db.session.query(SettingsModel).first()
        except Exception:
            settings = None

        if settings is not None:
            daily_target = getattr(settings, "daily_billable_hours", None)
            weekly_target = getattr(settings, "weekly_billable_hours", None)

            parts = []

            if daily_target and avg_daily:
                status = "over" if avg_daily > daily_target else "under"
                parts.append(
                    f"daily avg {avg_daily:.1f}h vs target {daily_target:.1f} ({status})"
                )

            if weekly_target and avg_weekly:
                status = "over" if avg_weekly > weekly_target else "under"
                parts.append(
                    f"weekly {avg_weekly:.1f}h vs target {weekly_target:.1f} ({status})"
                )

            if parts:
                lines.append("Workload vs target: " + "; ".join(parts))

    return "\n".join(lines)

def system_claims_snapshot(*, db: Any, ClaimModel: Any) -> str:
    total = db.session.query(ClaimModel).count()
    open_count, _ = answer_claim_count(scope="open", db=db, ClaimModel=ClaimModel)
    closed_count, _ = answer_claim_count(scope="closed", db=db, ClaimModel=ClaimModel)
    return f"Claims: {total} total ({open_count} open, {closed_count} closed)"


def system_billing_snapshot(*, db: Any, InvoiceModel: Any) -> str:
    billing = answer_outstanding_billing(db=db, InvoiceModel=InvoiceModel)
    return (
        f"Invoices: {billing['count']} outstanding "
        f"totaling {_money(billing['total'])}"
    )


def system_workload_snapshot(*, db: Any, BillableItemModel: Any) -> str:
    summary = answer_billables_summary(db=db, BillableItemModel=BillableItemModel)
    totals = answer_billables_totals(db=db, BillableItemModel=BillableItemModel)

    hours = totals.get("hours", 0.0)
    no_bill = totals.get("no_bill_hours", 0.0)

    return (
        f"Billables: {summary['total_count']} total; "
        f"{summary['uninvoiced_count']} uninvoiced; "
        f"{hours:.1f} billed hrs ({no_bill:.1f} non-billable)"
    )


def system_health_snapshot(*, data_path: str = "/") -> str:
    try:
        from system.health import basic_health_snapshot
        health = basic_health_snapshot(data_path=data_path)
    except Exception:
        return "System health: unavailable"

    lines = []

    disk = health.get("disk", {})
    free = disk.get("free_gb")
    total = disk.get("total_gb")
    pct = disk.get("percent_free")
    if free is not None and total is not None:
        if pct is not None:
            lines.append(f"Disk: {free:.1f}GB free of {total:.1f}GB ({pct:.1f}% free)")
        else:
            lines.append(f"Disk: {free:.1f}GB free of {total:.1f}GB")

    temps = health.get("temps") or {}
    cpu_c = temps.get("cpu_c")
    if cpu_c is not None:
        lines.append(f"CPU temp: {cpu_c:.1f} °C")

    for d in temps.get("drives") or []:
        dev = d.get("device")
        t = d.get("temp_c")
        if dev and t is not None:
            lines.append(f"Drive {dev}: {t:.1f} °C")

    mem = health.get("memory_mb")
    if mem is not None:
        lines.append(f"Memory: {mem:.0f} MB")

    uptime = health.get("uptime_seconds")
    if uptime:
        lines.append(f"Uptime: {uptime/3600:.1f} hours")

    backup = health.get("backup")
    if backup:
        if backup.get("exists") and backup.get("age_hours") is not None:
            lines.append(f"Backup age: {backup['age_hours']:.1f} hours")
        elif backup.get("exists") is False:
            lines.append("Backups: not found")

    return "System health: " + "; ".join(lines) if lines else "System health: available"

def answer_system_overview(
    *,
    db: Any,
    ClaimModel: Any,
    InvoiceModel: Any = None,
    BillableItemModel: Any = None,
    ProviderModel: Any = None,
    EmployerModel: Any = None,
    CarrierModel: Any = None,
    ReportModel: Any = None,
) -> str:
    lines = []

    # -------------------------------------------------
    # Claims
    # -------------------------------------------------
    total_claims = db.session.query(ClaimModel).count()
    open_count, _ = answer_claim_count(scope="open", db=db, ClaimModel=ClaimModel)
    closed_count, _ = answer_claim_count(scope="closed", db=db, ClaimModel=ClaimModel)

    lines.append(
        f"Claims: {total_claims} total "
        f"({open_count} open, {closed_count} closed)"
    )

    # -------------------------------------------------
    # Invoices
    # -------------------------------------------------
    if InvoiceModel is not None:
        billing = answer_outstanding_billing(db=db, InvoiceModel=InvoiceModel)
        lines.append(
            f"Invoices: {billing['count']} outstanding "
            f"totaling {_money(billing['total'])}"
        )

    # -------------------------------------------------
    # Billables & workload
    # -------------------------------------------------
    if BillableItemModel is not None:
        summary = answer_billables_summary(db=db, BillableItemModel=BillableItemModel)
        totals = answer_billables_totals(db=db, BillableItemModel=BillableItemModel)

        hours = totals.get("hours", 0.0)
        no_bill = totals.get("no_bill_hours", 0.0)

        lines.append(
            f"Billables: {summary['total_count']} total; "
            f"{summary['uninvoiced_count']} uninvoiced"
        )

        if hours:
            lines.append(
                f"Workload: {hours:.1f} billed hrs "
                f"({no_bill:.1f} non‑billable)"
            )

    # -------------------------------------------------
    # Workload targets (from Settings, if available)
    # -------------------------------------------------
    try:
        SettingsModel = getattr(db, "Settings", None) or globals().get("Settings")
        settings = None
        if SettingsModel is not None:
            settings = db.session.query(SettingsModel).first()

        if settings is not None:
            daily_target = getattr(settings, "daily_billable_hours", None)
            weekly_target = getattr(settings, "weekly_billable_hours", None)

            if daily_target or weekly_target:
                # Compute simple averages if billables exist
                avg_daily = None
                avg_weekly = None

                if BillableItemModel is not None:
                    from sqlalchemy import func
                    from datetime import timedelta, date

                    today = date.today()
                    last_7 = today - timedelta(days=7)
                    last_30 = today - timedelta(days=30)

                    q = db.session.query(BillableItemModel)
                    date_col = _get_first_attr(BillableItemModel, ["service_date", "dos", "date"])

                    if date_col is not None:
                        last_7_hours = q.filter(date_col >= last_7).with_entities(func.sum(_billable_qty_expr(BillableItemModel))).scalar() or 0.0
                        last_30_hours = q.filter(date_col >= last_30).with_entities(func.sum(_billable_qty_expr(BillableItemModel))).scalar() or 0.0

                        avg_daily = last_30_hours / 30 if last_30_hours else None
                        avg_weekly = last_7_hours

                # Build comparison line
                parts = []
                if daily_target and avg_daily is not None:
                    status = "over" if avg_daily > daily_target else "under"
                    parts.append(f"daily avg {avg_daily:.1f}h (target {daily_target:.1f}, {status})")

                if weekly_target and avg_weekly is not None:
                    status = "over" if avg_weekly > weekly_target else "under"
                    parts.append(f"weekly {avg_weekly:.1f}h (target {weekly_target:.1f}, {status})")

                if parts:
                    lines.append("Workload vs target: " + "; ".join(parts))
    except Exception:
        pass

    # -------------------------------------------------
    # Entity footprint
    # -------------------------------------------------
    if ProviderModel is not None:
        try:
            lines.append(f"Providers: {db.session.query(ProviderModel).count()}")
        except Exception:
            pass

    if EmployerModel is not None:
        try:
            lines.append(f"Employers: {db.session.query(EmployerModel).count()}")
        except Exception:
            pass

    if CarrierModel is not None:
        try:
            lines.append(f"Carriers: {db.session.query(CarrierModel).count()}")
        except Exception:
            pass

    # -------------------------------------------------
    # System health
    # -------------------------------------------------
    try:
        from system.health import basic_health_snapshot

        health = basic_health_snapshot(data_path="/")

        # Disk
        disk = health.get("disk", {})
        free = disk.get("free_gb")
        total = disk.get("total_gb")
        pct = disk.get("percent_free")

        if free is not None and total is not None:
            if pct is not None:
                lines.append(
                    f"System health: disk {free:.1f}GB free of {total:.1f}GB ({pct:.1f}% free)"
                )
            else:
                lines.append(
                    f"System health: disk {free:.1f}GB free of {total:.1f}GB"
                )
        else:
            lines.append("System health: available")

        # Temperatures (optional)
        temps = health.get("temps") or {}
        cpu_c = temps.get("cpu_c")
        if cpu_c is not None:
            lines.append(f"CPU temperature: {cpu_c:.1f} °C")

        drives = temps.get("drives") or []
        for d in drives:
            dev = d.get("device")
            t = d.get("temp_c")
            if dev and t is not None:
                lines.append(f"Drive {dev} temperature: {t:.1f} °C")

        # Memory (optional)
        mem = health.get("memory_mb")
        if mem is not None:
            lines.append(f"Memory usage: {mem:.0f} MB")

        # Uptime (optional)
        uptime = health.get("uptime_seconds")
        if uptime:
            hours = uptime / 3600
            lines.append(f"Uptime: {hours:.1f} hours")

        # Backup status (optional)
        backup = health.get("backup")
        if backup:
            if backup.get("exists") and backup.get("age_hours") is not None:
                lines.append(
                    f"Backups: last updated {backup['age_hours']:.1f} hours ago"
                )
            elif backup.get("exists") is False:
                lines.append("Backups: not found")

    except Exception:
        lines.append("System health: unavailable")

    return "\n".join(lines)