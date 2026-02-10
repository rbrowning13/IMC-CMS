from typing import Dict, Any
from sqlalchemy.orm import Session
from decimal import Decimal
from sqlalchemy import func
from app.models import Invoice, BillableItem, Claim
from datetime import datetime, timedelta


# Helper functions for executive snapshot signals
def derive_billing_signals(billing: Dict[str, Any]) -> Dict[str, Any]:
    unpaid = billing.get("unpaid_total", 0)
    uninvoiced = billing.get("uninvoiced_total", 0)
    exposure = billing.get("total_exposure", 0) if billing.get("uninvoiced_dollar_known", True) else billing.get("unpaid_total", 0)

    return {
        "has_unpaid_invoices": unpaid > 0,
        "has_uninvoiced_work": uninvoiced > 0,
        "has_exposure": exposure > 0,
        "risk_level": (
            "high" if exposure > 10000
            else "moderate" if exposure > 0
            else "none"
        ),
    }


def derive_workload_signals(workload: Dict[str, Any]) -> Dict[str, Any]:
    open_claims = workload.get("open_claims", 0)
    uninvoiced = workload.get("uninvoiced_count", 0)

    return {
        "open_claims": open_claims,
        "uninvoiced_count": uninvoiced,
        "load_level": (
            "high" if open_claims > 10 or uninvoiced > 10
            else "moderate" if open_claims > 0 or uninvoiced > 0
            else "low"
        ),
        "backlog": uninvoiced > 0,
    }

def compute_executive_snapshot(db: Session) -> Dict[str, Any]:
    """
    Single authoritative executive-level snapshot.
    This is the ONLY source Clarity should reason from.
    """

    metrics = compute_billing_metrics(db)

    billing = metrics.get("billing", {})
    workload = metrics.get("workload", {})

    # Normalize billing exposure
    unpaid_total = billing.get("unpaid_total", 0.0)
    uninvoiced_total = billing.get("uninvoiced_total", 0.0)
    total_exposure = unpaid_total + uninvoiced_total

    billing_snapshot = {
        "total_billed": billing.get("total_billed", 0.0),
        "unpaid_total": unpaid_total,
        "uninvoiced_total": uninvoiced_total,
        "total_exposure": total_exposure,
        "invoice_counts": billing.get("invoice_counts", {}),
        "confidence": billing.get("confidence", 0.5),
        "uninvoiced_dollar_known": billing.get("uninvoiced_dollar_known", True),
    }

    workload_snapshot = {
        "open_claims": workload.get("open_claims", 0),
        "total_claims": workload.get("total_claims", 0),
        "billable_count": workload.get("billable_count", 0),
        "uninvoiced_count": workload.get("uninvoiced_count", 0),
        "no_bill_count": workload.get("no_bill_count", 0),
        "confidence": workload.get("confidence", 0.5),
        "hours_total": workload.get("hours_total", 0.0),
        "miles_total": workload.get("miles_total", 0.0),
        "expense_total": workload.get("expense_total", 0.0),
    }

    hours_per_week = workload_snapshot["hours_total"] / 4 if workload_snapshot["hours_total"] else 0.0

    workload_snapshot["hours_per_week"] = hours_per_week

    assert set(billing_snapshot.keys()) >= {
        "total_billed", "unpaid_total", "uninvoiced_total", "total_exposure"
    }, "Billing snapshot missing required fields"

    billing_signals = derive_billing_signals(billing_snapshot)
    workload_signals = derive_workload_signals(workload_snapshot)

    return {
        "billing": billing_snapshot,
        "workload": workload_snapshot,
        "signals": {
            "billing": billing_signals,
            "workload": workload_signals,
        },
    }


def compute_billing_metrics(db: Session) -> Dict[str, Any]:
    """
    Computes authoritative billing + workload metrics directly from the DB.
    Dollar values are only populated when they are actually known.
    """

    # ---- Invoices ----
    invoices = db.query(Invoice).all()

    total_billed = Decimal("0.00")
    unpaid_total = Decimal("0.00")

    invoice_counts = {
        "total": 0,
        "paid": 0,
        "unpaid": 0,
        "draft": 0,
    }

    unpaid_invoice_ids = []

    for inv in invoices:
        invoice_counts["total"] += 1
        total_billed += inv.total or Decimal("0.00")

        status = (inv.status or "").lower()
        if status == "paid":
            invoice_counts["paid"] += 1
        elif status in ("unpaid", "sent"):
            invoice_counts["unpaid"] += 1
            unpaid_invoice_ids.append(inv.id)
            unpaid_total += inv.balance_due or inv.total or Decimal("0.00")
        elif status == "draft":
            invoice_counts["draft"] += 1

    # ---- Billables ----
    billables = db.query(BillableItem).all()

    uninvoiced_count = 0
    billable_count = 0
    no_bill_count = 0

    # We do NOT pretend we know dollar value unless rates are explicit
    uninvoiced_total = None
    uninvoiced_claim_ids = set()

    hours_total = Decimal("0.00")
    miles_total = Decimal("0.00")
    expense_total = Decimal("0.00")

    for b in billables:
        if b.activity_code == "NO BILL":
            no_bill_count += 1
            continue

        billable_count += 1

        if b.invoice_id is None:
            uninvoiced_count += 1
            if b.claim_id:
                uninvoiced_claim_ids.add(b.claim_id)

        qty = Decimal(str(b.quantity or 0))

        if b.activity_code in ("Travel", "MIL"):
            miles_total += qty
        elif b.activity_code == "Exp":
            expense_total += Decimal("0.00")
        else:
            hours_total += qty

    # ---- Claims / workload ----
    total_claims = db.query(func.count(Claim.id)).scalar() or 0
    open_claims = (
        db.query(func.count(Claim.id))
        .filter(Claim.is_closed == False)
        .scalar()
        or 0
    )

    return {
        "billing": {
            "total_billed": float(total_billed),
            "unpaid_total": float(unpaid_total),
            "uninvoiced_total": None,  # explicitly unknown
            "invoice_counts": invoice_counts,
            "unpaid_invoice_ids": unpaid_invoice_ids,
            "uninvoiced_claim_count": len(uninvoiced_claim_ids),
            "confidence": 1.0,
            "uninvoiced_dollar_known": False,
        },
        "workload": {
            "total_claims": total_claims,
            "open_claims": open_claims,
            "billable_count": billable_count,
            "uninvoiced_count": uninvoiced_count,
            "no_bill_count": no_bill_count,
            "hours_total": float(hours_total),
            "miles_total": float(miles_total),
            "expense_total": float(expense_total),
            "confidence": 1.0,
        },
    }