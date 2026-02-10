

from typing import Any, Dict, Optional

from ai.sources import (
    answer_claim_summary,
    answer_claim_count,
    answer_outstanding_billing,
    answer_billables_summary,
    answer_latest_report_work_status,
)

# -------------------------------------------------------------------
# Claim-scoped orchestration helpers
# -------------------------------------------------------------------

def handle_claim_summary(
    *,
    db: Any,
    ClaimModel: Any,
    claim_id: int,
    InvoiceModel: Any = None,
    BillableItemModel: Any = None,
) -> Dict[str, Any]:
    """
    Produce a structured summary for a single claim.
    """
    text = answer_claim_summary(
        db=db,
        ClaimModel=ClaimModel,
        claim_id=claim_id,
        InvoiceModel=InvoiceModel,
        BillableItemModel=BillableItemModel,
    )
    return {
        "text": text,
        "confidence": 1.0 if text and "not found" not in text.lower() else 0.5,
    }


def handle_claim_billing(
    *,
    db: Any,
    InvoiceModel: Any,
    claim_id: int,
) -> Dict[str, Any]:
    """
    Return outstanding billing information for a single claim.
    """
    billing = answer_outstanding_billing(
        db=db,
        InvoiceModel=InvoiceModel,
        claim_id=claim_id,
    )
    return {
        "count": billing.get("count", 0),
        "total": billing.get("total", 0.0),
        "label": billing.get("label", ""),
    }


def handle_claim_billables(
    *,
    db: Any,
    BillableItemModel: Any,
    claim_id: int,
) -> Dict[str, Any]:
    """
    Return a summary of billables for a single claim.
    """
    summary = answer_billables_summary(
        db=db,
        BillableItemModel=BillableItemModel,
        claim_id=claim_id,
    )
    return summary


def handle_claim_work_status(
    *,
    db: Any,
    ReportModel: Any,
    claim_id: int,
) -> Optional[str]:
    """
    Return the latest work status text for a claim, if available.
    """
    return answer_latest_report_work_status(
        db=db,
        ReportModel=ReportModel,
        claim_id=claim_id,
    )


def handle_claim_count(
    *,
    db: Any,
    ClaimModel: Any,
    scope: str,
) -> Dict[str, Any]:
    """
    Return claim counts scoped to open / closed / both.
    """
    count, label = answer_claim_count(
        scope=scope,
        db=db,
        ClaimModel=ClaimModel,
    )
    return {
        "count": count,
        "label": label,
    }