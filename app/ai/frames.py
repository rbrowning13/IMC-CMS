

from typing import Any, Dict, Tuple, List

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
# Frame/domain registry for frame-relative follow-ups
# -----------------------------

FRAME_REGISTRY = {
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

    frame_stack = thread_state.get("frame_stack") or []
    if not isinstance(frame_stack, list):
        frame_stack = []

    frames_to_try = list(reversed(frame_stack)) if frame_stack else []
    last_frame = thread_state.get("last_frame")

    if not frames_to_try and last_frame:
        frames_to_try = [last_frame]

    parts = q.split()
    if len(parts) < 3:
        return question, False

    noun = parts[2].rstrip("?.!,")

    for frame in frames_to_try:
        frame_entry = FRAME_REGISTRY.get(frame)
        if not frame_entry or "domains" not in frame_entry:
            continue

        domains = frame_entry["domains"]

        for dom_key, dom_entry in domains.items():
            if noun == dom_key or noun.rstrip("s") == dom_key.rstrip("s"):
                return dom_entry["question"], True

        for dom_key, dom_entry in domains.items():
            for syn in dom_entry.get("synonyms", []):
                if noun == syn or noun.rstrip("s") == syn.rstrip("s"):
                    return dom_entry["question"], True

    return question, False


# -----------------------------
# General follow-up canonicalization
# -----------------------------

def maybe_canonicalize_followup(question: str, thread_state: Dict[str, Any]) -> Tuple[str, bool]:
    """
    If the user asks a short, referential follow-up ("what about closed", "and unpaid", etc)
    and we have a last_intent, rewrite it into a canonical, full question.
    Returns (canonical_question, was_rewritten).
    """
    q = (question or "").strip().lower()
    last_intent = thread_state.get("last_intent")
    if not last_intent:
        return question, False

    if last_intent == "claim_count":
        if any(q in s for s in [
            "what about closed", "and closed", "only closed", "just closed", "closed?",
            "what about open", "and open", "only open", "just open", "open?",
            "what about both", "and both", "all", "everything", "both?",
        ]):
            if "closed" in q:
                return "How many closed claims do I have?", True
            if "open" in q:
                return "How many open claims do I have?", True
            if "both" in q or "all" in q or "everything" in q:
                return "How many claims do I have?", True

    if last_intent == "billing_total":
        if any(x in q for x in ["all claims", "every claim", "across all claims"]):
            return "How much billing do I have?", True

    return question, False