from typing import Any, Dict, Tuple, Optional, List
# -----------------------------
# Frame stack expiration / reset helper
# -----------------------------
# Frame reset philosophy: Clear the frame stack when the user asks a new top-level question,
# navigates away from claim context, or explicitly requests a reset.
def maybe_reset_frame_stack(question: str, context: Dict[str, Any], thread_state: Dict[str, Any]) -> None:
    """
    Mutates thread_state in-place.
    Clears ["frame_stack"] and ["last_frame"] if a reset trigger is detected.
    Reset triggers:
      - Explicit navigation phrases ("new question", "start over", "reset")
      - Obvious top-level/system questions ("system overview", "how many claims do I have")
      - Page context change away from claim_detail with no claim_id present
    """
    q = (question or "").strip().lower()
    # 1. Explicit navigation/reset phrases
    if any(kw in q for kw in ["new question", "start over", "reset"]):
        thread_state.pop("frame_stack", None)
        thread_state.pop("last_frame", None)
        return
    # 2. Obvious top-level/system questions
    system_triggers = [
        "system overview", "system snapshot", "system", "snapshot", "diagnostic",
        "how many claims do i have", "how many open claims", "how many closed claims",
        "my system", "overview",
    ]
    if any(kw in q for kw in system_triggers):
        # If not asking about a specific claim, treat as reset
        if "claim" not in q or "this claim" not in q:
            thread_state.pop("frame_stack", None)
            thread_state.pop("last_frame", None)
            return
    # 3. Page context changed away from claim_detail and no claim_id
    page_ctx = (context or {}).get("page_context") or (context or {}).get("context") or ""
    claim_id = (context or {}).get("claim_id") or thread_state.get("last_claim_id")
    if str(page_ctx) != "claim_detail" and not claim_id:
        thread_state.pop("frame_stack", None)
        thread_state.pop("last_frame", None)
        return
# -----------------------------
# Option B: Conversation Frame + Domain Registry
# -----------------------------
from typing import cast
# -----------------------------
# Frame/domain registry for frame-relative follow-ups.
# Enables "what about X?" to be reliably interpreted in context.
# -----------------------------
FRAME_REGISTRY = {
    # Added support for synonyms and canonical questions for each domain
    "system_overview": {
        "domains": {
            "claims": {
                "synonyms": ["claim", "cases", "files"],
                "question": "How many claims do I have?"
            },
            "invoices": {
                "synonyms": ["invoice", "bills", "statements"],
                "question": "How many invoices do I have?"
            },
            "billing": {
                "synonyms": ["bill", "outstanding billing", "unpaid bills"],
                "question": "How much outstanding billing do I have?"
            },
            "billables": {
                "synonyms": ["billable", "work", "billable items"],
                "question": "Summarize billables"
            },
            "reports": {
                "synonyms": ["report", "documents", "summaries"],
                "question": "How many reports do I have?"
            },
        }
    },
    "claim_overview": {
        "domains": {
            "billables": {
                "synonyms": ["billable", "work", "billable items"],
                "question": "Summarize billables on this claim"
            },
            "billing": {
                "synonyms": ["bill", "outstanding billing", "unpaid bills"],
                "question": "How much billing is on this claim?"
            },
            "invoices": {
                "synonyms": ["invoice", "bills", "statements"],
                "question": "How many invoices are on this claim?"
            },
            "reports": {
                "synonyms": ["report", "documents", "summaries"],
                "question": "Summarize reports on this claim"
            },
        }
    },
}
# -----------------------------
# Frame-relative follow-up canonicalization
# -----------------------------
def maybe_canonicalize_frame_followup(question: str, thread_state: Dict[str, Any]) -> Tuple[str, bool]:
    """
    Rewrite frame-relative follow-ups like "what about claims?" to canonical questions.
    Resolves against the most specific active frame in thread_state["frame_stack"] (top of stack).
    Walks backward through the stack (most specific to least), stopping at first match.
    """
    q = (question or "").strip().lower()
    if not (q.startswith("what about") or q.startswith("how about")):
        return question, False
    # Use frame_stack if present, else fallback to last_frame for backward compatibility
    frame_stack = thread_state.get("frame_stack") or []
    if not isinstance(frame_stack, list):
        frame_stack = []
    frames_to_try = list(reversed(frame_stack)) if frame_stack else []
    last_frame = thread_state.get("last_frame")
    # For backward compatibility, try last_frame if stack empty or not present
    if not frames_to_try and last_frame:
        frames_to_try = [last_frame]
    # Extract the noun after "what about" or "how about"
    parts = q.split()
    if len(parts) < 3:
        return question, False
    noun = parts[2]
    noun = noun.rstrip("?.!,")
    # Walk from most specific (top of stack) to least
    for frame in frames_to_try:
        frame_entry = FRAME_REGISTRY.get(frame)
        if not frame_entry or "domains" not in frame_entry:
            continue
        domains = frame_entry["domains"]
        # 1. Try exact key match (including plural/singular normalization)
        for dom_key, dom_entry in domains.items():
            if noun == dom_key or noun.rstrip("s") == dom_key.rstrip("s"):
                return dom_entry["question"], True
        # 2. Try synonym match (including plural/singular normalization)
        for dom_key, dom_entry in domains.items():
            synonyms = dom_entry.get("synonyms", [])
            for syn in synonyms:
                if noun == syn or noun.rstrip("s") == syn.rstrip("s"):
                    return dom_entry["question"], True
    # No match found
    return question, False
# -----------------------------
# Follow-up canonicalization helper
# -----------------------------
def maybe_canonicalize_followup(question: str, thread_state: Dict[str, Any]) -> Tuple[str, bool]:
    """
    If the user asks a short, referential follow-up ("what about closed", "and unpaid", etc)
    and we have a last_intent, rewrite it into a canonical, full question.
    This improves conversational awareness while preserving determinism.
    Returns (canonical_question, was_rewritten).
    """
    q = (question or "").strip().lower()
    last_intent = thread_state.get("last_intent")
    if not last_intent:
        return question, False
    # claim_count follow-ups
    if last_intent == "claim_count":
        # Handle e.g. "what about closed", "and open", "only closed", etc.
        if any(q in s for s in [
            "what about closed", "and closed", "only closed", "just closed", "closed?",
            "what about open", "and open", "only open", "just open", "open?",
            "what about both", "and both", "all", "everything", "both?",
        ]):
            # Map to canonical claim_count question
            if "closed" in q:
                return "How many closed claims do I have?", True
            if "open" in q:
                return "How many open claims do I have?", True
            if "both" in q or "all" in q or "everything" in q:
                return "How many claims do I have?", True
    # billing_total follow-ups
    if last_intent == "billing_total":
        # Handle e.g. "what about outstanding", "and unpaid", "only total", etc.
        if any(q in s for s in [
            "what about outstanding", "and outstanding", "only outstanding", "outstanding?",
            "what about unpaid", "and unpaid", "only unpaid", "unpaid?",
            "what about total", "and total", "only total", "total?",
        ]):
            if "outstanding" in q or "unpaid" in q:
                return "How much outstanding billing do I have?", True
            if "total" in q:
                return "How much total billed do I have?", True
    # system_overview follow-ups
    if last_intent == "system_overview":
        if any(q in s for s in [
            "what about open", "open claims?", "and open", "only open",
            "what about closed", "closed claims?", "and closed", "only closed",
        ]):
            if "open" in q:
                return "How many open claims do I have?", True
            if "closed" in q:
                return "How many closed claims do I have?", True
    # Scope shifts: "this claim" vs "all claims"
    if last_intent in {"claim_count", "billing_total"}:
        if any(x in q for x in ["this claim", "for this claim", "on this claim", "just this claim"]):
            # Try to rephrase to claim-specific
            if last_intent == "claim_count":
                return "How many claims do I have on this claim?", True
            if last_intent == "billing_total":
                return "How much billing do I have on this claim?", True
        if any(x in q for x in ["all claims", "every claim", "across all claims"]):
            if last_intent == "claim_count":
                return "How many claims do I have?", True
            if last_intent == "billing_total":
                return "How much billing do I have?", True
    return question, False
"""app.ai.chat_engine

Lightweight, deterministic chat orchestration helpers for Florence.

Goals:
- Keep conversation state ("thread_state") small and explicit.
- Support clarifying questions via structured `action` payloads.
- Prefer deterministic answers (DB + retrieval) before LLM.

This module is intentionally dependency-light and safe to call from services.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# -----------------------------
# Response style controls
# -----------------------------

MAX_LINES_BRIEF = 4  # hard cap for executive summaries


# -----------------------------
# Response helpers
# -----------------------------

def make_action_choose_one(slot: str, options: List[Tuple[str, str]]) -> Dict[str, Any]:
    """UI action payload: render buttons for a single-slot choice."""
    return {
        "type": "choose_one",
        "slot": slot,
        "options": [{"label": label, "value": value} for (label, value) in options],
    }


def make_clarify(*, text: str, action: Dict[str, Any], thread_state_update: Dict[str, Any]) -> Dict[str, Any]:
    """Standard clarify response (system, no LLM)."""
    # Always persist last clarify intent/slot/original_question if present in pending
    tsu = dict(thread_state_update or {})
    pending = tsu.get("pending")
    if isinstance(pending, dict):
        tsu.setdefault("last_clarify_intent", pending.get("intent"))
        tsu.setdefault("last_clarify_slot", pending.get("slot"))
        tsu.setdefault("last_clarify_original_question", pending.get("original_question"))
    return {
        "handled": True,
        "ok": True,
        "answer": text,
        "citations": [],
        "is_guess": False,
        "confidence": 1.0,
        "model_source": "system",
        "model": None,
        "local_only": True,
        "answer_mode": "clarify",
        "action": action,
        "thread_state_update": tsu,
    }


def make_answer(*, text: str, thread_state_update: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "handled": True,
        "ok": True,
        "answer": text,
        "citations": [],
        "is_guess": False,
        "confidence": 1.0,
        "model_source": "system",
        "model": None,
        "local_only": True,
        "answer_mode": "brief",
    }
    if thread_state_update is not None:
        out["thread_state_update"] = thread_state_update
    return out


# -----------------------------
# Thread state + parsing
# -----------------------------

def _qnorm(s: str) -> str:
    return (s or "").strip().lower()


def extract_claim_status_scope(question: str) -> Optional[str]:
    q = _qnorm(question)

    # Explicit “both/all” intent
    if any(k in q for k in [
        " both",
        " all",
        " everything",
        " open and closed",
        "closed and open",
        "total claims",
        "overall claims",
        "total number of claims",
    ]):
        return "both"

    # Common phrasing that implies total claims (open + closed)
    if q in {
        "how many claims do i have",
        "how many claims do we have",
        "how many claims are there",
        "how many total claims do i have",
        "how many total claims are there",
        "how many claims do i have?",
        "how many total claims do i have?",
    }:
        return "both"

    # “total/overall” in a count question usually means open + closed
    if ("how many" in q or "count" in q) and "claim" in q and ("total" in q or "overall" in q):
        return "both"

    if "open" in q or "active" in q or "current" in q:
        return "open"

    if "closed" in q or "inactive" in q:
        return "closed"

    return None


def extract_billing_scope(question: str) -> Optional[str]:
    """Return 'outstanding' | 'total' based on keywords, else None."""
    q = _qnorm(question)

    outstanding_keywords = ["outstanding", "owed", "due", "receivable", "unpaid"]
    total_keywords = ["total billing", "total invoices", "total billed", "total invoiced"]

    if any(k in q for k in outstanding_keywords):
        return "outstanding"
    if any(k in q for k in total_keywords):
        return "total"

    return None


def mentions_this_claim(question: str) -> bool:
    q = _qnorm(question)
    return "this claim" in q or "on this claim" in q


def _ctx_claim_id(context: Dict[str, Any], ts: Dict[str, Any]) -> Optional[int]:
    """Best-effort claim_id resolver: prefer context, else fall back to last_claim_id."""
    claim_id = None
    if isinstance(context, dict):
        claim_id = context.get("claim_id")

    if claim_id in (None, ""):
        claim_id = ts.get("last_claim_id")

    if claim_id in (None, ""):
        return None

    if isinstance(claim_id, int):
        return claim_id

    try:
        return int(claim_id)
    except Exception:
        return None


def _ctx_page_context(context: Dict[str, Any]) -> str:
    try:
        v = (context or {}).get("page_context") or (context or {}).get("context")
        return str(v) if v else "system"
    except Exception:
        return "system"


def maybe_resolve_pending_choice(thread_state: Dict[str, Any], question: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """If the user is answering a prior clarify question, resolve it deterministically.

    Returns: (pending_dict, normalized_choice) or (None, None)

    Supported pending slot types:
      - claim_status: open | closed | both
      - billing_scope: outstanding | total

    IMPORTANT:
    - Only treat short tokens as a slot answer when a pending clarify exists.
    - Keep this conservative to avoid hijacking normal questions.
    """
    if not isinstance(thread_state, dict):
        return None, None

    pending = thread_state.get("pending")
    if not isinstance(pending, dict):
        # Fallback: try to reconstruct pending from last_clarify_* keys
        li = thread_state.get("last_clarify_intent")
        ls = thread_state.get("last_clarify_slot")
        loq = thread_state.get("last_clarify_original_question")
        if li and ls:
            pending = {"intent": li, "slot": ls, "original_question": loq}
        else:
            return None, None

    slot = pending.get("slot")
    q = _qnorm(question)

    # Back-compat: older shape used `slots: {<name>: None}`
    if not slot:
        slots = pending.get("slots")
        if isinstance(slots, dict) and len(slots) == 1:
            slot = next(iter(slots.keys()))

    # Only treat very short replies as choices.
    # (If user types a full sentence, let it flow through normal routing.)
    if len(q) > 40 and len(q.split()) > 6:
        return None, None

    if slot == "claim_status":
        open_set = {"open", "opened", "active", "current"}
        closed_set = {"closed", "inactive"}
        both_set = {"both", "all", "everything", "open and closed", "closed and open"}

        if q in open_set:
            return pending, "open"
        if q in closed_set:
            return pending, "closed"
        if q in both_set:
            return pending, "both"

        # Allow tiny variants like "open pls" / "both please"
        if len(q.split()) <= 4:
            if "open" in q:
                return pending, "open"
            if "closed" in q:
                return pending, "closed"
            if "both" in q or "all" in q:
                return pending, "both"

        return None, None

    if slot == "billing_scope":
        outstanding_set = {"outstanding", "owed", "due", "unpaid", "receivable"}
        total_set = {"total", "total billed", "total invoiced", "billed", "invoiced"}

        if q in outstanding_set:
            return pending, "outstanding"
        if q in total_set:
            return pending, "total"

        if len(q.split()) <= 4:
            if any(k in q for k in ["outstanding", "owed", "due", "unpaid", "receivable"]):
                return pending, "outstanding"
            if "total" in q or "billed" in q or "invoiced" in q:
                return pending, "total"

        return None, None

    return None, None


# -----------------------------
# Capability registry
# -----------------------------

def get_capabilities() -> List[Dict[str, Any]]:
    """Registry of Florence capabilities for deterministic help output."""
    return [
        {
            "id": "claims_count",
            "title": "Claim counts (open / closed / total)",
            "examples": [
                "How many claims do I have?",
                "How many open claims do I have?",
                "How many closed claims do I have?",
            ],
        },
        {
            "id": "system_overview",
            "title": "System overview (counts + outstanding billing)",
            "examples": [
                "System overview",
                "My system snapshot",
                "Diagnostic",
            ],
        },
        {
            "id": "claim_summary",
            "title": "Claim summary (deterministic header facts)",
            "examples": [
                "Summarize this claim",
                "Tell me about this claim",
                "Claim summary",
            ],
        },
        {
            "id": "billables",
            "title": "Billables (counts / uninvoiced / list / value breakdown)",
            "examples": [
                "Summarize billables",
                "List uninvoiced billables",
                "How much uninvoiced work is on this claim?",
            ],
        },
        {
            "id": "billable_aggregates",
            "title": "Billable totals (hours / miles / expenses)",
            "examples": [
                "How many hours have I billed on this claim?",
                "How many miles have I billed?",
                "How much EXP do I have?",
                "Total hours, miles, and expenses",
            ],
        },
        {
            "id": "invoices",
            "title": "Invoices (paid / unpaid / draft breakdown)",
            "examples": [
                "Invoice breakdown",
                "How many unpaid invoices do I have?",
            ],
        },
        {
            "id": "billing_totals",
            "title": "Billing totals (outstanding vs total)",
            "examples": [
                "How much billing do I have?",
                "How much outstanding billing do I have?",
                "How much total billed do I have?",
            ],
        },
        {
            "id": "top_uninvoiced",
            "title": "Top claims by uninvoiced hours",
            "examples": [
                "Top claims by uninvoiced hours",
                "Most uninvoiced hours",
            ],
        },
        {
            "id": "latest_work_status",
            "title": "Latest report work status (if reports exist)",
            "examples": [
                "What did the latest report say about work status?",
            ],
        },
    ]


def _format_capabilities_text() -> str:
    caps = get_capabilities()
    lines: List[str] = ["Here’s what I can do right now (local + deterministic):"]
    for c in caps:
        title = c.get("title") or "(unnamed)"
        lines.append(f"- {title}")
        ex = c.get("examples") or []
        if ex:
            lines.append(f"  e.g. {ex[0]}")
    lines.append("")
    lines.append("Tip: If you’re on a Claim Detail page, I’ll assume ‘this claim’ unless you say ‘all claims’.")
    return "\n".join(lines)


# -----------------------------
# Executive brevity helper
# -----------------------------
def _trim_to_brief(text: str) -> str:
    """
    Enforce executive brevity:
    - Max MAX_LINES_BRIEF lines
    - Drop trailing detail if longer
    """
    if not text:
        return text
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    if len(lines) <= MAX_LINES_BRIEF:
        return "\n".join(lines)
    trimmed = lines[:MAX_LINES_BRIEF]
    trimmed.append("…")
    return "\n".join(trimmed)

# -----------------------------
# Deterministic skills
# -----------------------------

def answer_billables_totals(*, db: Any, BillableItemModel: Any, claim_id: Optional[int] = None) -> Dict[str, float]:
    """
    Compute total billable quantities by type:
    - hours (non-EXP / non-MIL / non-NO BILL)
    - miles (MIL)
    - exp_dollars (EXP)
    - no_bill_hours (NO BILL)
    """
    q = db.session.query(BillableItemModel)
    if claim_id is not None:
        filt = _billable_claim_filter(BillableItemModel, claim_id)
        if filt is not None:
            q = q.filter(filt)

    hours = 0.0
    miles = 0.0
    exp_dollars = 0.0
    no_bill_hours = 0.0

    rows = q.all()
    for b in rows:
        activity = _get_first_attr(b, ["activity", "activity_code", "code"])
        activity = (str(activity or "")).upper().strip()
        qty = _get_first_attr(b, ["quantity", "qty", "hours", "units", "amount"])
        try:
            qty_val = float(qty)
        except Exception:
            continue

        if activity == "EXP":
            exp_dollars += qty_val
        elif activity == "MIL":
            miles += qty_val
        elif activity in {"NO BILL", "NOBILL"}:
            no_bill_hours += qty_val
        else:
            hours += qty_val

    return {
        "hours": float(hours),
        "miles": float(miles),
        "exp_dollars": float(exp_dollars),
        "no_bill_hours": float(no_bill_hours),
    }


# -----------------------------
# Billable mix/aggregate comparison helper
# -----------------------------
def derive_billable_mix(totals: Dict[str, float]) -> Dict[str, Any]:
    """
    Compute the mix of hours vs miles vs EXP (ignoring no_bill_hours).
    Returns:
        {
            "dominant": "hours" | "miles" | "expenses",
            "summary_text": <short executive summary>,
            "ratios": {"hours": %, "miles": %, "expenses": %}
        }
    """
    h = float(totals.get("hours", 0.0))
    m = float(totals.get("miles", 0.0))
    e = float(totals.get("exp_dollars", 0.0))
    # For ratios, treat all as-is (no normalization to units, just percentage of total sum)
    values = [h, m, e]
    total = h + m + e
    if total > 0:
        ratios = {
            "hours": h / total * 100,
            "miles": m / total * 100,
            "expenses": e / total * 100,
        }
    else:
        ratios = {"hours": 0.0, "miles": 0.0, "expenses": 0.0}
    # Dominant category
    dom = max(ratios.items(), key=lambda x: x[1])[0]
    dom_label = {"hours": "hours", "miles": "miles", "expenses": "expenses"}[dom]
    # Executive summary
    def _fmt_pct(val):
        return f"{val:.0f}%"
    parts = []
    if ratios["hours"] >= 1:
        parts.append(f"{_fmt_pct(ratios['hours'])} hours")
    if ratios["miles"] >= 1:
        parts.append(f"{_fmt_pct(ratios['miles'])} miles")
    if ratios["expenses"] >= 1:
        parts.append(f"{_fmt_pct(ratios['expenses'])} EXP")
    if not parts:
        summary = "No billable activity found."
    else:
        summary = f"Mostly {dom_label} ({', '.join(parts)})."
    return {
        "dominant": dom,
        "summary_text": summary,
        "ratios": ratios,
    }

# -----------------------------
# Billable comparison helpers
# -----------------------------

def compute_system_billable_totals(*, db: Any, BillableItemModel: Any) -> Dict[str, float]:
    """Aggregate billable totals across ALL claims."""
    return answer_billables_totals(db=db, BillableItemModel=BillableItemModel, claim_id=None)


def compare_claim_to_system(*, claim_totals: Dict[str, float], system_totals: Dict[str, float]) -> Dict[str, Any]:
    """
    Compare a single claim's billable mix against the system-wide average mix.
    Returns a short executive comparison.
    """
    claim_mix = derive_billable_mix(claim_totals)
    system_mix = derive_billable_mix(system_totals)

    dominant_claim = claim_mix["dominant"]
    dominant_system = system_mix["dominant"]

    if sum(claim_totals.values()) == 0:
        summary = "This claim has no billable activity to compare."
    elif dominant_claim == dominant_system:
        summary = f"This claim is typical — mostly {dominant_claim}, similar to your overall work mix."
    else:
        summary = (
            f"This claim skews toward {dominant_claim}, "
            f"while your overall work is mostly {dominant_system}."
        )

    return {
        "claim_mix": claim_mix,
        "system_mix": system_mix,
        "summary_text": summary,
    }
def answer_uninvoiced_billables_value(*, db: Any, BillableItemModel: Any, claim_id: Optional[int] = None) -> Dict[str, Any]:
    """
    Compute uninvoiced billables value breakdown (hours, miles, EXP dollars, no-bill hours).
    """
    from sqlalchemy import or_
    q = db.session.query(BillableItemModel)
    if claim_id is not None:
        filt = _billable_claim_filter(BillableItemModel, claim_id)
        if filt is not None:
            q = q.filter(filt)

    invoiced_filt = _billable_is_invoiced_filter(BillableItemModel)
    if invoiced_filt is not None:
        q = q.filter(~invoiced_filt)
    elif _claim_has_attr(BillableItemModel, "invoice_id"):
        q = q.filter(getattr(BillableItemModel, "invoice_id") == None)  # noqa: E711

    rows = q.all()
    hours = 0.0
    no_bill_hours = 0.0
    miles = 0.0
    exp_dollars = 0.0
    count = 0
    for b in rows:
        activity = _get_first_attr(b, ["activity", "activity_code", "code"])
        activity = (str(activity or "")).upper().strip()
        qty = _get_first_attr(b, ["quantity", "qty", "hours", "units", "amount"])
        try:
            qty_val = float(qty)
        except Exception:
            continue
        count += 1
        if activity == "EXP":
            exp_dollars += qty_val
        elif activity == "MIL":
            miles += qty_val
        elif activity in {"NO BILL", "NOBILL"}:
            no_bill_hours += qty_val
        else:
            hours += qty_val
    return {
        "uninvoiced_count": count,
        "hours": float(hours),
        "no_bill_hours": float(no_bill_hours),
        "miles": float(miles),
        "exp_dollars": float(exp_dollars),
    }


def answer_invoice_status_breakdown(*, db: Any, InvoiceModel: Any, claim_id: Optional[int] = None) -> Dict[str, Any]:
    """
    Compute invoice status breakdown (paid/unpaid/draft/other), with totals if possible.
    """
    from sqlalchemy import func, not_, or_
    q = db.session.query(InvoiceModel)
    if claim_id is not None:
        filt = _invoice_claim_filter(InvoiceModel, claim_id)
        if filt is not None:
            q = q.filter(filt)
    # Build status filters
    paid_filter = _invoice_is_paid_filter(InvoiceModel)
    status_col = getattr(InvoiceModel, "status", None)
    total = q.count()
    paid_q = q
    draft_q = q
    unpaid_q = q
    other_q = q
    # Paid
    if paid_filter is not None:
        paid_q = q.filter(paid_filter)
    elif status_col is not None:
        paid_q = q.filter(status_col.ilike("%paid%"))
    else:
        paid_q = q.filter("1=0")  # fallback, no paid
    paid_count = paid_q.count()
    # Draft
    if status_col is not None:
        draft_q = q.filter(status_col.ilike("%draft%"))
        draft_count = draft_q.count()
    else:
        draft_count = 0
    # Unpaid = not paid and not draft
    unpaid_ids = set()
    all_ids = set()
    if hasattr(InvoiceModel, "id"):
        # Use ids to exclude paid and draft
        all_rows = q.with_entities(getattr(InvoiceModel, "id")).all()
        all_ids = set(r[0] for r in all_rows)
        paid_ids = set(r[0] for r in paid_q.with_entities(getattr(InvoiceModel, "id")).all())
        draft_ids = set(r[0] for r in draft_q.with_entities(getattr(InvoiceModel, "id")).all())
        unpaid_ids = all_ids - paid_ids - draft_ids
        unpaid_count = len(unpaid_ids)
        # Other = total - paid - unpaid - draft
        other_count = total - paid_count - unpaid_count - draft_count
    else:
        unpaid_count = max(0, total - paid_count - draft_count)
        other_count = 0

    # Other = total - paid - unpaid - draft (may be 0)
    if other_count < 0:
        other_count = 0

    # Totals by dollars if possible
    total_expr = _invoice_total_expr(InvoiceModel)
    totals = None
    if total_expr is not None:
        def sum_total(qset):
            try:
                return float(qset.with_entities(func.coalesce(func.sum(total_expr), 0)).scalar() or 0.0)
            except Exception:
                return 0.0
        paid_total = sum_total(paid_q)
        draft_total = sum_total(draft_q)
        if unpaid_ids and hasattr(InvoiceModel, "id"):
            unpaid_total = 0.0
            if unpaid_ids:
                unpaid_total = float(q.filter(getattr(InvoiceModel, "id").in_(list(unpaid_ids))).with_entities(func.coalesce(func.sum(total_expr), 0)).scalar() or 0.0)
        else:
            unpaid_total = sum_total(unpaid_q)
        # Other: everything not in paid/draft/unpaid
        if other_count > 0 and hasattr(InvoiceModel, "id"):
            other_ids = all_ids - set(list(unpaid_ids)) - set(r[0] for r in paid_q.with_entities(getattr(InvoiceModel, "id")).all()) - set(r[0] for r in draft_q.with_entities(getattr(InvoiceModel, "id")).all())
            other_total = 0.0
            if other_ids:
                other_total = float(q.filter(getattr(InvoiceModel, "id").in_(list(other_ids))).with_entities(func.coalesce(func.sum(total_expr), 0)).scalar() or 0.0)
        else:
            other_total = 0.0
        total_sum = sum_total(q)
        totals = {
            "total": total_sum,
            "paid": paid_total,
            "unpaid": unpaid_total,
            "draft": draft_total,
            "other": other_total,
        }
    return {
        "total": int(total),
        "paid": int(paid_count),
        "unpaid": int(unpaid_count),
        "draft": int(draft_count),
        "other": int(other_count),
        "totals": totals,
    }


def top_claims_by_uninvoiced_hours(*, db: Any, ClaimModel: Any, BillableItemModel: Any, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Top-N claims by uninvoiced hours (sum of non-EXP/MIL/NO BILL activities).
    """
    from sqlalchemy import func, desc
    # Only uninvoiced billables
    q = db.session.query(
        getattr(BillableItemModel, "claim_id"),
        func.coalesce(func.sum(_billable_qty_expr(BillableItemModel)), 0).label("hours")
    )
    # Filter out EXP/MIL/NO BILL/NOBILL
    activity_col = None
    for attr in ["activity", "activity_code", "code"]:
        if _claim_has_attr(BillableItemModel, attr):
            activity_col = getattr(BillableItemModel, attr)
            break
    qty_expr = _billable_qty_expr(BillableItemModel)
    if activity_col is not None and qty_expr is not None:
        q = q.filter(~func.upper(activity_col).in_(["EXP", "MIL", "NO BILL", "NOBILL"]))
    elif qty_expr is None:
        return []
    # Only uninvoiced
    invoiced_filt = _billable_is_invoiced_filter(BillableItemModel)
    if invoiced_filt is not None:
        q = q.filter(~invoiced_filt)
    elif _claim_has_attr(BillableItemModel, "invoice_id"):
        q = q.filter(getattr(BillableItemModel, "invoice_id") == None)  # noqa: E711
    q = q.group_by(getattr(BillableItemModel, "claim_id"))
    q = q.order_by(desc("hours"))
    q = q.limit(limit)
    rows = q.all()
    out = []
    for claim_id, hours in rows:
        claim_number = ""
        claimant = ""
        try:
            claim = db.session.get(ClaimModel, claim_id)
            if claim:
                claim_number = _get_first_attr(claim, ["claim_number", "claim_no", "number"]) or str(claim_id)
                claimant = _get_first_attr(claim, ["claimant_name", "claimant"])
                if not claimant:
                    last = _get_first_attr(claim, ["claimant_last_name"])
                    first = _get_first_attr(claim, ["claimant_first_name"])
                    if last or first:
                        claimant = f"{first or ''} {last or ''}".strip()
        except Exception:
            claim_number = str(claim_id)
            claimant = ""
        out.append({
            "claim_id": claim_id,
            "claim_number": claim_number or str(claim_id),
            "claimant": claimant or "",
            "uninvoiced_hours": float(hours or 0.0),
        })
    return out

def _claim_has_attr(obj: Any, name: str) -> bool:
    try:
        getattr(obj, name)
        return True
    except Exception:
        return False


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
    """Return (count, label). Safe best-effort across differing Claim schemas."""
    if scope == "both":
        return db.session.query(ClaimModel).count(), "open + closed"

    filt = _claim_open_closed_filter(ClaimModel, scope)
    if filt is None:
        return db.session.query(ClaimModel).count(), "claims"

    return db.session.query(ClaimModel).filter(filt).count(), f"{scope} claims"


def _money(amount: Any) -> str:
    try:
        val = float(amount)
        return f"${val:,.2f}"
    except Exception:
        return "$0.00"


def _get_first_attr(obj: Any, names: List[str]) -> Any:
    for name in names:
        try:
            val = getattr(obj, name, None)
            if val is not None:
                return val
        except Exception:
            continue
    return None


def _invoice_is_paid_filter(model: Any):
    if _claim_has_attr(model, "is_paid"):
        return getattr(model, "is_paid") == True  # noqa: E712
    if _claim_has_attr(model, "paid_at"):
        return getattr(model, "paid_at") != None  # noqa: E711
    if _claim_has_attr(model, "status"):
        status_col = getattr(model, "status")
        return status_col.ilike("%paid%")
    return None


def _invoice_claim_filter(model: Any, claim_id: Any):
    if _claim_has_attr(model, "claim_id"):
        return getattr(model, "claim_id") == claim_id
    if _claim_has_attr(model, "claim"):
        try:
            rel = getattr(model, "claim")
            return rel.has(id=claim_id)
        except Exception:
            pass
    return None


def _invoice_total_expr(model: Any):
    for attr in ["balance_due", "amount_due", "total_amount", "total", "amount"]:
        if _claim_has_attr(model, attr):
            return getattr(model, attr)
    return None


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
    else:
        if _claim_has_attr(InvoiceModel, "status"):
            status_col = getattr(InvoiceModel, "status")
            q = q.filter(~status_col.ilike("%paid%"))

    count = q.count()

    total_expr = _invoice_total_expr(InvoiceModel)
    total = 0.0
    if total_expr is not None:
        total = q.with_entities(func.coalesce(func.sum(total_expr), 0)).scalar() or 0.0
    else:
        invoices = q.all()
        for inv in invoices:
            val = _get_first_attr(inv, ["balance_due", "amount_due", "total_amount", "total", "amount"])
            try:
                total += float(val)
            except Exception:
                continue

    label = "outstanding invoice" if count == 1 else "outstanding invoices"
    return {"count": count, "total": float(total), "label": label}


def answer_claim_field(*, db: Any, ClaimModel: Any, claim_id: int, field: str) -> Optional[str]:
    claim = None
    try:
        claim = db.session.get(ClaimModel, claim_id)
    except Exception:
        try:
            claim = ClaimModel.query.get(claim_id)
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

def answer_claim_summary(*, db: Any, ClaimModel: Any, claim_id: int, InvoiceModel: Any = None, BillableItemModel: Any = None) -> str:
    claim = None
    try:
        claim = db.session.get(ClaimModel, claim_id)
    except Exception:
        try:
            claim = ClaimModel.query.get(claim_id)
        except Exception:
            claim = None

    if claim is None:
        return "Claim not found."

    lines = []

    claimant_name = _get_first_attr(claim, ["claimant_name", "claimant"])
    if claimant_name is None:
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
            is_closed = getattr(claim, "is_closed")
            if is_closed is True:
                status = "closed"
            elif is_closed is False:
                status = "open"
        except Exception:
            pass
    if status is None and _claim_has_attr(claim, "closed_at"):
        try:
            closed_at = getattr(claim, "closed_at")
            status = "closed" if closed_at else "open"
        except Exception:
            pass
    if status is None and _claim_has_attr(claim, "status"):
        try:
            s = getattr(claim, "status")
            if s:
                status = s
        except Exception:
            pass
    if status:
        lines.append(f"Status: {status}")

    if InvoiceModel is not None:
        billing = answer_outstanding_billing(db=db, InvoiceModel=InvoiceModel, claim_id=claim_id)
        lines.append(f"Invoices: {billing['count']} outstanding totaling {_money(billing['total'])}")

    if BillableItemModel is not None:
        q = db.session.query(BillableItemModel)
        filt = None
        if _claim_has_attr(BillableItemModel, "claim_id"):
            filt = getattr(BillableItemModel, "claim_id") == claim_id
        elif _claim_has_attr(BillableItemModel, "claim"):
            try:
                rel = getattr(BillableItemModel, "claim")
                filt = rel.has(id=claim_id)
            except Exception:
                filt = None
        if filt is not None:
            q = q.filter(filt)
        count = q.count()
        lines.append(f"Billable Items: {count}")

    return "\n".join(lines)


def _billable_claim_filter(model: Any, claim_id: Any):
    if _claim_has_attr(model, "claim_id"):
        return getattr(model, "claim_id") == claim_id
    if _claim_has_attr(model, "claim"):
        try:
            rel = getattr(model, "claim")
            return rel.has(id=claim_id)
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


def answer_billables_summary(*, db: Any, BillableItemModel: Any, claim_id: Optional[int] = None) -> Dict[str, Any]:
    from sqlalchemy import func

    q = db.session.query(BillableItemModel)
    if claim_id is not None:
        filt = _billable_claim_filter(BillableItemModel, claim_id)
        if filt is not None:
            q = q.filter(filt)

    total_count = q.count()

    invoiced_filt = _billable_is_invoiced_filter(BillableItemModel)
    if invoiced_filt is not None:
        uninvoiced_q = q.filter(~invoiced_filt)
    else:
        if _claim_has_attr(BillableItemModel, "invoice_id"):
            uninvoiced_q = q.filter(getattr(BillableItemModel, "invoice_id") == None)  # noqa: E711
        else:
            uninvoiced_q = q

    uninvoiced_count = uninvoiced_q.count()

    qty_expr = _billable_qty_expr(BillableItemModel)
    uninvoiced_qty = None
    if qty_expr is not None:
        try:
            uninvoiced_qty = float(uninvoiced_q.with_entities(func.coalesce(func.sum(qty_expr), 0)).scalar() or 0.0)
        except Exception:
            uninvoiced_qty = None

    return {"total_count": int(total_count), "uninvoiced_count": int(uninvoiced_count), "uninvoiced_qty": uninvoiced_qty}


def list_uninvoiced_billables(*, db: Any, BillableItemModel: Any, claim_id: Optional[int] = None, limit: int = 10) -> List[Dict[str, Any]]:
    q = db.session.query(BillableItemModel)
    if claim_id is not None:
        filt = _billable_claim_filter(BillableItemModel, claim_id)
        if filt is not None:
            q = q.filter(filt)

    invoiced_filt = _billable_is_invoiced_filter(BillableItemModel)
    if invoiced_filt is not None:
        q = q.filter(~invoiced_filt)
    elif _claim_has_attr(BillableItemModel, "invoice_id"):
        q = q.filter(getattr(BillableItemModel, "invoice_id") == None)  # noqa: E711

    for dt_attr in ["entered_at", "created_at", "service_date", "dos", "date"]:
        if _claim_has_attr(BillableItemModel, dt_attr):
            try:
                q = q.order_by(getattr(BillableItemModel, dt_attr).desc())
                break
            except Exception:
                pass

    rows = q.limit(int(limit)).all()
    out: List[Dict[str, Any]] = []
    for b in rows:
        out.append(
            {
                "dos": _get_first_attr(b, ["service_date", "dos", "date"]) or "",
                "activity": _get_first_attr(b, ["activity", "activity_code", "code"]) or "",
                "qty": _get_first_attr(b, ["quantity", "qty", "hours", "units", "amount"]) or "",
                "description": _get_first_attr(b, ["description", "short_description", "desc"]) or "",
                "notes": _get_first_attr(b, ["notes", "note"]) or "",
            }
        )
    return out


def answer_latest_report_work_status(*, db: Any, ReportModel: Any, claim_id: int) -> Optional[str]:
    if ReportModel is None:
        return None

    q = db.session.query(ReportModel)

    if _claim_has_attr(ReportModel, "claim_id"):
        q = q.filter(getattr(ReportModel, "claim_id") == claim_id)
    elif _claim_has_attr(ReportModel, "claim"):
        try:
            q = q.filter(getattr(ReportModel, "claim").has(id=claim_id))
        except Exception:
            pass

    for attr in ["dos_end", "created_at", "updated_at", "id"]:
        if _claim_has_attr(ReportModel, attr):
            try:
                q = q.order_by(getattr(ReportModel, attr).desc())
                break
            except Exception:
                pass

    rpt = q.first()
    if not rpt:
        return None

    work = _get_first_attr(rpt, ["work_status", "work_status_text", "work_status_notes", "work_status_plan"]) or ""
    work = str(work).strip()

    rtype = _get_first_attr(rpt, ["report_type", "type"]) or "report"
    dos_start = _get_first_attr(rpt, ["dos_start"]) or ""
    dos_end = _get_first_attr(rpt, ["dos_end"]) or ""

    if work:
        header = f"Latest {rtype} report"
        if dos_start or dos_end:
            header += f" (DOS {dos_start} → {dos_end})"
        return f"{header}:\n{work}"

    return None


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
    lines: List[str] = []

    try:
        total_claims = db.session.query(ClaimModel).count()
        open_count, _ = answer_claim_count(scope="open", db=db, ClaimModel=ClaimModel)
        closed_count, _ = answer_claim_count(scope="closed", db=db, ClaimModel=ClaimModel)
        lines.append(f"Claims: {total_claims} total ({open_count} open, {closed_count} closed)")
    except Exception:
        pass

    if InvoiceModel is not None:
        try:
            total_invoices = db.session.query(InvoiceModel).count()
            billing = answer_outstanding_billing(db=db, InvoiceModel=InvoiceModel, claim_id=None)
            lines.append(f"Invoices: {total_invoices} total; {billing['count']} outstanding totaling {_money(billing['total'])}")
        except Exception:
            pass

    if BillableItemModel is not None:
        try:
            s = answer_billables_summary(db=db, BillableItemModel=BillableItemModel, claim_id=None)
            extra = ""
            if s.get("uninvoiced_qty") is not None:
                extra = f" (uninvoiced units sum: {s['uninvoiced_qty']:.2f})"
            lines.append(f"Billables: {s['total_count']} total; {s['uninvoiced_count']} uninvoiced{extra}")
        except Exception:
            pass

    for label, model in [
        ("Providers", ProviderModel),
        ("Employers", EmployerModel),
        ("Carriers", CarrierModel),
        ("Reports", ReportModel),
    ]:
        if model is None:
            continue
        try:
            lines.append(f"{label}: {db.session.query(model).count()}")
        except Exception:
            continue

    if not lines:
        return "I can’t see enough system data to summarize right now."

    return "\n".join(lines)


# -----------------------------
# LLM handoff/routing hint helper
# -----------------------------

def build_llm_handoff(*, question: str, context: Dict[str, Any], thread_state: Dict[str, Any]) -> Dict[str, Any]:
    """Return a deterministic hint payload for the LLM layer.

    This does NOT call the LLM. It only provides routing hints so the caller
    (ai_service) can choose the correct prompt builder and include the right context.
    """
    q = _qnorm(question)

    # Default handoff
    hint: Dict[str, Any] = {
        "mode": "read",          # read | summary | qa
        "task": "general",      # general | claim_summary | billables_summary | reports_summary | work_status
        "focus": None,           # optional short hint
        "claim_id": _ctx_claim_id(context, thread_state),
        "page_context": _ctx_page_context(context),
    }

    # Claim summaries / overviews
    if any(k in q for k in [
        "summarize this claim",
        "summarize claim",
        "claim summary",
        "tell me what you know about this claim",
        "tell me about this claim",
        "what do you know about this claim",
        "overview of this claim",
        "summary of this claim",
    ]):
        hint["mode"] = "summary"
        hint["task"] = "claim_summary"
        hint["focus"] = "Summarize the claim using the provided context; include key parties, status, dates, latest report highlights, billables/invoices." 
        return hint

    # Billables summary
    if "billable" in q or "billables" in q:
        if "summar" in q or "overview" in q:
            hint["mode"] = "summary"
            hint["task"] = "billables_summary"
            hint["focus"] = "Summarize billables; call out uninvoiced items and notable notes/description patterns." 
            return hint
        hint["mode"] = "qa"
        hint["task"] = "billables_qa"
        hint["focus"] = "Answer using billables (including description + notes) from context." 
        return hint

    # Reports summary / work status
    if "report" in q or "reports" in q:
        if "latest" in q and "work" in q:
            hint["mode"] = "qa"
            hint["task"] = "work_status"
            hint["focus"] = "Use the latest report(s) to answer about work status; quote the relevant excerpt(s) if present." 
            return hint
        if "summar" in q or "overview" in q:
            hint["mode"] = "summary"
            hint["task"] = "reports_summary"
            hint["focus"] = "Summarize report history with emphasis on the most recent report and trends." 
            return hint
        hint["mode"] = "qa"
        hint["task"] = "reports_qa"
        hint["focus"] = "Answer using report content from context." 
        return hint

    # System-level overview
    if any(k in q for k in ["my system", "system overview", "snapshot", "diagnostic"]):
        hint["mode"] = "summary"
        hint["task"] = "system_overview"
        hint["focus"] = "Summarize system-wide counts and notable items." 
        return hint

    return hint


# -----------------------------
# Chat turn orchestrator
# -----------------------------

@dataclass(frozen=True)
class ChatTurn:
    response: Dict[str, Any]
    thread_state: Dict[str, Any]


def handle_chat_turn(
    *,
    question: str,
    context: Dict[str, Any],
    thread_state: Optional[Dict[str, Any]] = None,
    db: Any,
    ClaimModel: Any,
    InvoiceModel: Any = None,
    BillableItemModel: Any = None,
    ReportModel: Any = None,
) -> Optional[ChatTurn]:
    """Handle a single chat turn deterministically when possible.

    Returns ChatTurn if handled here; otherwise returns None (caller may fall back to retrieval/LLM).
    Frame stack reset: If the user asks a new top-level/system question, navigates away from claim context,
    or explicitly requests a reset, clear the frame stack to avoid stale context.
    """
    ts: Dict[str, Any] = dict(thread_state or {})

    # --- FRAME STACK: maintain hierarchical frame navigation in thread_state["frame_stack"] ---
    # The stack holds the current frame context(s) in order, most specific last.
    # All frame transitions should push to the stack (without consecutive duplicates).

    # Reset frame stack if context clearly changes (see helper for rules)
    maybe_reset_frame_stack(question, context, ts)

    # Persist last-known context so follow-ups like "both" can be resolved reliably.
    page_ctx = _ctx_page_context(context)
    ts["last_page_context"] = page_ctx

    cid = _ctx_claim_id(context, ts)
    if cid is not None:
        ts["last_claim_id"] = cid

    # 1) Resolve pending clarifications
    pending, choice = maybe_resolve_pending_choice(ts, question)
    if pending and choice:
        intent = pending.get("intent")
        slot = pending.get("slot")
        original_q = (pending.get("original_question") or "").strip()

        # Back-compat for older pending shape
        if not slot:
            slots = pending.get("slots")
            if isinstance(slots, dict) and len(slots) == 1:
                slot = next(iter(slots.keys()))

        ts["pending"] = None  # always clear on recognized choice
        ts.pop("last_clarify_intent", None)
        ts.pop("last_clarify_slot", None)
        ts.pop("last_clarify_original_question", None)

        # CLAIM COUNT
        if intent == "claim_count" and slot == "claim_status":
            count, label = answer_claim_count(scope=choice, db=db, ClaimModel=ClaimModel)
            ts["last_intent"] = "claim_count"
            return ChatTurn(
                response=make_answer(text=f"There are {count} {label}.", thread_state_update=ts),
                thread_state=ts,
            )

        # BILLING TOTALS
        if intent == "billing_total" and slot == "billing_scope":
            # Prefer rewriting from the original question so follow-ups like "both"/"outstanding"
            # are treated as answers to the clarify, not as a new question.
            base = original_q or "how much billing do I have?"
            if choice == "outstanding":
                rewritten = f"{base} (outstanding/unpaid only)"
            else:
                rewritten = f"{base} (total billed)"

            return handle_chat_turn(
                question=rewritten,
                context=context,
                thread_state=ts,
                db=db,
                ClaimModel=ClaimModel,
                InvoiceModel=InvoiceModel,
                BillableItemModel=BillableItemModel,
                ReportModel=ReportModel,
            )

    # 2) Canonicalize frame-relative follow-ups (if any), before intent detection.
    canonical_frame_q, frame_was_rewritten = maybe_canonicalize_frame_followup(question, ts)
    if frame_was_rewritten:
        ts["last_canonical_question"] = canonical_frame_q
        ts["last_followup_rewrite"] = True
        q = _qnorm(canonical_frame_q)
    else:
        # 3) Canonicalize intent follow-up (existing logic)
        canonical_q, was_rewritten = maybe_canonicalize_followup(question, ts)
        if was_rewritten:
            ts["last_canonical_question"] = canonical_q
            ts["last_followup_rewrite"] = True
            q = _qnorm(canonical_q)
        else:
            q = _qnorm(question)

    # CAPABILITIES / HELP
    if any(k in q for k in [
        "what can you do",
        "what can florence do",
        "help",
        "capabilities",
        "commands",
        "examples",
        "what do you do",
    ]):
        return ChatTurn(
            response=make_answer(text=_trim_to_brief(_format_capabilities_text()), thread_state_update=ts),
            thread_state=ts,
        )

    # TOP CLAIMS BY UNINVOICED HOURS (run before system overview)
    if (("top claims" in q or "most uninvoiced" in q or "largest uninvoiced" in q or "uninvoiced hours" in q)
        and (BillableItemModel is not None)
        and ("claim" in q or "claims" in q)
        and _ctx_claim_id(context, ts) is None):
        rows = top_claims_by_uninvoiced_hours(db=db, ClaimModel=ClaimModel, BillableItemModel=BillableItemModel, limit=5)
        if not rows:
            return ChatTurn(response=make_answer(text=_trim_to_brief("No uninvoiced hours found across claims."), thread_state_update=ts), thread_state=ts)
        lines = ["Top claims by uninvoiced hours:"]
        for r in rows:
            claim_number = r.get("claim_number", str(r.get("claim_id", "")))
            claimant = r.get("claimant", "")
            hours = r.get("uninvoiced_hours", 0.0)
            label = f"{claim_number}"
            if claimant:
                label += f" | {claimant}"
            label += f" — {hours:.2f} hr"
            lines.append(f"- {label}")
        return ChatTurn(response=make_answer(text=_trim_to_brief("\n".join(lines)), thread_state_update=ts), thread_state=ts)

    # SYSTEM OVERVIEW
    if any(k in q for k in ["my system", "system overview", "system snapshot", "overview", "snapshot", "diagnostic"]):
        if "claim" not in q and "report" not in q and "billable" not in q and "invoice" not in q:
            ProviderModel = context.get("ProviderModel")
            EmployerModel = context.get("EmployerModel")
            CarrierModel = context.get("CarrierModel")
            ReportModel2 = context.get("ReportModel") or ReportModel
            text = answer_system_overview(
                db=db,
                ClaimModel=ClaimModel,
                InvoiceModel=InvoiceModel,
                BillableItemModel=BillableItemModel,
                ProviderModel=ProviderModel,
                EmployerModel=EmployerModel,
                CarrierModel=CarrierModel,
                ReportModel=ReportModel2,
            )
            ts["last_intent"] = "system_overview"
            # --- FRAME STACK LOGIC: push "system_overview" frame if not already top ---
            frame_stack = ts.get("frame_stack") or []
            if not isinstance(frame_stack, list):
                frame_stack = []
            if not frame_stack or frame_stack[-1] != "system_overview":
                frame_stack = frame_stack + ["system_overview"]
            ts["frame_stack"] = frame_stack
            ts["last_frame"] = "system_overview"  # For backward compatibility
            return ChatTurn(response=make_answer(text=_trim_to_brief(text), thread_state_update=ts), thread_state=ts)

    # INVOICE STATUS BREAKDOWN (run before claim count)
    if ("invoice" in q and any(k in q for k in ["how many", "count", "breakdown", "paid", "unpaid", "draft"])):
        if InvoiceModel is None:
            return ChatTurn(response=make_answer(text=_trim_to_brief("Invoice data isn’t available yet."), thread_state_update=ts), thread_state=ts)
        claim_id = None
        if mentions_this_claim(question):
            claim_id = _ctx_claim_id(context, ts)
        elif "this" in q and "claim" in q:
            claim_id = _ctx_claim_id(context, ts)
        elif context.get("claim_id") is not None:
            if _ctx_page_context(context) == "claim_detail" or "claim" in q:
                claim_id = _ctx_claim_id(context, ts)
        breakdown = answer_invoice_status_breakdown(db=db, InvoiceModel=InvoiceModel, claim_id=claim_id)
        total = breakdown.get("total", 0)
        paid = breakdown.get("paid", 0)
        unpaid = breakdown.get("unpaid", 0)
        draft = breakdown.get("draft", 0)
        other = breakdown.get("other", 0)
        txt = f"Invoices: {total} total ({paid} paid, {unpaid} unpaid, {draft} draft, {other} other)."
        totals = breakdown.get("totals")
        if totals:
            paid_amt = _money(totals.get("paid", 0.0))
            unpaid_amt = _money(totals.get("unpaid", 0.0))
            draft_amt = _money(totals.get("draft", 0.0))
            txt += f" Totals: paid {paid_amt}, unpaid {unpaid_amt}, draft {draft_amt}."
        return ChatTurn(response=make_answer(text=_trim_to_brief(txt), thread_state_update=ts), thread_state=ts)

    # CLAIM HEADER FIELD LOOKUPS (DOB, DOI, adjuster, etc.)
    if any(k in q for k in ["dob", "date of birth", "doi", "date of injury", "adjuster", "claim state"]):
        claim_id = _ctx_claim_id(context, ts)
        if claim_id is None:
            return ChatTurn(
                response=make_answer(text=_trim_to_brief("Please open a claim first."), thread_state_update=ts),
                thread_state=ts,
            )

        field = None
        if "dob" in q or "date of birth" in q:
            field = "dob"
        elif "doi" in q or "date of injury" in q:
            field = "doi"
        elif "adjuster" in q:
            field = "adjuster"
        elif "claim state" in q or "state" in q:
            field = "claim_state"

        if field:
            val = answer_claim_field(db=db, ClaimModel=ClaimModel, claim_id=claim_id, field=field)
            if val:
                return ChatTurn(
                    response={
                        **make_answer(text=_trim_to_brief(val), thread_state_update=ts),
                        "citations": [
                            {
                                "type": "model_field",
                                "label": f"Claim.{field}",
                                "ref": f"claim.{field}",
                                "confidence": 1.0,
                            }
                        ],
                    },
                    thread_state=ts,
                )

            return ChatTurn(
                response=make_answer(
                    text=_trim_to_brief(f"I couldn’t find that field on this claim."),
                    thread_state_update=ts,
                ),
                thread_state=ts,
            )

    # CLAIM SUMMARY (natural phrasing)
    if any(k in q for k in ["tell me what you know", "tell me about this claim", "what do you know about this claim", "claim summary"]):
        claim_id = _ctx_claim_id(context, ts)
        if claim_id is None:
            return ChatTurn(response=make_answer(text=_trim_to_brief("Please open a claim first, then ask again."), thread_state_update=ts), thread_state=ts)
        summary = answer_claim_summary(db=db, ClaimModel=ClaimModel, claim_id=claim_id, InvoiceModel=InvoiceModel, BillableItemModel=BillableItemModel)
        ts["last_intent"] = "claim_summary"
        # --- FRAME STACK LOGIC: push "claim_overview" frame if not already top ---
        frame_stack = ts.get("frame_stack") or []
        if not isinstance(frame_stack, list):
            frame_stack = []
        if not frame_stack or frame_stack[-1] != "claim_overview":
            frame_stack = frame_stack + ["claim_overview"]
        ts["frame_stack"] = frame_stack
        ts["last_frame"] = "claim_overview"  # For backward compatibility
        return ChatTurn(response=make_answer(text=_trim_to_brief(summary), thread_state_update=ts), thread_state=ts)

    # LATEST REPORT: work status
    if ("latest report" in q or "last report" in q) and ("work status" in q or "work" in q):
        claim_id = _ctx_claim_id(context, ts)
        if claim_id is None:
            return ChatTurn(response=make_answer(text=_trim_to_brief("Please open a claim first, then ask about the latest report."), thread_state_update=ts), thread_state=ts)
        if ReportModel is None:
            return ChatTurn(response=make_answer(text=_trim_to_brief("Report lookup isn’t available yet (missing Report model)."), thread_state_update=ts), thread_state=ts)
        text = answer_latest_report_work_status(db=db, ReportModel=ReportModel, claim_id=claim_id)
        if not text:
            return ChatTurn(response=make_answer(text=_trim_to_brief("I couldn't find a report work status for that claim."), thread_state_update=ts), thread_state=ts)
        return ChatTurn(response=make_answer(text=_trim_to_brief(text), thread_state_update=ts), thread_state=ts)

    # BILLABLE MIX/DOMINANCE/COMPOSITION intent (run before billable totals)
    if any(k in q for k in ["mix", "mostly", "dominant", "breakdown", "what kind of work"]):
        if BillableItemModel is None:
            return ChatTurn(
                response=make_answer(text=_trim_to_brief("Billables aren’t available yet."), thread_state_update=ts),
                thread_state=ts,
            )
        claim_id = None
        if mentions_this_claim(question):
            claim_id = _ctx_claim_id(context, ts)
        elif _ctx_page_context(context) == "claim_detail":
            claim_id = _ctx_claim_id(context, ts)
        totals = answer_billables_totals(db=db, BillableItemModel=BillableItemModel, claim_id=claim_id)
        mix = derive_billable_mix(totals)
        text = mix["summary_text"]
        scope_txt = "this claim" if claim_id is not None else "all claims"
        if "No billable activity" not in text:
            text = f"{text} (for {scope_txt})"
        resp = make_answer(text=_trim_to_brief(text), thread_state_update=ts)
        resp["citations"] = [
            {"type": "billable_aggregate_mix", "claim_id": claim_id, "confidence": 1.0},
        ]
        return ChatTurn(
            response=resp,
            thread_state=ts,
        )

    # BILLABLE COMPARISON: claim vs system average
    if any(k in q for k in ["compare", "typical", "unusual", "normal"]) and any(k in q for k in ["claim", "work"]):
        if BillableItemModel is None:
            return ChatTurn(
                response=make_answer(text=_trim_to_brief("Billables aren’t available yet."), thread_state_update=ts),
                thread_state=ts,
            )

        claim_id = _ctx_claim_id(context, ts)
        if claim_id is None:
            return ChatTurn(
                response=make_answer(text=_trim_to_brief("Please open a claim to compare it."), thread_state_update=ts),
                thread_state=ts,
            )

        claim_totals = answer_billables_totals(db=db, BillableItemModel=BillableItemModel, claim_id=claim_id)
        system_totals = compute_system_billable_totals(db=db, BillableItemModel=BillableItemModel)

        comparison = compare_claim_to_system(
            claim_totals=claim_totals,
            system_totals=system_totals,
        )

        resp = make_answer(
            text=_trim_to_brief(comparison["summary_text"]),
            thread_state_update=ts,
        )
        resp["citations"] = [
            {"type": "billable_aggregate_mix", "claim_id": claim_id, "confidence": 1.0},
            {"type": "billable_aggregate_mix", "claim_id": None, "confidence": 1.0},
        ]
        return ChatTurn(response=resp, thread_state=ts)

    # Explicit billable aggregate intent (short-circuit before summaries)
    if any(k in q for k in ["hours", "miles", "expense", "expenses", "exp"]) and (
        "how many" in q or "total" in q or "sum" in q
    ):
        pass
    # BILLABLE TOTALS: hours / miles / EXP (claim-scoped or system)
    if any(k in q for k in ["how many", "total", "sum"]) and any(k in q for k in ["hour", "hours", "mile", "miles", "exp", "expense", "expenses"]):
        if BillableItemModel is None:
            return ChatTurn(
                response=make_answer(text=_trim_to_brief("Billables aren’t available yet."), thread_state_update=ts),
                thread_state=ts,
            )

        claim_id = None
        if mentions_this_claim(question):
            claim_id = _ctx_claim_id(context, ts)
        elif _ctx_page_context(context) == "claim_detail":
            claim_id = _ctx_claim_id(context, ts)

        totals = answer_billables_totals(db=db, BillableItemModel=BillableItemModel, claim_id=claim_id)

        parts = [
            f"{totals['hours']:.2f} hr",
            f"{totals['miles']:.2f} mi",
            f"${totals['exp_dollars']:,.2f} EXP",
        ]

        scope_txt = "this claim" if claim_id is not None else "all claims"
        text = f"Totals for {scope_txt}: " + ", ".join(parts) + "."

        resp = make_answer(text=_trim_to_brief(text), thread_state_update=ts)
        resp["citations"] = [
            {"type": "billable_aggregate", "metric": "hours", "claim_id": claim_id, "confidence": 1.0},
            {"type": "billable_aggregate", "metric": "miles", "claim_id": claim_id, "confidence": 1.0},
            {"type": "billable_aggregate", "metric": "expenses", "claim_id": claim_id, "confidence": 1.0},
        ]

        return ChatTurn(
            response=resp,
            thread_state=ts,
        )

    # BILLABLES: summarize / list uninvoiced (includes description + notes)
    if "billable" in q or "billables" in q:
        from typing import Optional
        claim_id: Optional[int] = None

        # Prefer explicit phrasing like "this claim" / "on this claim".
        if mentions_this_claim(question):
            claim_id = _ctx_claim_id(context, ts)

        # If we're on a claim_detail page (or have a claim_id in context), treat billables questions
        # as claim-scoped by default unless the user explicitly asks for "all claims".
        if claim_id is None:
            pc = _ctx_page_context(context)
            if (pc == "claim_detail" or context.get("claim_id") is not None) and "all claims" not in q:
                claim_id = _ctx_claim_id(context, ts)

        # Uninvoiced value breakdown (run before uninvoiced list)
        if (
            any(k in q for k in ["uninvoiced", "not invoiced", "unbilled"])
            and any(k in q for k in ["value", "worth", "total", "sum", "how much"])
        ):
            if BillableItemModel is None:
                return ChatTurn(response=make_answer(text=_trim_to_brief("Billables aren’t available yet (missing BillableItem model)."), thread_state_update=ts), thread_state=ts)
            s = answer_uninvoiced_billables_value(db=db, BillableItemModel=BillableItemModel, claim_id=claim_id)
            hours = s.get("hours", 0.0)
            no_bill_hours = s.get("no_bill_hours", 0.0)
            miles = s.get("miles", 0.0)
            exp_dollars = s.get("exp_dollars", 0.0)
            count = s.get("uninvoiced_count", 0)
            if claim_id is not None:
                txt = f"Uninvoiced on this claim: {count} items — {hours:.2f} hr ({no_bill_hours:.2f} NO BILL), {miles:.2f} mi, ${exp_dollars:,.2f} EXP."
            else:
                txt = f"Uninvoiced across all claims: {count} items — {hours:.2f} hr ({no_bill_hours:.2f} NO BILL), {miles:.2f} mi, ${exp_dollars:,.2f} EXP."
            return ChatTurn(response=make_answer(text=_trim_to_brief(txt), thread_state_update=ts), thread_state=ts)

        # Billables count (e.g. "how many billable items are on this claim?")
        if ("how many" in q or "count" in q or "number of" in q) and ("billable" in q or "billables" in q):
            if BillableItemModel is None:
                return ChatTurn(
                    response=make_answer(text=_trim_to_brief("Billables aren’t available yet (missing BillableItem model)."), thread_state_update=ts),
                    thread_state=ts,
                )

            # If the user asks about invoicing status, prefer uninvoiced counts.
            wants_uninvoiced = any(
                k in q
                for k in [
                    "uninvoiced",
                    "un-invoiced",
                    "not invoiced",
                    "not been invoiced",
                    "unbilled",
                    "not billed",
                    "unpaid",
                ]
            )

            s = answer_billables_summary(db=db, BillableItemModel=BillableItemModel, claim_id=claim_id)

            if wants_uninvoiced:
                if claim_id is not None:
                    return ChatTurn(
                        response=make_answer(
                            text=_trim_to_brief(f"This claim has {s['uninvoiced_count']} uninvoiced billable item(s) (out of {s['total_count']} total)."),
                            thread_state_update=ts,
                        ),
                        thread_state=ts,
                    )

                return ChatTurn(
                    response=make_answer(
                        text=_trim_to_brief(f"Across all claims, there are {s['uninvoiced_count']} uninvoiced billable item(s) (out of {s['total_count']} total)."),
                        thread_state_update=ts,
                    ),
                    thread_state=ts,
                )

            if claim_id is not None:
                return ChatTurn(
                    response=make_answer(text=_trim_to_brief(f"This claim has {s['total_count']} billable item(s)."), thread_state_update=ts),
                    thread_state=ts,
                )

            return ChatTurn(
                response=make_answer(text=_trim_to_brief(f"Across all claims, there are {s['total_count']} billable item(s)."), thread_state_update=ts),
                thread_state=ts,
            )

        if "summarize" in q or "summary" in q:
            if BillableItemModel is None:
                return ChatTurn(response=make_answer(text=_trim_to_brief("Billables aren’t available yet (missing BillableItem model)."), thread_state_update=ts), thread_state=ts)
            s = answer_billables_summary(db=db, BillableItemModel=BillableItemModel, claim_id=claim_id)
            scope_txt = "this claim" if claim_id is not None else "all claims"
            extra = ""
            if s.get("uninvoiced_qty") is not None:
                extra = f" (uninvoiced units sum: {s['uninvoiced_qty']:.2f})"
            text = f"Billables for {scope_txt}: {s['total_count']} total; {s['uninvoiced_count']} uninvoiced{extra}."
            return ChatTurn(response=make_answer(text=_trim_to_brief(text), thread_state_update=ts), thread_state=ts)

        if "uninvoiced" in q or "not been invoiced" in q or "not invoiced" in q or "unbilled" in q:
            if BillableItemModel is None:
                return ChatTurn(response=make_answer(text=_trim_to_brief("Billables aren’t available yet (missing BillableItem model)."), thread_state_update=ts), thread_state=ts)
            rows = list_uninvoiced_billables(db=db, BillableItemModel=BillableItemModel, claim_id=claim_id, limit=10)
            if not rows:
                return ChatTurn(response=make_answer(text=_trim_to_brief("No uninvoiced billables found."), thread_state_update=ts), thread_state=ts)
            lines = ["Here are up to 10 uninvoiced billables:"]
            for r in rows:
                dos = r.get("dos") or ""
                activity = r.get("activity") or ""
                qty = r.get("qty") or ""
                desc = r.get("description") or ""
                notes = r.get("notes") or ""
                tail = f" — {desc}" if desc else ""
                if notes:
                    tail += f" (notes: {notes})"
                lines.append(f"- {dos} | {activity} | {qty}{tail}")
            return ChatTurn(response=make_answer(text=_trim_to_brief("\n".join(lines)), thread_state_update=ts), thread_state=ts)

    # Billing totals (system-wide or claim-scoped)
    if ("bill" in q or "invoice" in q) and ("how much" in q or "total" in q or "amount" in q):
        billing_scope = extract_billing_scope(q)
        if not billing_scope:
            resp = make_clarify(
                text="Do you mean outstanding (unpaid) billing, or total billed?",
                action=make_action_choose_one("billing_scope", [("Outstanding", "outstanding"), ("Total billed", "total")]),
                thread_state_update={
                    **ts,
                    "pending": {
                        "intent": "billing_total",
                        "slot": "billing_scope",
                        "original_question": question,
                    },
                },
            )
            ts = dict(resp.get("thread_state_update") or ts)
            return ChatTurn(response=resp, thread_state=ts)

        claim_id = None
        if mentions_this_claim(q):
            claim_id = _ctx_claim_id(context, ts)
        elif "this" in q and "claim" in q:
            claim_id = _ctx_claim_id(context, ts)
        elif context.get("claim_id") is not None:
            if _ctx_page_context(context) == "claim_detail" or "claim" in q:
                claim_id = _ctx_claim_id(context, ts)

        if InvoiceModel is None:
            return ChatTurn(response=make_answer(text=_trim_to_brief("Billing totals aren’t available yet (missing Invoice model)."), thread_state_update=ts), thread_state=ts)

        if billing_scope == "outstanding":
            billing = answer_outstanding_billing(db=db, InvoiceModel=InvoiceModel, claim_id=claim_id)
            money = _money(billing["total"])
            if claim_id is not None:
                text = f"This claim has {billing['count']} outstanding invoices totaling {money}."
            else:
                text = f"You have {billing['count']} outstanding invoices totaling {money}."
            return ChatTurn(response=make_answer(text=_trim_to_brief(text), thread_state_update=ts), thread_state=ts)

        if billing_scope == "total":
            from sqlalchemy import func

            qinv = db.session.query(InvoiceModel)
            if claim_id is not None:
                filt = _invoice_claim_filter(InvoiceModel, claim_id)
                if filt is not None:
                    qinv = qinv.filter(filt)

            total_expr = _invoice_total_expr(InvoiceModel)
            total = 0.0
            if total_expr is not None:
                total = qinv.with_entities(func.coalesce(func.sum(total_expr), 0)).scalar() or 0.0
            else:
                invoices = qinv.all()
                for inv in invoices:
                    val = _get_first_attr(inv, ["balance_due", "amount_due", "total_amount", "total", "amount"])
                    try:
                        total += float(val)
                    except Exception:
                        continue

            count = qinv.count()
            money = _money(total)
            if claim_id is not None:
                text = f"This claim has {count} invoices totaling {money}."
            else:
                text = f"You have {count} invoices totaling {money}."
            return ChatTurn(response=make_answer(text=_trim_to_brief(text), thread_state_update=ts), thread_state=ts)

    # Deterministic claim summary (explicit summarize)
    if ("summarize" in q and "claim" in q) or (q in {"summarize this claim", "summarize claim"}):
        claim_id = _ctx_claim_id(context, ts)
        if claim_id is None:
            return ChatTurn(response=make_answer(text=_trim_to_brief("Please open a claim first to get a summary."), thread_state_update=ts), thread_state=ts)
        summary = answer_claim_summary(db=db, ClaimModel=ClaimModel, claim_id=claim_id, InvoiceModel=InvoiceModel, BillableItemModel=BillableItemModel)
        ts["last_intent"] = "claim_summary"
        # --- FRAME STACK LOGIC: push "claim_overview" frame if not already top ---
        frame_stack = ts.get("frame_stack") or []
        if not isinstance(frame_stack, list):
            frame_stack = []
        if not frame_stack or frame_stack[-1] != "claim_overview":
            frame_stack = frame_stack + ["claim_overview"]
        ts["frame_stack"] = frame_stack
        ts["last_frame"] = "claim_overview"  # For backward compatibility
        return ChatTurn(response=make_answer(text=_trim_to_brief(summary), thread_state_update=ts), thread_state=ts)

    # Claim count
    if ("how many" in q or "count" in q) and "claim" in q and "billable" not in q and "billables" not in q:
        scope = extract_claim_status_scope(q)
        if not scope:
            # Default to total (open + closed) for generic count questions.
            # This avoids relying on thread_state for the “open/closed/both” follow-up.
            scope = "both"

        count, label = answer_claim_count(scope=scope, db=db, ClaimModel=ClaimModel)
        ts["last_intent"] = "claim_count"
        return ChatTurn(response=make_answer(text=_trim_to_brief(f"There are {count} {label}."), thread_state_update=ts), thread_state=ts)

    return None


# -----------------------------
# Public entry point used by ai_service.py
# -----------------------------

def respond(question: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Primary public entry point.

    Contract:
    - Return a Florence-shaped dict when handled.
    - Return {"handled": False} when not handled.

    Note: `thread_state` can be passed either as:
      - context["thread_state"]
      - context["thread_state_update"] (treated same)
    """
    ctx: Dict[str, Any] = dict(context or {})
    thread_state = ctx.get("thread_state") or ctx.get("thread_state_update") or {}

    try:
        from app.extensions import db
        from app.models import Claim, Invoice, BillableItem, Report, Provider, Employer, Carrier
    except Exception:
        try:
            from app.extensions import db
            from app.models import Claim
            Invoice = None
            BillableItem = None
            Report = None
            Provider = None
            Employer = None
            Carrier = None
        except Exception:
            return {"handled": False}

    # Provide optional models through context for overview handlers.
    ctx.setdefault("ProviderModel", Provider)
    ctx.setdefault("EmployerModel", Employer)
    ctx.setdefault("CarrierModel", Carrier)
    ctx.setdefault("ReportModel", Report)

    # Ensure thread_state_update is stable even on handoff
    ts = dict(thread_state or {})
    ts["last_page_context"] = _ctx_page_context(ctx)
    cid = _ctx_claim_id(ctx, ts)
    if cid is not None:
        ts["last_claim_id"] = cid

    turn = handle_chat_turn(
        question=question,
        context=ctx,
        thread_state=ts,
        db=db,
        ClaimModel=Claim,
        InvoiceModel=Invoice,
        BillableItemModel=BillableItem,
        ReportModel=Report,
    )

    if not turn:
        return {
            "handled": False,
            "ok": True,
            "thread_state_update": ts,
            "llm_handoff": build_llm_handoff(question=question, context=ctx, thread_state=ts),
        }

    resp = dict(turn.response)
    if "thread_state_update" not in resp:
        resp["thread_state_update"] = turn.thread_state

    resp.setdefault("handled", True)
    resp.setdefault("ok", True)

    # --- Safety: never emit raw schema / empty answers to chat ---
    ans = resp.get("answer")
    if ans is None or (isinstance(ans, str) and ans.strip() == ""):
        # Provide a human fallback instead of leaking structured schema
        resp["answer"] = _trim_to_brief(
            "I found relevant information, but I need to summarize it more clearly.\n"
            "Try: 'Summarize this claim' or 'Summarize billables on this claim'."
        )
        resp["is_guess"] = True
        resp["confidence"] = resp.get("confidence") or 0.3
        resp["answer_mode"] = "fallback"

    return resp


# Backwards-compatible aliases (ai_service.py may probe these names)
chat_respond = respond
handle_chat = respond
handle_message = respond
ask = respond
run = respond