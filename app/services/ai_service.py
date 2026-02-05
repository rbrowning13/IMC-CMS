from typing import Any, Dict, List, Optional, Tuple, Iterable, Sequence
# -----------------------------------------------------------------------------
# Deterministic Clarity routing (fast-paths that do NOT use the LLM)
# -----------------------------------------------------------------------------

def _qnorm(question: str) -> str:
    return (question or "").strip().lower()


def detect_metric_query(question: str) -> str | None:
    """Legacy metric detection (kept for backwards compatibility)."""
    q = _qnorm(question)
    if (
        "how many claims" in q
        or "number of claims" in q
        or "how many open claims" in q
        or "how many closed claims" in q
        or "total claims" in q
    ):
        return "claim_count"
    if "how many invoices" in q or "number of invoices" in q:
        return "invoice_count"
    if "total billable" in q or "total hours" in q:
        return "billable_totals"
    return None


def detect_deterministic_intent(question: str) -> str | None:
    """Return a deterministic intent label when we can answer WITHOUT an LLM."""
    q = _qnorm(question)

    # Smalltalk / acknowledgements
    if q in {"thanks", "thank you", "thx", "ty", "ok", "okay", "got it", "cool", "sweet", "awesome"}:
        return "smalltalk_ack"
    if any(q.startswith(k) for k in ["thanks ", "thank you ", "thx ", "ty ", "ok ", "okay ", "got it "]):
        return "smalltalk_ack"

    # Capability / help
    if any(k in q for k in [
        "what can you do", "capabilities", "help", "how do i ask", "what questions",
        "examples", "commands",
    ]):
        return "capabilities"

    # System-level claim counts / lists (no claim_id required)
    if any(k in q for k in [
        "how many claims", "number of claims", "total claims",
        "how many open claims", "open claims",
        "how many closed claims", "closed claims",
    ]):
        if any(k in q for k in ["how many", "number of", "count"]):
            if "open" in q and "closed" in q:
                return "claim_count_both"
            if "open" in q:
                return "claim_count_open"
            if "closed" in q:
                return "claim_count_closed"
            return "claim_count"

    if any(k in q for k in [
        "list claims", "show claims", "all claims", "my claims",
        "tell me about my claims", "claims overview",
    ]):
        if "open" in q and "closed" in q:
            return "claim_list_both"
        if "open" in q:
            return "claim_list_open"
        if "closed" in q:
            return "claim_list_closed"
        return "claim_list_both"

    if q in {"open", "closed", "both"}:
        return "claim_scope_followup"

    # Claim overview / summary
    if any(k in q for k in [
        "summarize this claim", "summarize the claim", "claim summary", "summary of this claim",
        "overview", "what's going on", "what is going on", "status of this claim",
    ]):
        return "claim_summary"

    # Billables comparison/typicality/relative queries (deterministic)
    billable_terms = ["billables", "billable items", "billing items", "billable", "billing", "hours", "units", "miles", "expenses"]
    compare_keywords = [
        "compare", "compared", "typical", "average", "vs", "versus", "unusual", "outlier", "normal", "relative"
    ]
    if any(k in q for k in compare_keywords) and any(k in q for k in billable_terms):
        is_global = any(k in q for k in [
            "across all", "across all claims", "overall", "system", "all claims"
        ])
        if is_global:
            return "billables_compare_system"
        else:
            return "billables_compare_claim"

    # Billables
    if any(k in q for k in billable_terms):
        # System-wide phrasing
        is_global = any(k in q for k in [
            "across all claims", "across all", "overall", "system", "system-wide", "in the system",
            "all claims", "every claim",
        ])
        is_uninvoiced = any(k in q for k in ["uninvoiced", "not invoiced", "unbilled", "not billed", "un-invoiced", "not yet invoiced"])

        if is_global:
            if is_uninvoiced:
                return "global_billables_uninvoiced"
            if any(k in q for k in ["summary", "totals", "total", "how many", "count", "number of"]):
                return "global_billables_summary"
            return "global_billables_summary"

        # Claim-scoped (requires claim_id)
        if is_uninvoiced:
            return "billables_uninvoiced"
        if any(k in q for k in ["summary", "totals", "total", "how many", "count", "number of"]):
            return "billables_summary"
        return "billables_list"

    # Reports
    if "last dos" in q or "latest dos" in q or "most recent dos" in q:
        return "latest_dos"
    if any(k in q for k in ["latest report", "most recent report", "last report"]):
        if "work status" in q:
            return "latest_report_work_status"
        if any(k in q for k in ["status", "treatment", "plan"]):
            return "latest_report_status_plan"
        return "latest_report_summary"

    # Invoices
    if any(k in q for k in ["invoice", "invoices"]):
        if any(k in q for k in ["how many", "count", "number of"]):
            return "invoice_count"
        return "invoice_list"

    # System billing / A/R totals (no claim_id required)
    if any(k in q for k in [
        "outstanding billing", "accounts receivable", "a/r", "ar total",
        "how much outstanding", "total outstanding",
    ]):
        return "billing_outstanding_total"

    if any(k in q for k in [
        "total billing", "total billed", "how much billing", "how much have i billed",
        "billing total",
    ]):
        return "billing_total"

    return None


def _format_kv_line(label: str, value: Any) -> str:
    if value is None or value == "":
        return ""
    return f"{label}: {value}"


def _format_billable_line(b: Dict[str, Any]) -> str:
    """Stable, human-readable billable line (best-effort)."""
    sd = b.get("service_date") or ""
    code = b.get("activity_code") or b.get("activity") or ""
    qty = b.get("quantity")
    desc = (b.get("description") or b.get("notes") or "").strip()
    if len(desc) > 140:
        desc = desc[:140].rstrip() + "…"

    parts: List[str] = []
    if sd:
        parts.append(str(sd))
    if code:
        parts.append(str(code))
    if qty is not None and qty != "":
        parts.append(f"qty={qty}")

    inv = b.get("invoice_id") or b.get("invoice") or b.get("linked_invoice")
    if inv:
        parts.append(f"invoice={inv}")

    if desc:
        parts.append(desc)

    return " — ".join(parts).strip(" —")



def _billable_is_invoiced(b: Dict[str, Any]) -> bool:
    """Heuristic: treat billable as invoiced if any invoice linkage flag/value exists."""
    for k in ("invoice_id", "invoice", "invoice_number", "linked_invoice", "invoiced"):
        v = b.get(k)
        if isinstance(v, bool) and v:
            return True
        if v not in (None, "", False):
            return True
    return False


# -----------------------------------------------------------------------------
# Deterministic billable aggregation - authoritative numeric rollups
# -----------------------------------------------------------------------------

def aggregate_billables(billables: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Deterministically aggregate billable items.

    This is the SINGLE source of truth for numeric rollups.
    Do NOT re-compute totals elsewhere.
    """
    totals = {
        "billable_count": 0,
        "no_bill_count": 0,
        "uninvoiced_count": 0,
        "hours_total": 0.0,
        "miles_total": 0.0,
        "expense_total": 0.0,
    }

    for b in billables or []:
        if not isinstance(b, dict):
            continue

        totals["billable_count"] += 1

        activity = (b.get("activity_code") or b.get("activity") or "").upper()
        qty = b.get("quantity")

        # NO BILL handling
        if activity == "NO BILL":
            totals["no_bill_count"] += 1
            continue

        # Uninvoiced handling
        if not _billable_is_invoiced(b):
            totals["uninvoiced_count"] += 1

        # Quantity rollups (best-effort, deterministic)
        try:
            q = float(qty)
        except Exception:
            continue

        if activity in {"HR", "HRS", "HOUR", "HOURS"}:
            totals["hours_total"] += q
        elif activity in {"MIL", "MILE", "MILES"}:
            totals["miles_total"] += q
        elif activity in {"EXP", "EXPENSE", "EXPENSES"}:
            totals["expense_total"] += q

    # Normalize floats for stable display
    totals["hours_total"] = round(totals["hours_total"], 2)
    totals["miles_total"] = round(totals["miles_total"], 2)
    totals["expense_total"] = round(totals["expense_total"], 2)

    return totals


# --------------------------------------------------------------------------
# Deterministic billables comparison helpers (inserted)
# --------------------------------------------------------------------------

def _safe_float(x: Any) -> float:
    try:
        if x in (None, "", False):
            return 0.0
        return float(x)
    except Exception:
        return 0.0


def _pct_delta(a: float, b: float) -> float | None:
    """Return percent delta of a vs b, or None if b is ~0."""
    if b is None:
        return None
    if abs(b) < 1e-9:
        return None
    return ((a - b) / b) * 100.0


def _compare_billable_totals(*, claim_totals: Dict[str, Any], system_totals: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministic comparison of claim vs system totals.
    Returns a structured comparison dict suitable for short summaries.
    """
    out: Dict[str, Any] = {"deltas": {}, "claim": {}, "system": {}}

    for k in ("hours_total", "miles_total", "expense_total", "billable_count", "uninvoiced_count"):
        out["claim"][k] = claim_totals.get(k, 0)
        out["system"][k] = system_totals.get(k, 0)

    for k in ("hours_total", "miles_total", "expense_total"):
        a = _safe_float(claim_totals.get(k))
        b = _safe_float(system_totals.get(k))
        out["deltas"][k] = {
            "abs": round(a - b, 2),
            "pct": (round(_pct_delta(a, b), 1) if _pct_delta(a, b) is not None else None),
        }

    # Counts: pct delta isn't meaningful when system is huge; keep abs only.
    for k in ("billable_count", "uninvoiced_count"):
        a = _safe_float(claim_totals.get(k))
        b = _safe_float(system_totals.get(k))
        out["deltas"][k] = {"abs": int(a - b), "pct": None}

    return out


def _system_billables_rollup() -> Dict[str, Any]:
    """
    Deterministic rollup across ALL billables in the database.
    This is used for 'system' comparisons and must not call the LLM.
    """
    from app.models import BillableItem
    from app.extensions import db

    rows = db.session.query(BillableItem).all()

    # Convert ORM objects to the dict shape aggregate_billables expects.
    items: List[Dict[str, Any]] = []
    for b in rows:
        items.append({
            "activity_code": getattr(b, "activity_code", None),
            "activity": getattr(b, "activity", None),
            "quantity": getattr(b, "quantity", None),
            "invoice_id": getattr(b, "invoice_id", None) if hasattr(b, "invoice_id") else None,
            "invoice_number": getattr(b, "invoice_number", None) if hasattr(b, "invoice_number") else None,
            "invoiced": getattr(b, "invoiced", None) if hasattr(b, "invoiced") else None,
            "is_invoiced": getattr(b, "is_invoiced", None) if hasattr(b, "is_invoiced") else None,
        })

    return aggregate_billables(items)


def _claim_billables_rollup_from_context(context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministic rollup for the current claim using already-loaded context billables when possible.
    Falls back to retrieval_context if billables weren't attached.
    """
    billables = context.get("billables")
    if isinstance(billables, list) and billables:
        return aggregate_billables(billables)

    claim_id = context.get("claim_id")
    if not claim_id:
        return aggregate_billables([])

    # Best-effort: ask retrieval_context for billables (does not use LLM)
    try:
        from app.ai.retrieval import retrieve_context as _retrieve_context
        structured = _retrieve_context(
            claim_id=claim_id,
            report=None,
            max_billables=int(context.get("max_billables") or 200),
            max_reports=int(context.get("max_reports") or 12),
        )
        billables2 = (structured or {}).get("billables") or []
        return aggregate_billables(billables2)
    except Exception:
        return aggregate_billables([])


def _deterministic_capabilities() -> Dict[str, Any]:
    """What Clarity can do today (grounded in current backend wiring)."""
    return {
        "read": [
            "Summarize a claim (safe, non-identifying)",
            "Summarize billables (totals; recent activity)",
            "List billables (optionally uninvoiced)",
            "Show latest DOS window",
            "Summarize latest report (or just work status / status-plan)",
            "List invoices (if invoice data is present in context)",
            "Count claims / count invoices",
        ],
        "draft": [
            "Draft report fields (status/treatment plan, work status, case management plan, etc.)",
            "Rewrite/shorten/expand an existing field value (no auto-save)",
        ],
        "notes": [
            "Answers are grounded in retrieved claim context; if context is missing a field, Clarity will say so.",
            "PHI is intentionally minimized in prompts (no claimant name/DOB/claim #/contact identifiers).",
        ],
    }
"""AI service layer for Impact Medical CMS.

This module is intentionally split from routes/templates so we can:
- Centralize privacy/PHI redaction rules
- Assemble context (prior reports + billables timeline)
- Match Gina's tone/style using historical report writing
- Provide a single, testable API for AI-assisted drafting

IMPORTANT:
- This module does NOT store AI output automatically.
- LLM calls are behind `call_llm()`.

All entry points should enforce Settings toggles:
- settings.ai_enabled
- settings.ai_allow_provider_names

AI ARCHITECTURE NOTE
--------------------
This service acts as the orchestration layer for AI features.

Responsibilities:
- Enforce Settings + environment kill-switches
- Assemble structured context (claims, reports, billables)
- Apply privacy/redaction rules
- Build prompts and guardrails
- Delegate execution to an LLM backend

Execution backends (local vs remote), retrieval, embeddings, and permissions
are intentionally delegated to modules under app.ai.* so they can evolve
without changing routes or templates.

Do NOT put vector DB logic or model-specific code directly in this file.
"""
from dataclasses import dataclass
from datetime import date, datetime
import json
import os

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Modular AI stack imports
from app.ai.llm import call_llm, call_llm_with_meta
from app.ai.llm import get_active_llm_info
from app.ai.prompts import build_prompt
from importlib import import_module
from types import ModuleType

# -----------------------------------------------------------------------------
# Optional Chat Engine integration (persistent, multi-turn)
# -----------------------------------------------------------------------------

def _try_chat_engine(*, question: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Best-effort delegation to app.ai.chat_engine.

    We intentionally keep this loose/defensive so ai_service.py can integrate
    with evolving chat_engine APIs without hard coupling.

    Expected return (preferred): Clarity-shaped dict with at least `answer`.

    If chat_engine is not present or cannot handle the request, returns None.
    """
    try:
        mod: ModuleType = import_module("app.ai.chat_engine")
    except Exception:
        return None

    # Try a few likely entry points (keep ordered; first match wins)
    candidates = [
        "chat_respond",
        "respond",
        "handle_chat",
        "handle_message",
        "ask",
        "run",
    ]

    fn = None
    for name in candidates:
        fn = getattr(mod, name, None)
        if callable(fn):
            break
    if not callable(fn):
        return None

    try:
        # Prefer keyword calling; if the chat_engine signature differs, fall back.
        try:
            res = fn(question=question, context=context)
        except TypeError:
            res = fn(question, context)
    except Exception:
        return None

    # A "not handled" convention is allowed.
    if isinstance(res, dict) and res.get("handled") is False:
        return None

    # If the engine returns a plain string, wrap it.
    if isinstance(res, str):
        return {
            "answer": res,
            "citations": [],
            "is_guess": True,
            "confidence": None,
            "model_source": "chat_engine",
            "model": None,
            "local_only": True,
            "answer_mode": "chat",
        }

    # If it already returns a Clarity-shaped dict, pass it through.
    if isinstance(res, dict) and ("answer" in res or "citations" in res or "answer_mode" in res):
        return res

    return None

# -----------------------------------------------------------------------------
# Public universal Clarity entry point
# -----------------------------------------------------------------------------

def _context_to_prompt_text(ctx: Dict[str, Any]) -> str:
    """Serialize structured context to a stable prompt string."""
    try:
        return json.dumps(ctx, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return str(ctx)

def ask(*, question: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generic AI entry point.

    This is used by Clarity and any callers expecting ai_service.ask().
    It delegates to ask_clarity() and returns its result unchanged.
    """
    return ask_clarity(
        question=question,
        context=context,
    )


def generate(prompt: str) -> Dict[str, Any]:
    """
    Clarity generation entry point.
    Expects: ai_service.generate(prompt=...)
    Returns a dict with keys:
        { "text", "citations", "is_guess", "confidence", "model_source", "model" }
    """
    result = call_llm_with_meta(prompt)
    normalized = _normalize_llm_result(result)
    # Ensure text is always a plain string and all required keys are present
    llm_info = get_active_llm_info()
    return {
        "text": normalized.get("text", ""),
        "citations": normalized.get("citations", []),
        "is_guess": normalized.get("is_guess", False),
        "confidence": normalized.get("confidence"),
        "model_source": llm_info.get("backend"),
        "model": llm_info.get("model"),
        "answer_mode": normalized.get("answer_mode"),
    }

def ask_clarity(question: str, context: dict) -> Dict[str, Any]:
    # ------------------------------------------------------------------
    # System-level invoice helpers (best-effort across schema variants)
    # ------------------------------------------------------------------
    def _invoice_status(inv: Any) -> str:
        for attr in ("status", "invoice_status", "state"):
            if hasattr(inv, attr):
                v = getattr(inv, attr)
                if v not in (None, ""):
                    return str(v).strip()
        return ""

    def _invoice_is_paid(inv: Any) -> bool:
        for attr in ("is_paid", "paid"):
            if hasattr(inv, attr):
                try:
                    if bool(getattr(inv, attr)):
                        return True
                except Exception:
                    pass
        s = _invoice_status(inv).lower()
        return s in {"paid", "complete", "completed", "closed"}

    def _invoice_is_draft(inv: Any) -> bool:
        s = _invoice_status(inv).lower()
        return s in {"draft", "in progress", "in_progress"}

    def _invoice_is_unpaid(inv: Any) -> bool:
        # Treat anything not paid as unpaid; draft counts as unpaid too.
        return not _invoice_is_paid(inv)

    def _looks_like_missing_context(ans: str) -> bool:
        a = (ans or "").strip().lower()
        if not a:
            return True
        needles = [
            "context is empty",
            "provided context does not contain",
            "does not contain any",
            "no relevant facts",
            "returned an empty answer",
            "i pulled the claim context successfully, but",
            "lacks specific",
        ]
        return any(n in a for n in needles)
    """
    Universal Clarity entry point.
    Guarantees:
    - Local-only inference (no remote LLM)
    - Explicit model provenance in every response
    - No persistence / no side effects
    - Uses Clarity-compatible retrieval
    """
    # ------------------------------------------------------------------
    # Pending-intent follow-up resolution (clarifying questions)
    # ------------------------------------------------------------------
    pending = context.get("pending_intent") or context.get("pendingIntent")
    qn = _qnorm(question)

    # Resolve scope-only replies ("open" / "closed" / "both") even if the frontend
    # did not persist pending_intent across turns.
    if qn in {"open", "opened"}:
        question = "how many open claims"
    elif qn in {"closed", "close", "closed only"}:
        question = "how many closed claims"
    elif qn in {"both", "all", "total"}:
        question = "how many open and closed claims"

    # Clear pending intent after resolution (if present)
    if pending:
        context = {**context}
        context.pop("pending_intent", None)
        context.pop("pendingIntent", None)

    # Chat engine first (persistent, multi-turn). If it can handle the request,
    # return immediately. Otherwise, fall back to deterministic routing + LLM.
    chat_resp = _try_chat_engine(question=question, context=context or {})
    if isinstance(chat_resp, dict):
        return chat_resp
    if _ai_globally_disabled():
        raise RuntimeError("AI is globally disabled (OPENAI_DISABLED).")

    # Fast-path metric handler before retrieval
    metric = detect_metric_query(question)
    if metric:
        from app.models import Claim, Invoice
        from app.extensions import db

        if metric == "claim_count":
            # Best-effort status handling across schema variants.
            def _claim_status(c: Any) -> str:
                for attr in ("status", "claim_status", "state", "lifecycle"):
                    if hasattr(c, attr):
                        v = getattr(c, attr)
                        if v not in (None, ""):
                            return str(v).strip()
                return ""

            def _is_open(c: Any) -> bool:
                s = _claim_status(c).lower()
                if s in {"open", "active", "in progress", "in_progress"}:
                    return True
                if s in {"closed", "inactive", "complete", "completed"}:
                    return False
                return True

            claims = db.session.query(Claim).all()

            qn = _qnorm(question)
            wants_open = "open" in qn
            wants_closed = "closed" in qn

            open_count = sum(1 for c in claims if _is_open(c))
            closed_count = len(claims) - open_count
            total_count = len(claims)

            # Default behavior: when the user asks "how many claims" without a scope,
            # answer with BOTH + total (this avoids brittle multi-turn follow-ups).
            if wants_open and not wants_closed:
                msg = f"There are {open_count} open claims."
            elif wants_closed and not wants_open:
                msg = f"There are {closed_count} closed claims."
            else:
                msg = f"There are {open_count} open and {closed_count} closed claims ({total_count} total)."

            return {
                "answer": msg,
                "citations": [],
                "is_guess": False,
                "confidence": 1.0,
                "model_source": "system",
                "model": None,
                "local_only": True,
                "answer_mode": "brief",
            }

        if metric == "invoice_count":
            count = db.session.query(Invoice).count()
            return {
                "answer": f"There are {count} invoices.",
                "citations": [],
                "is_guess": False,
                "confidence": 1.0,
                "model_source": "system",
                "model": None,
                "local_only": True,
                "answer_mode": "brief",
            }
        # For billable_totals, fall through to normal retrieval (not handled here)

    # ---------------------------------------------------------------------
    # Deterministic system-level billing totals (no claim_id required)
    # ---------------------------------------------------------------------
    det_intent = detect_deterministic_intent(question)
    # Smalltalk fast-path (keeps chat from acting like a report generator)
    if det_intent == "smalltalk_ack":
        return {
            "answer": "You got it. Want unpaid invoices, uninvoiced billables, or claims next?",
            "citations": [],
            "is_guess": False,
            "confidence": 1.0,
            "model_source": "system",
            "model": None,
            "local_only": True,
            "answer_mode": "brief",
        }

    if det_intent in {"billing_outstanding_total", "billing_total"}:
        from app.models import Invoice
        from app.extensions import db

        def _invoice_total(inv: Any) -> float:
            # Best-effort across schema variants
            for attr in ("total", "total_amount", "amount_total", "grand_total", "balance", "amount"):
                if hasattr(inv, attr):
                    v = getattr(inv, attr)
                    if v not in (None, ""):
                        try:
                            return float(v)
                        except Exception:
                            pass
            return 0.0

        def _is_paid(inv: Any) -> bool:
            # Best-effort: treat status/paid flags
            for attr in ("is_paid", "paid"):
                if hasattr(inv, attr):
                    try:
                        if bool(getattr(inv, attr)):
                            return True
                    except Exception:
                        pass
            status = (getattr(inv, "status", "") or "").strip().lower()
            return status in {"paid", "complete", "completed", "closed"}

        invoices = db.session.query(Invoice).all()
        if det_intent == "billing_total":
            total = sum(_invoice_total(inv) for inv in invoices)
            return {
                "answer": f"Total billed (all invoices): ${total:,.2f}",
                "citations": [],
                "is_guess": False,
                "confidence": 1.0,
                "model_source": "system",
                "model": None,
                "local_only": True,
                "answer_mode": "brief",
            }

        # Outstanding = sum of unpaid invoices
        outstanding = sum(_invoice_total(inv) for inv in invoices if not _is_paid(inv))
        return {
            "answer": f"Outstanding billing (unpaid invoices): ${outstanding:,.2f}",
            "citations": [],
            "is_guess": False,
            "confidence": 1.0,
            "model_source": "system",
            "model": None,
            "local_only": True,
            "answer_mode": "brief",
        }

    # ---------------------------------------------------------------------
    # Deterministic system-level invoice counts (no claim_id required)
    # ---------------------------------------------------------------------
    if det_intent == "invoice_count":
        from app.models import Invoice
        from app.extensions import db

        invoices = db.session.query(Invoice).all()
        total = len(invoices)
        paid = sum(1 for inv in invoices if _invoice_is_paid(inv))
        draft = sum(1 for inv in invoices if _invoice_is_draft(inv))
        unpaid = total - paid

        qn = _qnorm(question)
        wants_paid = any(k in qn for k in ["paid", "settled"]) 
        wants_unpaid = any(k in qn for k in ["unpaid", "outstanding", "open", "due"]) 
        wants_draft = "draft" in qn

        if wants_paid and not (wants_unpaid or wants_draft):
            msg = f"There are {paid} paid invoices."
        elif wants_draft and not (wants_paid or wants_unpaid):
            msg = f"There are {draft} draft invoices."
        elif wants_unpaid and not (wants_paid or wants_draft):
            msg = f"There are {unpaid} unpaid invoices."
        else:
            # Default: give a useful breakdown when the user didn't specify.
            msg = f"There are {total} invoices ({paid} paid, {unpaid} unpaid, {draft} draft)."

        return {
            "answer": msg,
            "citations": [],
            "is_guess": False,
            "confidence": 1.0,
            "model_source": "system",
            "model": None,
            "local_only": True,
            "answer_mode": "brief",
        }

    # ---------------------------------------------------------------------
    # Deterministic system-level billables (no claim_id required)
    # ---------------------------------------------------------------------
    if det_intent in {"global_billables_uninvoiced", "global_billables_summary"}:
        from app.models import BillableItem
        from app.extensions import db

        # Best-effort invoice linkage detection across schema variants
        def _is_uninvoiced_row(b: Any) -> bool:
            # Common direct FK fields
            for attr in ("invoice_id", "invoice", "linked_invoice_id"):
                if hasattr(b, attr):
                    v = getattr(b, attr)
                    if v not in (None, "", False):
                        return False
            # Some schemas store an invoice number string
            if hasattr(b, "invoice_number"):
                v = getattr(b, "invoice_number")
                if v not in (None, "", False):
                    return False
            # Boolean flags
            for attr in ("is_invoiced", "invoiced"):
                if hasattr(b, attr):
                    try:
                        if bool(getattr(b, attr)):
                            return False
                    except Exception:
                        pass
            return True

        q = db.session.query(BillableItem)

        if det_intent == "global_billables_uninvoiced":
            rows = [b for b in q.all() if _is_uninvoiced_row(b)]
        else:
            rows = q.all()

        count = len(rows)

        # Optional numeric rollups (best-effort)
        qty_sum = 0.0
        qty_ok = False
        for b in rows:
            if hasattr(b, "quantity"):
                v = getattr(b, "quantity")
                try:
                    if v not in (None, ""):
                        qty_sum += float(v)
                        qty_ok = True
                except Exception:
                    pass

        if det_intent == "global_billables_uninvoiced":
            if qty_ok:
                msg = f"Uninvoiced billable items (all claims): {count} (quantity sum: {qty_sum:,.2f})"
            else:
                msg = f"Uninvoiced billable items (all claims): {count}"
        else:
            if qty_ok:
                msg = f"Billable items (all claims): {count} (quantity sum: {qty_sum:,.2f})"
            else:
                msg = f"Billable items (all claims): {count}"

        return {
            "answer": msg,
            "citations": [],
            "is_guess": False,
            "confidence": 1.0,
            "model_source": "system",
            "model": None,
            "local_only": True,
            "answer_mode": "brief",
        }

    # Deterministic intent (only used after we have claim context; some intents don't require it)
    # det_intent already set above to avoid recomputing
    ql = _qnorm(question)

    # Always use this module's retrieve() for Clarity context
    claim_id = context.get("claim_id")
    retrieval_data = None
    if claim_id:
        retrieval_data = retrieve(context=context, question=question)
        context = {
            **context,
            "facts": retrieval_data.get("facts", []),
            "chunks": retrieval_data.get("chunks", []),
            "sources": retrieval_data.get("sources", []),
        }
    else:
        context = {**context}

    # Optional: enrich with structured claim context for deterministic answers.
    # This is best-effort and must never crash the endpoint.
    try:
        from app.ai.retrieval import retrieve_context as _retrieve_context

        structured = _retrieve_context(
            claim_id=claim_id,
            report=None,
            max_billables=int(context.get("max_billables") or 80),
            max_reports=int(context.get("max_reports") or 12),
        )

        if isinstance(structured, dict):
            # Merge known, high-value structured keys if present.
            # Keep legacy aliases (summary -> billable_summary).
            preferred_keys = (
                # Existing
                "header",
                "current_report",
                "current_report_fields",
                "prior_reports",
                "billables",
                "summary",
                # New: core data types + relationships (if retrieval provides them)
                "claim",
                "carrier",
                "employer",
                "providers",
                "contacts",
                "invoices",
                "invoice_summary",
                "reports",
                "report_summaries",
                "latest_report",
            )

            for k in preferred_keys:
                if structured.get(k) is not None:
                    # Preserve legacy naming for billable summary
                    if k == "summary":
                        context["billable_summary"] = structured.get("summary")
                    elif k == "billables":
                        context["billables"] = structured.get("billables")
                    else:
                        context[k] = structured.get(k)

            # Also keep a namespaced copy of the full structured payload for the LLM.
            # This avoids losing useful new fields as retrieval evolves.
            if structured:
                context.setdefault("structured", structured)

    except Exception:
        # Ignore any retrieval issues; LLM path can still run with chunks/sources.
        pass


    # ---------------------------------------------------------------------
    # Deterministic router (no LLM) — stable, grounded answers
    # ---------------------------------------------------------------------

    # Capabilities/help can be answered without a claim_id.
    if det_intent == "capabilities":
        caps = _deterministic_capabilities()
        return {
            "answer": json.dumps(caps, ensure_ascii=False, indent=2),
            "citations": [],
            "is_guess": False,
            "confidence": 1.0,
            "model_source": "system",
            "model": None,
            "local_only": True,
            "answer_mode": "debug",
        }

    # Everything below this line benefits from claim context.
    if claim_id:
        # Claim summary
        if det_intent == "claim_summary":
            summary_text = _deterministic_claim_summary(context)
            if summary_text:
                resp = {
                    "answer": summary_text,
                    "citations": [],
                    "is_guess": False,
                    "confidence": 1.0,
                    "model_source": "system",
                    "model": None,
                    "local_only": True,
                    "answer_mode": "summary",
                }
                if _env_truthy("FLORENCE_DEBUG", "0"):
                    resp["diagnostics"] = debug_retrieval_snapshot(context)
                    resp["context_keys"] = sorted(context.keys())
                return resp

        # Latest DOS window
        if det_intent == "latest_dos":
            lr = _get_latest_report_from_context(context)
            cr = context.get("current_report") or {}
            header = context.get("header") or {}
            dos_start = (lr.get("dos_start") if isinstance(lr, dict) else None) or cr.get("dos_start") or header.get("dos_start")
            dos_end = (lr.get("dos_end") if isinstance(lr, dict) else None) or cr.get("dos_end") or header.get("dos_end")
            if dos_start or dos_end:
                return {
                    "answer": f"Latest DOS: {dos_start or '?'} → {dos_end or '?'}",
                    "citations": [],
                    "is_guess": False,
                    "confidence": 1.0,
                    "model_source": "system",
                    "model": None,
                    "local_only": True,
                    "answer_mode": "brief",
                }

        # Latest report narrative (best-effort; prefers retrieval-provided latest_report)
        if det_intent and det_intent.startswith("latest_report"):
            r0 = _get_latest_report_from_context(context)

            if isinstance(r0, dict) and r0:
                if det_intent == "latest_report_work_status":
                    ws = (r0.get("work_status") or "").strip()
                    if ws:
                        return {
                            "answer": ws,
                            "citations": [],
                            "is_guess": False,
                            "confidence": 1.0,
                            "model_source": "system",
                            "model": None,
                            "local_only": True,
                            "answer_mode": "brief",
                        }

                if det_intent == "latest_report_status_plan":
                    stp = (r0.get("status_treatment_plan") or "").strip()
                    if stp:
                        return {
                            "answer": stp,
                            "citations": [],
                            "is_guess": False,
                            "confidence": 1.0,
                            "model_source": "system",
                            "model": None,
                            "local_only": True,
                            "answer_mode": "brief",
                        }

                # Generic latest report summary (compact)
                bits: List[str] = []
                for key, label in [
                    ("report_type", "Type"),
                    ("dos_start", "DOS start"),
                    ("dos_end", "DOS end"),
                    ("next_report_due", "Next due"),
                ]:
                    line = _format_kv_line(label, r0.get(key))
                    if line:
                        bits.append(line)

                stp = (r0.get("status_treatment_plan") or "").strip()
                ws = (r0.get("work_status") or "").strip()
                if stp:
                    stp = stp[:600] + ("…" if len(stp) > 600 else "")
                    bits.append("Status/Plan: " + stp)
                if ws:
                    ws = ws[:400] + ("…" if len(ws) > 400 else "")
                    bits.append("Work status: " + ws)

                if bits:
                    return {
                        "answer": "\n".join(bits),
                        "citations": [],
                        "is_guess": False,
                        "confidence": 1.0,
                        "model_source": "system",
                        "model": None,
                        "local_only": True,
                        "answer_mode": "summary",
                    }

        # Billables comparison handler (deterministic)
        if det_intent == "billables_compare_claim" and claim_id:
            claim_totals = _claim_billables_rollup_from_context(context)
            system_totals = _system_billables_rollup()
            cmp = _compare_billable_totals(claim_totals=claim_totals, system_totals=system_totals)
            # Compose a short, single-paragraph answer:
            h = claim_totals.get("hours_total", 0.0)
            m = claim_totals.get("miles_total", 0.0)
            e = claim_totals.get("expense_total", 0.0)
            # Percent deltas
            pd_h = cmp["deltas"]["hours_total"]["pct"]
            pd_m = cmp["deltas"]["miles_total"]["pct"]
            pd_e = cmp["deltas"]["expense_total"]["pct"]
            def _pct_str(p):
                if p is None:
                    return ""
                sign = "+" if p > 0 else ""
                return f" ({sign}{p:.1f}%)"
            answer_parts = [
                f"This claim has {h} hours, {m} miles, and ${e} expenses.",
                "Compared to your overall system totals, that's:",
            ]
            subbits = []
            if pd_h is not None:
                subbits.append(f"hours: {_pct_str(pd_h)} vs system")
            if pd_m is not None:
                subbits.append(f"miles: {_pct_str(pd_m)} vs system")
            if pd_e is not None:
                subbits.append(f"expenses: {_pct_str(pd_e)} vs system")
            if subbits:
                answer_parts.append(", ".join(subbits) + ".")
            answer = " ".join(answer_parts)
            answer = answer.strip()
            return {
                "answer": answer,
                "citations": ["system:billables_rollup", f"claim:{claim_id}:billables_rollup"],
                "is_guess": False,
                "confidence": 1.0,
                "model_source": "system",
                "model": None,
                "local_only": True,
                "answer_mode": "brief",
            }

        # Billables summary / list / uninvoiced list
        if det_intent and det_intent.startswith("billables"):
            billables = context.get("billables") or []
            summary = context.get("billable_summary") or {}

            # Deterministic aggregation (authoritative)
            summary = aggregate_billables(billables)
            context["billable_summary"] = summary

            if det_intent == "billables_summary":
                # Prefer stable ordering
                wanted = ["billable_count", "uninvoiced_count", "no_bill_count", "hours_total", "miles_total", "expense_total"]
                lines: List[str] = []
                for k in wanted:
                    v = summary.get(k)
                    if v is None or v == "":
                        continue
                    lines.append(f"- {k.replace('_',' ')}: {v}")
                if not lines and summary:
                    for k in sorted(summary.keys()):
                        v = summary.get(k)
                        if v is None or v == "":
                            continue
                        lines.append(f"- {k}: {v}")
                if lines:
                    return {
                        "answer": "Billable summary:\n" + "\n".join(lines),
                        "citations": [],
                        "is_guess": False,
                        "confidence": 1.0,
                        "model_source": "system",
                        "model": None,
                        "local_only": True,
                        "answer_mode": "brief",
                    }

            if billables:
                items = billables
                if det_intent == "billables_uninvoiced":
                    items = [b for b in billables if isinstance(b, dict) and not _billable_is_invoiced(b)]

                # Keep deterministic and not insane in the UI
                tail = items[-25:] if len(items) > 25 else items
                lines = [
                    f"{i}. {_format_billable_line(b)}" for i, b in enumerate(tail, start=1)
                    if isinstance(b, dict)
                ]
                if not lines:
                    lines = ["(No billables matched the filter.)"]
                header_line = "Billables:" if det_intent != "billables_uninvoiced" else "Uninvoiced billables:"
                return {
                    "answer": header_line + "\n" + "\n".join(lines),
                    "citations": [],
                    "is_guess": False,
                    "confidence": 1.0,
                    "model_source": "system",
                    "model": None,
                    "local_only": True,
                    "answer_mode": "list",
                }

            # If we have only summary totals, return those deterministically.
            if summary:
                lines: List[str] = []
                for k in sorted(summary.keys()):
                    v = summary.get(k)
                    if v is None or v == "":
                        continue
                    lines.append(f"- {k}: {v}")
                if lines:
                    return {
                        "answer": "Billable summary:\n" + "\n".join(lines),
                        "citations": [],
                        "is_guess": False,
                        "confidence": 1.0,
                        "model_source": "system",
                        "model": None,
                        "local_only": True,
                        "answer_mode": "brief",
                    }

        # Invoice list (best-effort from structured context if present)
        if det_intent == "invoice_list":
            invoices = context.get("invoices") or (context.get("header") or {}).get("invoices")
            if isinstance(invoices, list) and invoices:
                lines: List[str] = []
                for i, inv in enumerate(invoices[:25], start=1):
                    if not isinstance(inv, dict):
                        continue
                    parts: List[str] = []
                    for k in ("invoice_number", "id", "date", "total", "status"):
                        v = inv.get(k)
                        if v not in (None, ""):
                            parts.append(f"{k}={v}")
                    if parts:
                        lines.append(f"{i}. " + "; ".join(parts))
                if lines:
                    return {
                        "answer": "Invoices:\n" + "\n".join(lines),
                        "citations": [],
                        "is_guess": False,
                        "confidence": 1.0,
                        "model_source": "system",
                        "model": None,
                        "local_only": True,
                        "answer_mode": "list",
                    }

    # System/global comparison intent
    if det_intent == "billables_compare_system":
        return {
            "answer": "Comparisons require a specific claim. Open a claim and ask: 'Is this claim typical?'",
            "citations": [],
            "is_guess": False,
            "confidence": 1.0,
            "model_source": "system",
            "model": None,
            "local_only": True,
            "answer_mode": "brief",
        }

    # ---------------------------------------------------------------------
    # End deterministic router — fall through to LLM
    # ---------------------------------------------------------------------

    # Clarity debug mode: only when explicitly requested
    if ql.startswith("debug"):
        return {
            "answer": "Clarity diagnostic snapshot (no LLM)",
            "diagnostics": debug_retrieval_snapshot(context),
            "context_keys": sorted(context.keys()),
            "model_source": "none",
            "model": None,
            "local_only": True,
            "answer_mode": "debug",
        }

    prompt_context_text = _context_to_prompt_text(context)
    # Intent-aware mode selection
    # (ql already defined above)
    if any(k in ql for k in ["summarize", "summary", "overview", "what's going on", "what is going on", "status of this claim"]):
        mode = "summary"
    elif any(k in ql for k in ["draft", "write", "rewrite", "generate"]):
        mode = "draft"
    else:
        mode = (context.get("mode") or "read")

    # NOTE: summary mode is intentionally less restrictive than read mode
    # to allow higher-level synthesis while still grounded in retrieved context.
    prompt = build_prompt(
        question=question,
        context=prompt_context_text,
        mode=mode,
    )
    result = call_llm_with_meta(prompt)
    normalized = _normalize_llm_result(result)
    llm_info = get_active_llm_info()

    answer_text = (normalized.get("text") or "").strip()

    # If the model gave an empty or "no context" style answer and we're not
    # claim-scoped, automatically retry with a broader/system scope once.
    already_escalated = bool((context or {}).get("_escalated"))
    has_claim_id = bool((context or {}).get("claim_id"))
    scope = (context or {}).get("scope")
    if (not has_claim_id) and (not already_escalated) and _looks_like_missing_context(answer_text):
        try:
            ctx2 = dict(context or {})
            ctx2["_escalated"] = True
            # Force system scope; retrieval layer / chat engine can use this hint.
            ctx2["scope"] = "system"
            retry = ask_clarity(question=question, context=ctx2)
            if isinstance(retry, dict) and retry.get("answer"):
                return retry
        except Exception:
            pass

    # Defensive fallback: some local models occasionally return a valid JSON shell
    # but an empty answer. The UI treats that as an invalid/"not understood" reply.
    if not answer_text:
        answer_text = _fallback_answer_from_context(question=question, context=context)
        # If we had to synthesize a fallback, mark it as a guess and lower confidence.
        normalized["is_guess"] = True
        if normalized.get("confidence") is None or float(normalized.get("confidence") or 0) > 0.6:
            normalized["confidence"] = 0.6
        if not normalized.get("answer_mode"):
            normalized["answer_mode"] = "fallback"

    answer_mode = normalized.get("answer_mode") or "brief"

    diagnostics = None
    if _env_truthy("FLORENCE_DEBUG", "0"):
        diagnostics = debug_retrieval_snapshot(context)
    return {
        "answer": answer_text,
        "citations": normalized.get("citations", []),
        "is_guess": normalized.get("is_guess", False),
        "confidence": normalized.get("confidence"),
        "model_source": llm_info.get("backend"),
        "model": llm_info.get("model"),
        "local_only": llm_info.get("local", False),
        "answer_mode": answer_mode,
        **({"diagnostics": diagnostics, "context_keys": sorted(context.keys())} if diagnostics else {}),
    }

from app.ai.retrieval import retrieve_context, retrieve as clarity_retrieve

# -----------------------------------------------------------------------------
# Public helper: Clarity-compatible retrieval façade
# -----------------------------------------------------------------------------
def retrieve(context: dict = None, question: str = None, **kwargs) -> Dict[str, Any]:
    """
    Clarity retrieval façade.
    Always returns { "facts": list, "chunks": list, "sources": list }
    Delegates ONLY to app.ai.retrieval.retrieve().
    """
    context = context or {}
    claim_id = context.get("claim_id")
    if not claim_id:
        return {
            "facts": [],
            "chunks": [],
            "sources": [],
        }
    result = clarity_retrieve(
        claim_id=claim_id,
        query=question or "",
        scope=context.get("scope"),
        mode=context.get("mode"),
    )
    # Only keep the Clarity contract keys
    return {
        "facts": result.get("facts", []),
        "chunks": result.get("chunks", []),
        "sources": result.get("sources", []),
    }


# -----------------------------------------------------------------------------
# Deterministic retrieval diagnostics for Clarity
# -----------------------------------------------------------------------------
from typing import Any, Dict

def debug_retrieval_snapshot(context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministic diagnostics for Clarity retrieval.

    Returns facts about what data is present BEFORE any LLM call.
    This is used to debug grounding issues without invoking the model.
    """
    snapshot = {
        "has_claim_id": bool(context.get("claim_id")),
        "claim_id": context.get("claim_id"),
        "billable_count": 0,
        "prior_report_count": 0,
        "has_billables": False,
        "has_prior_reports": False,
        "billable_summary_keys": [],
    }

    billables = context.get("billables") or []
    prior_reports = context.get("prior_reports") or []
    summary = context.get("billable_summary") or {}

    snapshot["billable_count"] = len(billables)
    snapshot["prior_report_count"] = len(prior_reports)
    snapshot["has_billables"] = len(billables) > 0
    snapshot["has_prior_reports"] = len(prior_reports) > 0
    snapshot["billable_summary_keys"] = list(summary.keys())

    return snapshot

# NOTE:
# Billable quantities (hours / miles / dollars), invoice linkage, and unit semantics
# are provided exclusively by retrieve_context().
# Do NOT recompute, infer, or reformat numeric values in this service layer.
# This guarantees AI answers remain consistent with invoice math.


# -----------------------------------------------------------------------------
# Field guidance (what each field is supposed to contain)
# -----------------------------------------------------------------------------

FIELD_GUIDANCE: Dict[str, Dict[str, Any]] = {
    # =========================
    # Cross-report long-text fields
    # =========================
    "status_treatment_plan": {
        "label": "Current Status / Treatment Plan",
        "purpose": (
            "Summarize the claimant’s current clinical status and the treatment plan during this DOS range. "
            "Focus on what changed since the last report and what is planned next."
        ),
        "include": [
            "Brief current status update (symptom trend, functional progress, or clinical status) ONLY if documented",
            "Treatment actions during this DOS range (visits, referrals, therapies, medications, procedures/surgery scheduling or completion) if documented",
            "Diagnostics/imaging performed and key findings if documented (do not interpret beyond the record)",
            "Concrete next steps and timeframes when present (follow-up visit, pending records, pending imaging, post-op evaluation)",
            "If information is limited, write one concise sentence stating what was not documented and what is being awaited",
        ],
        "avoid": [
            "Inventing symptoms, diagnoses, restrictions, or treatment decisions",
            "Copying long generic boilerplate",
            "Repeating 'pending evaluation' or 'not provided' multiple times",
            "Clinical recommendations not present in the record",
            "Identifiers (name, DOB, claim number, address, phone, fax, email)",
        ],
    },

    "work_status": {
        "label": "Work Status",
        "purpose": "Describe the claimant’s current work capacity or restrictions if documented.",
        "include": [
            "Full duty, modified duty, or off-work status if documented",
            "Restrictions or RTW guidance if documented",
            "If unknown, state succinctly that work status was not documented",
        ],
        "avoid": [
            "Guessing restrictions or disability status",
            "Overly verbose explanations",
        ],
    },

    "case_management_plan": {
        "label": "Case Management Plan",
        "purpose": "Outline case management actions and coordination planned for the next interval.",
        "include": [
            "Provider follow-up or appointment coordination",
            "Records requests or review",
            "Barrier mitigation steps if applicable",
            "Next steps and anticipated follow-up",
        ],
        "avoid": [
            "Inventing communications or actions",
            "Clinical decision-making outside case management scope",
        ],
    },

    "case_management_impact": {
        "label": "Case Management Impact",
        "purpose": "Summarize the impact or outcome of case management services provided.",
        "include": [
            "What coordination or intervention achieved",
            "Current status following intervention",
            "Remaining needs if any",
        ],
        "avoid": ["Generic filler or marketing language"],
    },

    "closure_details": {
        "label": "Closure Details",
        "purpose": "Explain the reason for case closure and any relevant wrap-up information.",
        "include": [
            "Reason for closure",
            "Final care status or disposition",
            "Any documented follow-up instructions",
        ],
        "avoid": ["Inventing closure rationale"],
    },

    "next_appointment_notes": {
        "label": "Next Appointment Notes",
        "purpose": "Capture appointment scheduling status and relevant notes.",
        "include": [
            "Whether an appointment is scheduled or pending",
            "What is awaited (authorization, scheduling confirmation)",
            "Timeframes if documented",
        ],
        "avoid": ["Inventing dates or confirmations"],
    },

    # =========================
    # Initial report–specific fields
    # =========================
    "diagnosis": {
        "label": "Diagnosis",
        "purpose": "Summarize the working diagnosis or diagnoses supported by available documentation.",
        "include": [
            "Primary diagnosis if documented",
            "Secondary diagnoses if documented",
            "Neutral phrasing if diagnosis is not finalized",
        ],
        "avoid": [
            "Inventing diagnoses",
            "Speculative language",
        ],
    },

    "mechanism_of_injury": {
        "label": "Mechanism of Injury",
        "purpose": "Describe how the injury occurred based on documented history.",
        "include": [
            "Brief description of the injury event",
            "Relevant context (work-related, accident type) if documented",
            "If unclear, state that the mechanism was not documented",
        ],
        "avoid": [
            "Guessing circumstances",
            "Overly detailed storytelling",
        ],
    },

    "concurrent_conditions": {
        "label": "Concurrent Conditions",
        "purpose": "Identify documented comorbid or concurrent conditions impacting recovery.",
        "include": [
            "Documented relevant conditions",
            "Conditions specifically noted as affecting recovery",
        ],
        "avoid": [
            "Diagnosing new conditions",
            "Lengthy explanations",
        ],
    },

    "surgical_history": {
        "label": "Surgical History",
        "purpose": "Summarize relevant surgical history related to the current condition.",
        "include": [
            "Prior surgeries relevant to the claim",
            "Recent procedures related to this injury if documented",
            "Dates only if present in context",
        ],
        "avoid": [
            "Inventing surgeries or dates",
            "Irrelevant historical detail",
        ],
    },

    "medications": {
        "label": "Medications",
        "purpose": "Summarize medications documented in available records.",
        "include": [
            "Medication names if documented",
            "High-level categories if specifics are not provided",
            "If none documented, state that succinctly",
        ],
        "avoid": [
            "Inventing medications or dosages",
            "Treatment recommendations",
        ],
    },

    "diagnostics": {
        "label": "Diagnostics",
        "purpose": "Summarize diagnostic testing related to the injury.",
        "include": [
            "Imaging or tests performed and key findings if documented",
            "Pending diagnostics if explicitly noted",
        ],
        "avoid": [
            "Inventing results",
            "Clinical interpretation beyond documentation",
        ],
    },

    "employment_status": {
        "label": "Employment Status",
        "purpose": "Summarize employment or job role information if documented.",
        "include": [
            "Employment status or job role",
            "Relevant work context if documented",
            "If unknown, state that it was not documented",
        ],
        "avoid": [
            "Guessing job details",
            "Unnecessary narrative",
        ],
    },

    # =========================
    # Helper / derived fields
    # =========================
    "barriers_summary": {
        "label": "Barriers to Recovery Summary",
        "purpose": "Briefly summarize selected barriers to recovery.",
        "include": [
            "Only documented barriers",
            "Brief impact on recovery if supported by context",
        ],
        "avoid": [
            "Adding new barriers",
            "Counseling-style language",
        ],
    },
}


def get_field_guidance(field_name: str) -> Dict[str, Any]:
    """Return guidance dict for a field (best-effort)."""
    return FIELD_GUIDANCE.get(field_name, {
        "label": field_name,
        "purpose": "Draft this field based on the provided context.",
        "include": [],
        "avoid": [],
    })


AI_CONTEXT_FIELDS: Tuple[str, ...] = (
    # Cross-report common fields
    "status_treatment_plan",
    "work_status",
    "case_management_plan",
    "case_management_impact",
    "closure_details",
    "next_appointment_notes",
    # Initial report fields (when present in schema)
    "diagnosis",
    "mechanism_of_injury",
    "concurrent_conditions",
    "surgical_history",
    "medications",
    "diagnostics",
    "employment_status",
)



# -----------------------------------------------------------------------------
# Global AI kill-switch helpers
# -----------------------------------------------------------------------------

def _env_truthy(name: str, default: str = "") -> bool:
    """Return True if an env var is set to a truthy value."""
    val = (os.getenv(name, default) or "").strip().lower()
    return val in {"1", "true", "yes", "y", "on"}


def _ai_globally_disabled() -> bool:
    """Global kill-switch for AI features (in addition to Settings.ai_enabled).

    Legacy env var retained for backward compatibility: OPENAI_DISABLED.

    Behavior:
    - If OPENAI_DISABLED is not truthy -> AI allowed.
    - If OPENAI_DISABLED is truthy -> block ONLY non-local backends (remote/remote LLM).
      Local LLM usage should still work.
    """
    if not _env_truthy("OPENAI_DISABLED", "0"):
        return False

    try:
        info = get_active_llm_info() or {}
        # Treat explicit local backends as allowed even when OPENAI_DISABLED is set.
        if info.get("local") is True:
            return False
        backend = (info.get("backend") or "").strip().lower()
        if backend in {"local", "ollama"}:
            return False
    except Exception:
        # If we can't determine backend, fail closed when OPENAI_DISABLED is truthy.
        pass

    return True


# -----------------------------------------------------------------------------
# Data structures
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class ToneProfile:
    """Lightweight style guide derived from prior reports."""

    # These are guidance signals, not strict rules.
    avg_sentence_len: float
    uses_bullets_often: bool
    common_phrases: Tuple[str, ...]
    # If you later want to get fancy: tense, voice, headings patterns, etc.


@dataclass(frozen=True)
class AIPrivacyRules:
    """Rules controlling what the AI may see."""

    allow_provider_names: bool


@dataclass(frozen=True)
class FaxLikeContactFields:
    """Helper structure for redacting common contact identifiers."""

    phone: str = ""
    fax: str = ""
    email: str = ""


# -----------------------------------------------------------------------------
# Public API (routes should call these)
# -----------------------------------------------------------------------------


def generate_report_field(
    *,
    report_id: int,
    field_name: str,
    user_prompt: str = "",
    max_prior_reports: int = 6,
    max_billables: int = 60,
) -> str:
    """Generate draft text for one report field.

    This is the single entry point routes should use.

    Privacy behavior:
    - Always removes claimant name, DOB, phone/fax, emails (best-effort)
    - Provider names are included only if Settings allow them

    NOTE: This function intentionally does NOT persist anything.

    Args:
        report_id: Current Report.id
        field_name: The report field we are drafting (e.g., "status_treatment_plan")
        user_prompt: Optional user guidance (e.g., "make it shorter")
        max_prior_reports: How many previous reports to include in context
        max_billables: How many billable items to include in timeline

    Returns:
        Draft text for the field.

    Raises:
        RuntimeError: If AI is disabled or report not found.
    """

    # Lazy imports to avoid circular import issues.
    from app.extensions import db
    from app.models import BillableItem, Report, Settings

    if _ai_globally_disabled():
        raise RuntimeError("AI is globally disabled (OPENAI_DISABLED).")

    settings = Settings.query.first()
    if not settings or not getattr(settings, "ai_enabled", False):
        raise RuntimeError("AI is disabled in Settings.")

    report: Optional[Report] = Report.query.get(report_id)
    if not report:
        raise RuntimeError(f"Report {report_id} not found")

    rules = AIPrivacyRules(
        allow_provider_names=bool(getattr(settings, "ai_allow_provider_names", False))
    )

    context = build_context(
        report=report,
        field_name=field_name,
        rules=rules,
        max_prior_reports=max_prior_reports,
        max_billables=max_billables,
        BillableItem=BillableItem,
        db=db,
    )

    tone = infer_tone_profile(context.get("prior_reports", []))

    prompt_context_text = _context_to_prompt_text(context)

    tone_hint = (
        f"avg_sentence_len={tone.avg_sentence_len:.1f}; "
        f"uses_bullets_often={tone.uses_bullets_often}; "
        f"common_phrases={list(tone.common_phrases)}"
    )

    instructions = (
        f"Draft the report field '{field_name}'. "
        f"Follow the field guidance in context. "
        f"Tone hints: {tone_hint}. "
        f"User request: {user_prompt.strip()}"
    ).strip()

    prompt = build_prompt(
        question=user_prompt or f"Draft {field_name}",
        context=prompt_context_text,
        mode="draft",
        instructions=instructions,
    )

    # One choke point for the actual model call.
    result = call_llm_with_meta(prompt)
    normalized = _normalize_llm_result(result)

    llm_info = get_active_llm_info()

    return {
        "text": normalized.get("text"),
        "model_source": llm_info.get("backend"),
        "model": llm_info.get("model"),
        "local_only": llm_info.get("local", False),
        "citations": normalized.get("citations", []),
        "is_guess": normalized.get("is_guess", False),
        "confidence": normalized.get("confidence"),
    }


# -----------------------------------------------------------------------------
# Inserted: Prompt builder for previewing report field draft prompts
# -----------------------------------------------------------------------------

def build_report_field_draft_prompt(
    *,
    report_id: Optional[int] = None,
    report: Any = None,
    field_name: str,
    user_prompt: str = "",
    max_prior_reports: int = 6,
    max_billables: int = 60,
    settings: Any = None,
) -> str:
    """Build (but do not send) an AI prompt for drafting one report field.

    This is a helper used by routes so they can:
    - Render a prompt preview
    - Reuse the same context/tone logic

    The LLM call is intentionally NOT performed here.

    Args:
        report_id: Report.id (optional if `report` is provided)
        report: Report instance (optional if `report_id` is provided)
        field_name: Target report field (e.g. "status_treatment_plan")
        user_prompt: Optional user guidance (e.g. "make it shorter")
        max_prior_reports: How many previous reports to include
        max_billables: How many billable items to include
        settings: Settings instance (optional; will be loaded if omitted)

    Returns:
        A fully assembled prompt string.

    Raises:
        RuntimeError: If AI is disabled or report not found.
    """

    # Lazy imports to avoid circular import issues.
    from app.extensions import db
    from app.models import BillableItem, Report, Settings

    if settings is None:
        settings = Settings.query.first()

    if _ai_globally_disabled():
        raise RuntimeError("AI is globally disabled (OPENAI_DISABLED).")

    if not settings or not getattr(settings, "ai_enabled", False):
        raise RuntimeError("AI is disabled in Settings.")

    if report is None:
        if report_id is None:
            raise RuntimeError("Missing report/report_id")
        report = Report.query.get(report_id)

    if not report:
        raise RuntimeError(f"Report {report_id} not found")

    rules = AIPrivacyRules(
        allow_provider_names=bool(getattr(settings, "ai_allow_provider_names", False))
    )

    context = build_context(
        report=report,
        field_name=field_name,
        rules=rules,
        max_prior_reports=max_prior_reports,
        max_billables=max_billables,
        BillableItem=BillableItem,
        db=db,
    )

    tone = infer_tone_profile(context.get("prior_reports", []))

    prompt_context_text = _context_to_prompt_text(context)

    tone_hint = (
        f"avg_sentence_len={tone.avg_sentence_len:.1f}; "
        f"uses_bullets_often={tone.uses_bullets_often}; "
        f"common_phrases={list(tone.common_phrases)}"
    )

    instructions = (
        f"Draft the report field '{field_name}'. "
        f"Follow the field guidance in context. "
        f"Tone hints: {tone_hint}. "
        f"User request: {user_prompt.strip()}"
    ).strip()

    prompt = build_prompt(
        question=user_prompt or f"Draft {field_name}",
        context=prompt_context_text,
        mode="draft",
        instructions=instructions,
    )

    return prompt


# -----------------------------------------------------------------------------
# Context assembly
# -----------------------------------------------------------------------------

#
# NOTE (AI vNext):
# ----------------
# Billable items, totals, hours, miles, and expense dollars are sourced
# EXCLUSIVELY via app.ai.retrieval.retrieve_context().
# Do NOT reintroduce ad-hoc BillableItem queries in this file.
# This guarantees numeric accuracy and keeps AI answers aligned with
# invoice math and reporting totals.
#
def build_context(
    *,
    report: Any,
    field_name: str,
    rules: AIPrivacyRules,
    max_prior_reports: int,
    max_billables: int,
    BillableItem: Any,
    db: Any,
) -> Dict[str, Any]:
    """Assemble a redacted context payload for AI generation."""

    claim = getattr(report, "claim", None)
    claim_id = getattr(report, "claim_id", None)

    # Collect prior reports on the same claim (any type), excluding the current report.
    prior_reports = collect_prior_reports(
        claim_id=claim_id,
        current_report_id=getattr(report, "id", None),
        limit=max_prior_reports,
    )

    # Retrieve billables and other structured context using the retrieval layer.
    retrieval = retrieve_context(
        claim_id=claim_id,
        report=report,
        max_billables=max_billables,
        max_reports=max_prior_reports,
    )
    billable_summary = retrieval.get("summary") or {}
    billables = retrieval.get("billables", [])

    header = build_safe_header(report=report, claim=claim)

    # Current report barriers (resolved to labels for prompt usefulness)
    current_barrier_ids = _parse_barrier_ids(getattr(report, "barriers_json", None))
    current_barriers = _resolve_barrier_labels(barrier_ids=current_barrier_ids)

    # Provide the current field's existing value so the model can revise/improve it.
    current_field_value = _clip_text(getattr(report, field_name, None)) if hasattr(report, field_name) else None

    # Include additional current report fields as context (best-effort; scrubbed later).
    current_fields: Dict[str, Any] = {
        "field_being_drafted": field_name,
        "existing_field_value": current_field_value,
        "barriers_to_recovery": current_barriers,
        "treating_provider_name": getattr(getattr(report, "treating_provider", None), "name", None),
        "treating_provider_specialty": getattr(getattr(report, "treating_provider", None), "specialty", None),
        "next_appointment": _safe_dt(getattr(report, "next_appointment", None)),
        "next_appointment_notes": _clip_text(getattr(report, "next_appointment_notes", None)),
    }

    # Add all supported report fields as context (best-effort)
    for fname in AI_CONTEXT_FIELDS:
        if fname in current_fields:
            continue
        if hasattr(report, fname):
            current_fields[fname] = _clip_text(getattr(report, fname, None))

    # Redact everything we can.
    header = scrub_dict(header, rules=rules)

    # Add resolved barrier labels to prior reports before scrubbing.
    prior_reports_enriched: List[Dict[str, Any]] = []
    for r in prior_reports:
        ids = _parse_barrier_ids(r.get("barriers_json"))
        r = dict(r)
        r["barriers_to_recovery"] = _resolve_barrier_labels(barrier_ids=ids)
        prior_reports_enriched.append(r)

    prior_reports = [scrub_dict(r, rules=rules) for r in prior_reports_enriched]
    billables = [scrub_dict(b, rules=rules) for b in billables]
    current_fields = scrub_dict(current_fields, rules=rules)

    # Numeric billable totals (summary, e.g. hours/miles/dollars) come from retrieval only.
    # Do NOT recompute, infer, or transform these values in prompts or the service layer.
    return {
        "header": header,
        "current_report_fields": current_fields,
        "current_report": {
            "id": getattr(report, "id", None),
            "report_type": getattr(report, "report_type", None),
            "dos_start": _safe_date(getattr(report, "dos_start", None)),
            "dos_end": _safe_date(getattr(report, "dos_end", None)),
            "next_report_due": _safe_date(getattr(report, "next_report_due", None)),
        },
        "prior_reports": prior_reports,
        "billables": billables,
        "billable_summary": billable_summary,
        "privacy": {
            "allow_provider_names": rules.allow_provider_names,
        },
    }

def build_safe_header(*, report: Any, claim: Any) -> Dict[str, Any]:
    """Build a minimal header for context.

    IMPORTANT: We intentionally do NOT include claimant name, DOB, phone, email,
    claim number, address, or other identifying fields.

    We do include dates and high-level claim/report metadata.
    """

    header: Dict[str, Any] = {
        # High-level, non-identifying claim/report context
        "claim_state": getattr(claim, "claim_state", None) if claim else None,
        "doi": _safe_date(getattr(claim, "doi", None) if claim else None),
        "referral_date": _safe_date(getattr(claim, "referral_date", None) if claim else None),
        "surgery_date": _safe_date(getattr(claim, "surgery_date", None) if claim else None),
        "injured_body_part": getattr(claim, "injured_body_part", None) if claim else None,

        "report_type": getattr(report, "report_type", None),
        "dos_start": _safe_date(getattr(report, "dos_start", None)),
        "dos_end": _safe_date(getattr(report, "dos_end", None)),
        "next_report_due": _safe_date(getattr(report, "next_report_due", None)),
    }

    # Provider identity is conditionally allowed; other provider metadata is okay.
    treating_provider = getattr(report, "treating_provider", None)
    if treating_provider is not None:
        header["treating_provider_name"] = getattr(treating_provider, "name", None)
        header["treating_provider_specialty"] = getattr(treating_provider, "specialty", None)

    return header

def collect_prior_reports(
    *,
    claim_id: Optional[int],
    current_report_id: Optional[int],
    limit: int,
) -> List[Dict[str, Any]]:
    """Fetch prior reports as dicts.

    Returns a list of lightweight dicts containing report metadata + report fields.

    NOTE: We intentionally return a broad set of fields so the AI has richer context.
    Actual redaction rules are applied later (scrub_dict / scrub_text).
    """

    if not claim_id:
        return []

    from app.models import Report

    q = Report.query.filter(Report.claim_id == claim_id)
    if current_report_id:
        q = q.filter(Report.id != current_report_id)

    # Most recent first.
    q = q.order_by(Report.dos_end.desc().nullslast(), Report.created_at.desc())
    rows = q.limit(limit).all()

    out: List[Dict[str, Any]] = []
    for r in rows:
        item: Dict[str, Any] = {
            "id": getattr(r, "id", None),
            "report_type": getattr(r, "report_type", None),
            "created_at": _safe_dt(getattr(r, "created_at", None)),
            "dos_start": _safe_date(getattr(r, "dos_start", None)),
            "dos_end": _safe_date(getattr(r, "dos_end", None)),
            "next_report_due": _safe_date(getattr(r, "next_report_due", None)),
            "closure_reason": getattr(r, "reason_for_closure", None),
            "barriers_json": getattr(r, "barriers_json", None),
            # Provider is conditionally allowed; include name and redact later if needed.
            "treating_provider_name": getattr(getattr(r, "treating_provider", None), "name", None),
        }

        # Include all supported AI context fields (best-effort).
        for fname in AI_CONTEXT_FIELDS:
            if fname in item:
                continue
            if hasattr(r, fname):
                item[fname] = getattr(r, fname, None)

        out.append(item)

    return out

def collect_billable_timeline(
    *,
    BillableItem: Any,
    claim_id: Optional[int],
    dos_start: Any,
    dos_end: Any,
    limit: int,
) -> List[Dict[str, Any]]:
    # DEPRECATED: retained for reference only. New AI paths must use retrieve_context().
    """Fetch billable items as a lightweight timeline.

    We include:
    - date
    - activity code
    - short description
    - notes (if present) for richer context

    NOTE: Any identifying information in notes will be scrubbed later.
    """

    if not claim_id:
        return []

    q = BillableItem.query.filter(BillableItem.claim_id == claim_id)

    # Prefer DOS window when available.
    if dos_start and dos_end and hasattr(BillableItem, "service_date"):
        q = q.filter(BillableItem.service_date >= dos_start, BillableItem.service_date <= dos_end)

    # Most recent first so the AI sees latest narrative.
    if hasattr(BillableItem, "service_date"):
        q = q.order_by(BillableItem.service_date.desc())
    else:
        q = q.order_by(BillableItem.id.desc())

    rows = q.limit(limit).all()

    out: List[Dict[str, Any]] = []
    for b in rows:
        out.append(
            {
                "id": getattr(b, "id", None),
                "service_date": _safe_date(getattr(b, "service_date", None)),
                "activity_code": getattr(b, "activity_code", None),
                "description": getattr(b, "description", None),
                "notes": getattr(b, "notes", None),
                "quantity": getattr(b, "quantity", None),
            }
        )

    # Reverse so it's chronological (oldest -> newest). Better for narrative building.
    out.reverse()
    return out


# -----------------------------------------------------------------------------
# Tone inference
# -----------------------------------------------------------------------------

def infer_tone_profile(prior_reports: Sequence[Dict[str, Any]]) -> ToneProfile:
    """Infer a small tone profile from prior reports.

    This is intentionally lightweight (fast + deterministic).
    """

    texts: List[str] = []
    for r in prior_reports:
        for k in ("status_treatment_plan", "work_status", "case_management_plan", "case_management_impact", "closure_details"):
            v = r.get(k)
            if isinstance(v, str) and v.strip():
                texts.append(v.strip())

    joined = "\n\n".join(texts)
    sentences = _split_sentences(joined)
    avg_len = float(sum(len(s.split()) for s in sentences) / max(1, len(sentences)))

    uses_bullets = bool(re.search(r"(^\s*[-*•]\s+)|(^\s*\d+\.)", joined, flags=re.MULTILINE))

    # Common phrases: super basic n-gram-ish extraction.
    common = _extract_common_phrases(joined, max_phrases=8)

    return ToneProfile(
        avg_sentence_len=avg_len,
        uses_bullets_often=uses_bullets,
        common_phrases=tuple(common),
    )


# -----------------------------------------------------------------------------
# Prompt building
# -----------------------------------------------------------------------------


# build_prompt is now imported from app.ai.prompts


# -----------------------------------------------------------------------------
# Privacy / redaction
# -----------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)

# Broad US phone patterns; also catches many fax formats.
_PHONE_RE = re.compile(
    r"(?:(?:\+?1\s*[-.]?\s*)?(?:\(\s*\d{3}\s*\)|\d{3})\s*[-.]?\s*\d{3}\s*[-.]?\s*\d{4})(?:\s*(?:x|ext\.?|extension)\s*\d+)?",
    re.IGNORECASE,
)

# MM/DD/YYYY and similar
_DATELIKE_RE = re.compile(r"\b(0?[1-9]|1[0-2])[/-](0?[1-9]|[12]\d|3[01])[/-](\d{2}|\d{4})\b")

# Very rough claim number / policy number patterns (kept conservative)
_CLAIMNO_RE = re.compile(r"\b(?:claim|policy)\s*(?:#|number|no\.?|:)\s*[A-Z0-9-]{4,}\b", re.IGNORECASE)


def scrub_dict(d: Dict[str, Any], *, rules: AIPrivacyRules) -> Dict[str, Any]:
    """Scrub strings inside a dict (best-effort)."""

    out: Dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, str):
            out[k] = scrub_text(v, rules=rules)
        else:
            out[k] = v

    # Provider name removal when disallowed.
    if not rules.allow_provider_names:
        for key in ("treating_provider_name",):
            if key in out and isinstance(out.get(key), str):
                out[key] = None

    return out


def scrub_text(text: str, *, rules: AIPrivacyRules) -> str:
    """Remove obvious identifiers from free text.

    This is best-effort and intentionally conservative.

    NOTE: Claimant names are hard to scrub without known tokens.
    We rely on not including them in the first place.
    """

    if not text:
        return text

    t = text
    t = _EMAIL_RE.sub("[REDACTED_EMAIL]", t)
    t = _PHONE_RE.sub("[REDACTED_PHONE]", t)
    t = _CLAIMNO_RE.sub("[REDACTED_CLAIMNO]", t)

    # DOB handling: we do not want to remove general dates (they are useful),
    # but if someone writes "DOB 01/02/1980" we should strip that token.
    t = re.sub(r"\bDOB\b\s*" + _DATELIKE_RE.pattern, "DOB [REDACTED_DOB]", t, flags=re.IGNORECASE)
    t = re.sub(r"\bDate\s+of\s+Birth\b\s*" + _DATELIKE_RE.pattern, "Date of Birth [REDACTED_DOB]", t, flags=re.IGNORECASE)

    # Provider names are handled structurally (we avoid including them unless allowed).
    # If later needed: pass a provider-name list and scrub exact matches.

    return t


# call_llm is now imported from app.ai.llm


###############################################################################
# Small internal helpers
# -----------------------------------------------------------------------------

 # Deterministic claim summary fast-path helper
def _deterministic_claim_summary(context: Dict[str, Any]) -> str:
    """Build a useful claim overview without an LLM.

    This is intentionally deterministic and grounded in retrieved/structured context.
    """

    header = context.get("header") or {}
    current_report = context.get("current_report") or {}
    prior_reports = context.get("prior_reports") or []
    billables = context.get("billables") or []
    billable_summary = context.get("billable_summary") or {}

    lines: List[str] = []

    # Core header (non-identifying)
    claim_state = header.get("claim_state")
    doi = header.get("doi")
    referral_date = header.get("referral_date")
    surgery_date = header.get("surgery_date")
    injured_body_part = header.get("injured_body_part")

    # Current report metadata (if present)
    report_type = current_report.get("report_type") or header.get("report_type")
    dos_start = current_report.get("dos_start") or header.get("dos_start")
    dos_end = current_report.get("dos_end") or header.get("dos_end")
    next_report_due = current_report.get("next_report_due") or header.get("next_report_due")

    meta_bits: List[str] = []
    if claim_state:
        meta_bits.append(f"State: {claim_state}")
    if doi:
        meta_bits.append(f"DOI: {doi}")
    if referral_date:
        meta_bits.append(f"Referral: {referral_date}")
    if injured_body_part:
        meta_bits.append(f"Body part: {injured_body_part}")
    if surgery_date:
        meta_bits.append(f"Surgery date: {surgery_date}")

    if meta_bits:
        lines.append(" | ".join(meta_bits))

    # Report window
    rep_bits: List[str] = []
    if report_type:
        rep_bits.append(f"Report: {report_type}")
    if dos_start or dos_end:
        rep_bits.append(f"DOS: {dos_start or '?'} → {dos_end or '?'}")
    if next_report_due:
        rep_bits.append(f"Next report due: {next_report_due}")
    if rep_bits:
        lines.append(" | ".join(rep_bits))

    # Next appointment (if present)
    next_appt = None
    try:
        next_appt = (context.get("current_report_fields") or {}).get("next_appointment")
    except Exception:
        next_appt = None
    next_appt_notes = None
    try:
        next_appt_notes = (context.get("current_report_fields") or {}).get("next_appointment_notes")
    except Exception:
        next_appt_notes = None

    if next_appt or next_appt_notes:
        if next_appt and next_appt_notes:
            lines.append(f"Next appt: {next_appt} — {next_appt_notes}")
        elif next_appt:
            lines.append(f"Next appt: {next_appt}")
        else:
            lines.append(f"Next appt notes: {next_appt_notes}")

    # Billables: totals + recent activity
    if billable_summary:
        # Keep stable ordering for readability
        wanted = [
            "hours_total",
            "miles_total",
            "expense_total",
            "no_bill_count",
            "uninvoiced_count",
            "billable_count",
        ]
        bits: List[str] = []
        for k in wanted:
            v = billable_summary.get(k)
            if v is None or v == "":
                continue
            bits.append(f"{k.replace('_',' ')}: {v}")
        # If summary doesn't have our preferred keys, include a compact fallback
        if not bits:
            for k in sorted(billable_summary.keys()):
                v = billable_summary.get(k)
                if v is None or v == "":
                    continue
                bits.append(f"{k}: {v}")
        if bits:
            lines.append("Billables — " + " | ".join(bits))

    if billables:
        # Show last 5 chronologically (billables are typically chronological already)
        tail = billables[-5:]
        rec: List[str] = []
        for b in tail:
            sd = b.get("service_date") or ""
            code = b.get("activity_code") or ""
            qty = b.get("quantity")
            desc = (b.get("description") or "").strip()
            desc = desc[:120] + ("…" if len(desc) > 120 else "")
            parts: List[str] = []
            if sd:
                parts.append(str(sd))
            if code:
                parts.append(str(code))
            if qty is not None and qty != "":
                parts.append(f"qty={qty}")
            if desc:
                parts.append(desc)
            if parts:
                rec.append(" — ".join(parts))
        if rec:
            lines.append("Recent billables:")
            lines.extend([f"- {x}" for x in rec])

    # Pull key narrative from most recent prior report (best-effort)
    if prior_reports:
        r0 = prior_reports[0] if isinstance(prior_reports, list) else None
        if isinstance(r0, dict):
            stp = (r0.get("status_treatment_plan") or "").strip()
            ws = (r0.get("work_status") or "").strip()
            if stp:
                lines.append("Latest report — status/treatment:")
                lines.append(stp[:600] + ("…" if len(stp) > 600 else ""))
            if ws:
                lines.append("Latest report — work status:")
                lines.append(ws[:400] + ("…" if len(ws) > 400 else ""))

    # If we still have nothing, return empty so caller can fall back
    out = "\n".join([ln for ln in lines if isinstance(ln, str) and ln.strip()])
    return out.strip()


# Helper: get latest report from context (best-effort, prefers retrieval-provided)
def _get_latest_report_from_context(context: Dict[str, Any]) -> Dict[str, Any]:
    """Return a best-effort 'latest report' dict from whatever the context contains.

    Priority:
    1) retrieval-provided `latest_report` (already assembled/ordered)
    2) first item in `prior_reports` (they are typically ordered most-recent-first)
    3) synthesize from `current_report` + `current_report_fields`

    Always returns a dict (may be empty).
    """
    lr = context.get("latest_report")
    if isinstance(lr, dict) and lr:
        return lr

    prior = context.get("prior_reports")
    if isinstance(prior, list) and prior:
        if isinstance(prior[0], dict):
            return prior[0]

    # Synthesize from current report structures
    cr = context.get("current_report")
    cf = context.get("current_report_fields")
    out: Dict[str, Any] = {}
    if isinstance(cr, dict):
        out.update(cr)
    if isinstance(cf, dict):
        # Only pull the narrative fields we care about to avoid clutter
        for k in (
            "status_treatment_plan",
            "work_status",
            "case_management_plan",
            "case_management_impact",
            "closure_details",
            "next_appointment_notes",
            "next_appointment",
            "barriers_to_recovery",
        ):
            v = cf.get(k)
            if v not in (None, ""):
                out[k] = v
    return out

def _parse_barrier_ids(barriers_json: Any) -> List[int]:
    """Best-effort parse of barriers_json to a list of integer IDs."""
    if not barriers_json:
        return []

    # Sometimes stored as JSON string, sometimes already a list.
    if isinstance(barriers_json, str):
        raw = barriers_json.strip()
        if not raw:
            return []
        try:
            barriers_json = json.loads(raw)
        except Exception:
            return []

    if isinstance(barriers_json, dict):
        # Some shapes store ids under a key.
        for key in ("ids", "barriers", "selected", "values"):
            val = barriers_json.get(key)
            if isinstance(val, list):
                barriers_json = val
                break

    if not isinstance(barriers_json, list):
        return []

    out: List[int] = []
    for x in barriers_json:
        try:
            out.append(int(x))
        except Exception:
            continue
    return out


def _resolve_barrier_labels(*, barrier_ids: Sequence[int]) -> List[Dict[str, str]]:
    """Resolve BarrierOption IDs to label/category (best-effort)."""
    if not barrier_ids:
        return []

    try:
        from app.models import BarrierOption
    except Exception:
        return []

    try:
        rows = (
            BarrierOption.query.filter(BarrierOption.id.in_(list(barrier_ids)))
            .order_by(BarrierOption.sort_order.asc(), BarrierOption.label.asc())
            .all()
        )
    except Exception:
        return []

    out: List[Dict[str, str]] = []
    for r in rows:
        label = (getattr(r, "label", None) or "").strip()
        if not label:
            continue
        category = (getattr(r, "category", None) or "").strip()
        out.append({"label": label, "category": category})
    return out


def _clip_text(value: Any, *, max_chars: int = 1500) -> Optional[str]:
    """Clamp long free-text so prompts don't explode."""
    if value is None:
        return None
    if not isinstance(value, str):
        return str(value)
    t = value.strip()
    if not t:
        return None
    if len(t) > max_chars:
        return t[:max_chars].rstrip() + "…"
    return t

def _safe_date(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    # If it looks like a date string already, keep it.
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None

def _safe_dt(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None

def _split_sentences(text: str) -> List[str]:
    if not text.strip():
        return []
    # Simple sentence split.
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]

def _extract_common_phrases(text: str, max_phrases: int = 8) -> List[str]:
    """Very light common phrase extraction.

    We intentionally keep it simple: count repeated 3-5 word sequences.
    """

    words = re.findall(r"[A-Za-z']+", text.lower())
    if len(words) < 12:
        return []

    counts: Dict[str, int] = {}
    for n in (3, 4, 5):
        for i in range(0, len(words) - n + 1):
            gram = " ".join(words[i : i + n])
            # Skip grams that are mostly filler.
            if gram.startswith("the ") or gram.startswith("and "):
                continue
            counts[gram] = counts.get(gram, 0) + 1

    # Keep only things that repeat.
    candidates = [(k, v) for k, v in counts.items() if v >= 3]
    candidates.sort(key=lambda kv: (-kv[1], -len(kv[0])))

    # Return the original casing is unknown; keep lower-case but readable.
    return [k for k, _ in candidates[:max_phrases]]

def _pretty_kv(d: Dict[str, Any]) -> str:
    lines: List[str] = []
    for k in sorted(d.keys()):
        v = d.get(k)
        if v is None or v == "":
            continue
        lines.append(f"- {k}: {v}")
    return "\n".join(lines) if lines else "- (none)"

def _pretty_list_of_dicts(items: Sequence[Dict[str, Any]], *, keys: Sequence[str]) -> str:
    lines: List[str] = []
    for idx, it in enumerate(items, start=1):
        parts: List[str] = []
        for k in keys:
            v = it.get(k)
            if v is None or v == "":
                continue
            # Shorten huge blocks in context to avoid runaway prompts.
            if isinstance(v, str) and len(v) > 800:
                v = v[:800].rstrip() + "…"
            parts.append(f"{k}={v}")
        if parts:
            lines.append(f"{idx}. " + "; ".join(parts))
    return "\n".join(lines) if lines else "(none)"


# -----------------------------------------------------------------------------
# AI capability flags (central gating helper)
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# LLM compatibility helpers

def _fallback_answer_from_context(*, question: str, context: Dict[str, Any]) -> str:
    """Best-effort fallback answer when the LLM returns an empty answer.

    We keep this deterministic and grounded in the retrieved context.
    """

    facts = context.get("facts") or []
    chunks = context.get("chunks") or []
    sources = context.get("sources") or []

    # Prefer explicit facts if retrieval provided them.
    if isinstance(facts, list) and any(isinstance(x, str) and x.strip() for x in facts):
        items = [x.strip() for x in facts if isinstance(x, str) and x.strip()][:8]
        lines = "\n".join(f"- {x}" for x in items)
        return (
            "I couldn’t get a usable model-generated answer, but here’s what I can confirm from the claim context:\n"
            f"{lines}"
        )

    # Otherwise, provide a small diagnostic-style summary that is still useful to the user.
    n_chunks = len(chunks) if isinstance(chunks, list) else 0
    n_sources = len(sources) if isinstance(sources, list) else 0

    # If this was a broad request (e.g., summarize claim), try to produce a deterministic overview.
    ql = _qnorm(question)
    if any(k in ql for k in ["summarize", "summary", "overview", "what's going on", "status of this claim"]):
        det = ""
        try:
            det = _deterministic_claim_summary(context)
        except Exception:
            det = ""
        if det:
            return det

    # Keep it short and actionable.
    q = (question or "").strip()
    q_hint = f"Question: {q}\n" if q else ""
    return (
        "I pulled the claim context successfully, but the model returned an empty answer.\n"
        f"{q_hint}"
        f"Context loaded: {n_chunks} sections, {n_sources} sources.\n"
        "Try one of these:\n"
        "- Summarize this claim\n"
        "- Summarize billables\n"
        "- List uninvoiced billables\n"
        "- What was the last DOS?\n"
        "- What did the latest report say about work status?"
    )
# -----------------------------------------------------------------------------

def _normalize_llm_result(result: Any) -> Dict[str, Any]:
    """Normalize LLM return values.

    We support three shapes:
      1) dicts returned by our backend (often {"text": "..."})
      2) raw strings
      3) Legacy STRICT JSON payloads embedded in text (optionally fenced)

    Output is always a dict containing at least:
      - text (string)
      - citations (list)
      - is_guess (bool)
      - confidence (float|None)
      - answer_mode (str|None)

    Notes:
    - Legacy prompts demand STRICT JSON, but some models will still wrap it
      in code fences or add explanations. We defensively extract and parse the
      first JSON object we can decode.
    - If JSON parsing fails, we fall back to returning the raw text.
    """

    def _as_text(r: Any) -> str:
        if r is None:
            return ""
        if isinstance(r, str):
            return r
        if isinstance(r, dict) and isinstance(r.get("text"), str):
            return r.get("text") or ""
        return str(r)

    def _strip_code_fences(s: str) -> str:
        t = (s or "").strip()
        if t.startswith("```"):
            # remove leading ```lang and trailing ```
            # Keep only the inner content.
            t = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", t)
            t = re.sub(r"\s*```\s*$", "", t)
        return t.strip()

    def _extract_first_json_obj(s: str) -> Optional[Dict[str, Any]]:
        """Extract the first JSON object from the given text (best-effort)."""
        txt = _strip_code_fences(s)
        start = txt.find("{")
        if start < 0:
            return None

        decoder = json.JSONDecoder()
        try:
            obj, _end = decoder.raw_decode(txt[start:])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

        # Fallback: try a greedy brace span
        end = txt.rfind("}")
        if end > start:
            candidate = txt[start : end + 1]
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                return None
        return None

    def _normalize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize Legacy payload keys/types."""
        answer = payload.get("answer")
        # Some models may still use "text" despite our schema; allow it.
        if answer is None and payload.get("text") is not None:
            answer = payload.get("text")
        if answer is None:
            answer = ""
        if not isinstance(answer, str):
            answer = str(answer)

        citations = payload.get("citations", [])
        if citations is None:
            citations = []
        if isinstance(citations, str):
            citations = [citations]
        if not isinstance(citations, list):
            citations = [str(citations)]

        is_guess = payload.get("is_guess", False)
        if not isinstance(is_guess, bool):
            is_guess = bool(is_guess)

        confidence = payload.get("confidence", None)
        try:
            confidence = None if confidence is None else float(confidence)
        except Exception:
            confidence = None

        answer_mode = payload.get("answer_mode", None)
        if answer_mode is not None and not isinstance(answer_mode, str):
            answer_mode = str(answer_mode)

        model_source = payload.get("model_source", "unknown")
        if model_source is not None and not isinstance(model_source, str):
            model_source = str(model_source)

        out: Dict[str, Any] = {
            "text": answer,
            "citations": citations,
            "is_guess": is_guess,
            "confidence": confidence,
            "answer_mode": answer_mode,
            "model_source": model_source,
        }

        # Preserve any extra keys for debugging/forward-compat.
        for k in ("diagnostics", "action", "intent"):
            if k in payload:
                out[k] = payload.get(k)

        return out

    # 1) If backend already gave us a Legacy-shaped dict, normalize it.
    if isinstance(result, dict) and ("answer" in result or "citations" in result or "answer_mode" in result):
        return _normalize_payload(result)

    # 2) If backend gave a dict with a text field, try parsing that text as JSON.
    if isinstance(result, dict):
        raw_text = _as_text(result)
        parsed = _extract_first_json_obj(raw_text)
        if isinstance(parsed, dict):
            normalized = _normalize_payload(parsed)
            # If backend supplied confidence/citations etc. explicitly, let it override.
            for k in ("citations", "is_guess", "confidence", "answer_mode"):
                if k in result and result.get(k) is not None:
                    normalized[k] = result.get(k)
            # Preserve backend model fields (they are provenance, not LLM content).
            if result.get("model") is not None:
                normalized["model"] = result.get("model")
            if result.get("model_source") is not None:
                normalized["model_source"] = result.get("model_source")
            return normalized

        # No parsable JSON; treat as plain text.
        out = {
            "text": raw_text,
            "citations": result.get("citations", []) if isinstance(result.get("citations"), list) else [],
            "is_guess": bool(result.get("is_guess", False)),
            "confidence": result.get("confidence", None),
            "answer_mode": result.get("answer_mode", None),
            "model_source": result.get("model_source", "unknown"),
            "model": result.get("model"),
        }
        return out

    # 3) Raw string: attempt JSON extraction; otherwise return as-is.
    raw_text = _as_text(result)
    parsed = _extract_first_json_obj(raw_text)
    if isinstance(parsed, dict):
        return _normalize_payload(parsed)

    return {
        "text": raw_text,
        "citations": [],
        "is_guess": False,
        "confidence": None,
        "answer_mode": None,
        "model_source": "unknown",
        "model": None,
    }

def _ai_capabilities() -> Dict[str, bool]:
    return {
        "local_llm": True,
        "embeddings": True,
        "write_actions": True,
        "phi_allowed": False,
    }

    # ---------------------------------------------------------------------
    # Deterministic system-level claims list/count (no claim_id required)
    # ---------------------------------------------------------------------
    if det_intent in {
        "claim_count", "claim_count_open", "claim_count_closed", "claim_count_both",
        "claim_list_open", "claim_list_closed", "claim_list_both",
        "claim_scope_followup",
    }:
        from app.models import Claim
        from app.extensions import db

        def _claim_status(c: Any) -> str:
            for attr in ("status", "claim_status", "state", "lifecycle"):
                if hasattr(c, attr):
                    v = getattr(c, attr)
                    if v not in (None, ""):
                        return str(v).strip()
            return ""

        def _is_open(c: Any) -> bool:
            s = _claim_status(c).lower()
            if s in {"open", "active", "in progress", "in_progress"}:
                return True
            if s in {"closed", "inactive", "complete", "completed"}:
                return False
            return True

        claims = db.session.query(Claim).order_by(Claim.id.asc()).all()
        open_claims = [c for c in claims if _is_open(c)]
        closed_claims = [c for c in claims if not _is_open(c)]

        qn = _qnorm(question)
        if det_intent == "claim_scope_followup":
            pending = (context or {}).get("pending_intent")
            if pending == "claim_count":
                if qn == "open":
                    return {
                        "answer": f"There are {len(open_claims)} open claims.",
                        "citations": [],
                        "is_guess": False,
                        "confidence": 1.0,
                        "model_source": "system",
                        "model": None,
                        "local_only": True,
                        "answer_mode": "brief",
                    }
                if qn == "closed":
                    return {
                        "answer": f"There are {len(closed_claims)} closed claims.",
                        "citations": [],
                        "is_guess": False,
                        "confidence": 1.0,
                        "model_source": "system",
                        "model": None,
                        "local_only": True,
                        "answer_mode": "brief",
                    }
                if qn == "both":
                    return {
                        "answer": f"There are {len(open_claims)} open and {len(closed_claims)} closed claims ({len(claims)} total).",
                        "citations": [],
                        "is_guess": False,
                        "confidence": 1.0,
                        "model_source": "system",
                        "model": None,
                        "local_only": True,
                        "answer_mode": "brief",
                    }

        if det_intent in {"claim_count_open", "claim_count_closed", "claim_count_both", "claim_count"}:
            if det_intent == "claim_count_open":
                return {
                    "answer": f"There are {len(open_claims)} open claims.",
                    "citations": [],
                    "is_guess": False,
                    "confidence": 1.0,
                    "model_source": "system",
                    "model": None,
                    "local_only": True,
                    "answer_mode": "brief",
                }
            if det_intent == "claim_count_closed":
                return {
                    "answer": f"There are {len(closed_claims)} closed claims.",
                    "citations": [],
                    "is_guess": False,
                    "confidence": 1.0,
                    "model_source": "system",
                    "model": None,
                    "local_only": True,
                    "answer_mode": "brief",
                }
            if det_intent == "claim_count_both":
                return {
                    "answer": f"There are {len(open_claims)} open and {len(closed_claims)} closed claims ({len(claims)} total).",
                    "citations": [],
                    "is_guess": False,
                    "confidence": 1.0,
                    "model_source": "system",
                    "model": None,
                    "local_only": True,
                    "answer_mode": "brief",
                }
            return {
                "answer": "Open, closed, or both?",
                "citations": [],
                "is_guess": False,
                "confidence": 1.0,
                "model_source": "system",
                "model": None,
                "local_only": True,
                "answer_mode": "brief",
                "pending_intent": "claim_count",
            }

        def _fmt_claim(c: Any) -> str:
            cid = getattr(c, "id", None)
            s = _claim_status(c) or "(unknown)"
            return f"{cid} — {s}"

        if det_intent == "claim_list_open":
            rows = open_claims
            title = "Open claims"
        elif det_intent == "claim_list_closed":
            rows = closed_claims
            title = "Closed claims"
        else:
            rows = claims
            title = "All claims"

        max_rows = 30
        lines = [_fmt_claim(c) for c in rows[:max_rows]]
        more = len(rows) - len(lines)
        if more > 0:
            lines.append(f"…and {more} more")

        return {
            "answer": title + ":\n" + "\n".join(f"- {ln}" for ln in lines),
            "citations": [],
            "is_guess": False,
            "confidence": 1.0,
            "model_source": "system",
            "model": None,
            "local_only": True,
            "answer_mode": "list",
        }