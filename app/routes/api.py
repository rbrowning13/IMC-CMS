from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from flask import jsonify, request, session

from . import bp  # use the already-registered main blueprint
from app.services import ai_service


# -----------------------------------------------------------------------------
# Clarity universal API endpoint
#
# Key goals:
# - Accept payloads from multiple UI implementations (query/question/page_data)
# - Support one-turn clarifying follow-ups (open/closed/both) reliably
# - Do NOT rely solely on server session for follow-up state:
#     * Prefer client-provided `pending_intent` if present
#     * Fall back to session if client didn't send it
# - Normalize the response shape so the UI can always render.
# -----------------------------------------------------------------------------


def _get_str(d: Dict[str, Any], *keys: str, default: str = "") -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return default


def _get_dict(d: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, dict):
            return v
    return {}


def _normalize_context(data: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort normalize UI-provided context into a single dict."""
    context: Dict[str, Any] = _get_dict(data, "page_data", "context", "pageContext", "page_context")

    # Some clients may send these at the top level.
    # NOTE: Many UIs historically used `scope` for the *active record id* (e.g. claim_id),
    # so we must not blindly interpret it as a textual context scope.
    for key in (
        "claim_id",
        "invoice_id",
        "report_id",
        "carrier_id",
        "employer_id",
        "provider_id",
        "active_tab",
        "path",
        "url",
        "context_scope",
    ):
        if key in data and key not in context:
            context[key] = data.get(key)

    # Accept top-level `context` (some UIs send: { context: "system" | "claim_detail" | ... })
    if "context" in data and "context_scope" not in context and isinstance(data.get("context"), str):
        context["context_scope"] = data.get("context")

    # Special handling for `scope`:
    # - Many UIs use `scope` to mean the *active record id* (often claim_id).
    # - Some UIs send the id as a string (e.g. "9"). Treat digit-strings as ids.
    # - If it's a non-numeric string, treat it as a scope label.
    if "scope" in data and "scope" not in context:
        scope_val = data.get("scope")
        if isinstance(scope_val, int):
            if not context.get("claim_id"):
                context["claim_id"] = scope_val
        elif isinstance(scope_val, str):
            sv = scope_val.strip()
            if sv.isdigit():
                if not context.get("claim_id"):
                    context["claim_id"] = int(sv)
            elif sv:
                context["scope"] = sv
        else:
            # Ignore other types
            pass

    # Some UIs send `scope` inside the page_data/context dict.
    # If it looks like a numeric id (e.g. "9"), treat it as claim_id.
    # Do this BEFORE we consider promoting `scope` -> `context_scope`.
    scope_in_ctx = context.get("scope")
    if isinstance(scope_in_ctx, int):
        if not context.get("claim_id"):
            context["claim_id"] = scope_in_ctx
    elif isinstance(scope_in_ctx, str):
        sv = scope_in_ctx.strip()
        if sv.isdigit():
            if not context.get("claim_id"):
                context["claim_id"] = int(sv)
            # Leave context["scope"] intact for debugging/UI display, but do not
            # treat it as a textual scope label.

    # Canonicalize a couple common variants.
    # Only promote `scope` -> `context_scope` when `scope` is a *non-numeric* string label.
    if (
        "scope" in context
        and "context_scope" not in context
        and isinstance(context.get("scope"), str)
        and context.get("scope").strip()
        and not context.get("scope").strip().isdigit()
    ):
        context["context_scope"] = context.get("scope")
    if "context" in context and "context_scope" not in context:
        # Some UIs send: { context: "system" }
        if isinstance(context.get("context"), str):
            context["context_scope"] = context.get("context")

    # Safety: some legacy payloads accidentally set `context_scope` to a numeric id.
    # If that happens, treat it as claim_id.
    if isinstance(context.get("context_scope"), int):
        if not context.get("claim_id"):
            context["claim_id"] = context.get("context_scope")
        context.pop("context_scope", None)

    # Safety: some payloads set `context_scope` to a numeric string id.
    # If that happens, treat it as claim_id.
    if isinstance(context.get("context_scope"), str) and context.get("context_scope").strip().isdigit():
        if not context.get("claim_id"):
            context["claim_id"] = int(context.get("context_scope").strip())
        context.pop("context_scope", None)

    # Safe default: if no explicit scope provided, infer one from known IDs.
    if "context_scope" not in context:
        if context.get("claim_id") or context.get("report_id"):
            context["context_scope"] = "claim"
        elif context.get("invoice_id"):
            context["context_scope"] = "invoice"
        else:
            context["context_scope"] = "system"

    return context


def _pending_intent_from_request(data: Dict[str, Any]) -> Optional[str]:
    # Client-side pending intent is preferred (more reliable than cookies)
    v = data.get("pending_intent") or _get_dict(data, "page_data").get("pending_intent")
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def _get_thread_state_from_request(data: Dict[str, Any]) -> Dict[str, Any]:
    ts = data.get("thread_state")
    if isinstance(ts, dict):
        return ts
    # Some clients might nest it.
    ts2 = _get_dict(data, "page_data").get("thread_state")
    if isinstance(ts2, dict):
        return ts2
    return {}


def _expand_followup_if_needed(query: str, pending: Optional[str]) -> str:
    """Expand a one-word follow-up into a full question for deterministic handlers."""
    if not pending:
        return query

    qn = query.strip().lower()
    if qn not in {"open", "closed", "both"}:
        return query

    # Claim counts
    if pending in {"claim_count", "claim_count_open", "claim_count_closed", "claim_count_both"}:
        if qn == "open":
            return "how many open claims do i have?"
        if qn == "closed":
            return "how many closed claims do i have?"
        return "how many open and closed claims do i have?"

    # Claim lists
    if pending in {"claim_list", "claim_list_open", "claim_list_closed", "claim_list_both"}:
        if qn == "open":
            return "list open claims"
        if qn == "closed":
            return "list closed claims"
        return "list all claims"

    return query


def _rewrite_query_with_context(query: str, context: Dict[str, Any]) -> str:
    """Deterministic, low-risk query rewrites using page context.

    This exists to prevent ambiguous natural language from triggering the wrong
    deterministic intent (e.g. "how many billable items are on this claim?" being
    interpreted as a claim-count question because it contains the word "claim").

    Only applies when we have strong page context (like claim_id).
    """

    q = (query or "").strip()
    if not q:
        return query

    ql = q.lower()

    claim_id = context.get("claim_id")
    invoice_id = context.get("invoice_id")

    # If we are on a claim page (claim_id present) and the user asks "how many ... billable ...",
    # force an explicit per-claim billable-count question.
    if claim_id and ("billable" in ql or "billables" in ql):
        # common patterns: "how many billable items are on this claim?", "how many billables on this claim"
        if re.search(r"\bhow\s+many\b", ql) and re.search(r"\b(on|for)\s+(this\s+)?claim\b", ql):
            return f"How many billable items are on claim {claim_id}?"

        # also catch: "how many billable items do i have on this claim"
        if re.search(r"\bhow\s+many\b", ql) and "claim" in ql:
            return f"How many billable items are on claim {claim_id}?"

    # If we are on an invoice page and user asks "how many billable ...",
    # force an explicit per-invoice billable-count question.
    if invoice_id and ("billable" in ql or "billables" in ql):
        if re.search(r"\bhow\s+many\b", ql) and ("invoice" in ql or "this" in ql):
            return f"How many billable items are on invoice {invoice_id}?"

    return query
@bp.route("/api/clarity/query", methods=["POST"])
def clarity_query():
    """Universal Clarity entrypoint."""
    data: Dict[str, Any] = request.get_json(force=True) or {}

    # Accept either "query" or "question" (UI variations)
    query: str = _get_str(data, "query", "question")
    if not query:
        return jsonify({"ok": False, "error": "No query provided"}), 400

    # Normalize context
    context: Dict[str, Any] = _normalize_context(data)

    # Deterministically rewrite ambiguous queries using known page context (claim_id, invoice_id, etc.)
    query = _rewrite_query_with_context(query, context)

    # Compatibility: keep `scope` aligned with `context_scope`
    if "context_scope" in context and "scope" not in context:
        context["scope"] = context.get("context_scope")

    # Client-owned chat state (preferred over server session)
    thread_state: Dict[str, Any] = _get_thread_state_from_request(data)
    if thread_state:
        context["thread_state"] = thread_state

    # Follow-up state: prefer client-provided pending_intent; else session
    pending_intent: Optional[str] = (
        _pending_intent_from_request(data)
        or (thread_state.get("pending_intent") if isinstance(thread_state, dict) else None)
        or session.get("clarity_pending_intent")
    )

    # If user replied with only open/closed/both and we have pending intent, expand it.
    expanded_query = _expand_followup_if_needed(query, pending_intent)
    if expanded_query != query:
        query = expanded_query
        # Consume pending intent so it doesn't pollute future turns
        session.pop("clarity_pending_intent", None)
        session.modified = True

    # Ask Clarity via the service layer
    result: Dict[str, Any] = ai_service.ask_clarity(question=query, context=context)

    # Round-trip thread state for the chat UI (client-owned).
    # Backend may return `thread_state_update` (preferred) or `thread_state`.
    tsu = result.get("thread_state_update")
    if isinstance(tsu, dict):
        # Echo it back explicitly; UI merges it into its stored state.
        result["thread_state_update"] = tsu
        # Convenience: if the update includes pending_intent, also expose it top-level.
        if isinstance(tsu.get("pending_intent"), str) and tsu.get("pending_intent").strip():
            result["pending_intent"] = tsu.get("pending_intent").strip()
    else:
        ts_full = result.get("thread_state")
        if isinstance(ts_full, dict):
            # If the backend returned full state, treat it as an update blob.
            result["thread_state_update"] = ts_full

    # Persist pending intent for one-turn follow-ups
    try:
        pi = result.get("pending_intent")
        if isinstance(pi, str) and pi.strip():
            session["clarity_pending_intent"] = pi.strip()
            session.modified = True
    except Exception:
        pass

    # If the service returned JSON-as-text, merge it into the dict.
    if isinstance(result, dict) and isinstance(result.get("text"), str):
        try:
            parsed = json.loads(result["text"])
            if isinstance(parsed, dict) and "answer" in parsed:
                result.update(parsed)
        except (json.JSONDecodeError, TypeError):
            pass

    # Normalize expected keys for the UI
    if "answer" not in result:
        result["answer"] = result.get("text", "")

    result.setdefault("citations", [])
    result.setdefault("is_guess", False)
    result.setdefault("confidence", None)
    result.setdefault("model_source", result.get("source", "unknown"))
    result.setdefault("local_only", True)

    # Echo back pending intent so the UI can store it client-side.
    # Prefer explicit result pending_intent (possibly set from thread_state_update), else session.
    if not (isinstance(result.get("pending_intent"), str) and result.get("pending_intent").strip()):
        pi_sess = session.get("clarity_pending_intent")
        if isinstance(pi_sess, str) and pi_sess.strip():
            result["pending_intent"] = pi_sess.strip()
        else:
            result["pending_intent"] = None

    return jsonify({"ok": True, **result})