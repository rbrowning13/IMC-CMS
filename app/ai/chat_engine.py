from __future__ import annotations

from typing import Any, Dict, Tuple, Optional, List
from ai.sources import (
    _claim_has_attr,
    _get_first_attr,
    _money,
    _claim_open_closed_filter,
    answer_claim_count,
    answer_claim_field,
    answer_claim_summary,
    _invoice_is_paid_filter,
    _invoice_claim_filter,
    _invoice_total_expr,
    answer_invoice_status_breakdown,
    answer_outstanding_billing,
    _billable_claim_filter,
    _billable_is_invoiced_filter,
    _billable_qty_expr,
    answer_billables_totals,
    answer_billables_summary,
    answer_uninvoiced_billables_value,
    list_uninvoiced_billables,
    derive_billable_mix,
    compute_system_billable_totals,
    compare_claim_to_system,
    top_claims_by_uninvoiced_hours,
    answer_latest_report_work_status,
    answer_system_overview,
)
from ai.confidence import compute_confidence
# --- CLAIM-SCOPED ORCHESTRATION (delegated) ---
from ai.claims import (
    handle_claim_summary,
    handle_claim_billing,
    handle_claim_billables,
    handle_claim_work_status,
    handle_claim_count,
)
# --- Frame logic imports from ai.frames ---
# --- Frame logic imports from ai.frames ---
# (frame helpers now imported from ai.frames; local definitions removed below)
from ai.frames import (
    maybe_reset_frame_stack,
    FRAME_REGISTRY,
    maybe_canonicalize_frame_followup,
)

# --- Routing table import ---
from ai.routing import ROUTES
# --- Intent detection import ---
from ai.intents import detect_intent
"""app.ai.chat_engine

Lightweight, deterministic chat orchestration helpers for Florence.

Goals:
- Keep conversation state ("thread_state") small and explicit.
- Support clarifying questions via structured `action` payloads.
- Prefer deterministic answers (DB + retrieval) before LLM.

This module is intentionally dependency-light and safe to call from services.
"""


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
        "confidence": compute_confidence(text=text, had_data=True),
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
        "model_source": "system",
        "model": None,
        "local_only": True,
        "answer_mode": "brief",
    }
    if thread_state_update is not None:
        out["thread_state_update"] = thread_state_update
    out["confidence"] = compute_confidence(text=text, had_data=True)
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



# -----------------------------
# Billable mix/aggregate comparison helper
# -----------------------------

# -----------------------------
# Billable comparison helpers
# -----------------------------









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
        # (was: maybe_canonicalize_followup; now handled by extracted module)
        from ai.frames import maybe_canonicalize_followup
        canonical_q, was_rewritten = maybe_canonicalize_followup(question, ts)
        if was_rewritten:
            ts["last_canonical_question"] = canonical_q
            ts["last_followup_rewrite"] = True
            q = _qnorm(canonical_q)
        else:
            q = _qnorm(question)

    # --- Intent detection (wiring only, no routing change) ---
    intent, intent_data = detect_intent(question=q, context=context)
    ts["last_detected_intent"] = intent

    # --- ROUTING TABLE DISPATCH ---
    for handler in ROUTES:
        turn = handler(
            question=question,
            q=q,
            context=context,
            thread_state=ts,
            db=db,
            ClaimModel=ClaimModel,
            InvoiceModel=InvoiceModel,
            BillableItemModel=BillableItemModel,
            ReportModel=ReportModel,
        )
        if turn is not None:
            return turn

    # --- (legacy inline routing remains for now; will be removed as migrated) ---

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
        resp["confidence"] = compute_confidence(text=resp.get("answer"), had_data=False, was_fallback=True)
        resp["answer_mode"] = "fallback"

    return resp


# Backwards-compatible aliases (ai_service.py may probe these names)
chat_respond = respond
handle_chat = respond
handle_message = respond
ask = respond
run = respond