from typing import Dict, Tuple, Optional
from .intents_registry import get_intent

# -------------------------------------------------------------------
# Intent detection helpers
# -------------------------------------------------------------------

def detect_intent(question: str, context: Dict[str, any]):
    """
    Determine the user's intent and extract intent-specific slots.
    Returns (intent_name, intent_data).
    """
    q = (question or "").strip().lower()
    intent_data: Dict[str, any] = {}

    # -----------------------------
    # Billing totals (must win early)
    # -----------------------------
    billing_due_keywords = [
        "outstanding",
        "owed",
        "unpaid",
        "receivable",
        "due",
        "balance",
    ]
    billing_subject_keywords = [
        "billing",
        "invoice",
        "invoices",
        "accounts receivable",
        "a/r",
    ]

    # Any billing/invoice question that mentions due/unpaid money
    if any(k in q for k in billing_due_keywords) and any(
        k in q for k in billing_subject_keywords
    ):
        return get_intent("billing_outstanding_total"), intent_data

    # Explicit money phrasing should also trigger totals
    if any(
        k in q
        for k in [
            "how much",
            "total",
            "amount",
            "$",
            "dollars",
        ]
    ) and any(k in q for k in billing_subject_keywords) and not any(
        k in q for k in ["tell me about", "overview", "summary", "status"]
    ):
        return get_intent("billing_outstanding_total"), intent_data

    # -----------------------------
    # System health (must win early)
    # -----------------------------
    if any(
        k in q
        for k in [
            "health",
            "server",
            "disk",
            "storage",
            "backup",
            "uptime",
            "memory",
            "cpu",
            "temperature",
            "temp",
        ]
    ):
        return get_intent("system_health"), intent_data

    # -----------------------------
    # Workload / capacity analysis
    # -----------------------------
    if any(
        k in q
        for k in [
            "workload",
            "capacity",
            "busy",
            "too much work",
            "how am i doing",
            "billing load",
            "hours per day",
            "hours per week",
        ]
    ):
        return get_intent("workload_overview"), intent_data

    # -----------------------------
    # Claim count / system overview
    # -----------------------------
    if any(k in q for k in ["how many claims", "number of claims", "count claims"]):
        intent_data["scope"] = _extract_scope(q)
        return get_intent("claim_count"), intent_data

    if any(
        k in q
        for k in [
            "system overview",
            "system snapshot",
            "overall status",
            "big picture",
            "how is everything",
        ]
    ) and "health" not in q:
        return get_intent("system_overview"), intent_data

    # -----------------------------
    # Claim-specific summaries
    # -----------------------------
    if "summarize this claim" in q or "summary of this claim" in q:
        return get_intent("claim_summary"), intent_data

    if "invoices" in q and "how many" in q:
        return get_intent("invoice_count"), intent_data

    # -----------------------------
    # Billables
    # -----------------------------
    if "uninvoiced billables" in q:
        return get_intent("uninvoiced_billables"), intent_data

    if "summarize billables" in q or "billables summary" in q:
        return get_intent("billables_summary"), intent_data

    # -----------------------------
    # Reports
    # -----------------------------
    if "work status" in q or "latest report" in q:
        return get_intent("latest_report_work_status"), intent_data

    if any(k in q for k in ["billing", "invoice", "invoices"]) and any(
        k in q for k in ["tell me about", "overview", "summary", "status"]
    ):
        return None, intent_data

    return None, intent_data


def _extract_scope(q: str) -> str:
    if "closed" in q:
        return "closed"
    if "open" in q:
        return "open"
    if "both" in q or "all" in q:
        return "both"
    return "open"
