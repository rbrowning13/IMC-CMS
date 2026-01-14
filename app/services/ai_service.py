
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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
import logging
import os
import re

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


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
    """Global kill-switch for AI features (in addition to Settings.ai_enabled)."""
    return _env_truthy("OPENAI_DISABLED", "0")


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

    prompt = build_prompt(
        field_name=field_name,
        user_prompt=user_prompt,
        context=context,
        tone=tone,
    )

    # One choke point for the actual model call.
    return call_llm(prompt)


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

    prompt = build_prompt(
        field_name=field_name,
        user_prompt=user_prompt,
        context=context,
        tone=tone,
    )

    return prompt


# -----------------------------------------------------------------------------
# Context assembly
# -----------------------------------------------------------------------------

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

    # Collect billables inside or near the DOS range as a lightweight timeline.
    billables = collect_billable_timeline(
        BillableItem=BillableItem,
        claim_id=claim_id,
        dos_start=getattr(report, "dos_start", None),
        dos_end=getattr(report, "dos_end", None),
        limit=max_billables,
    )

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

def build_prompt(
    *,
    field_name: str,
    user_prompt: str,
    context: Dict[str, Any],
    tone: ToneProfile,
) -> str:
    """Build the model prompt.

    We keep prompts plain text so they are portable across providers.
    """

    header = context.get("header", {})
    current_report = context.get("current_report", {})
    current_fields = context.get("current_report_fields", {})
    prior_reports = context.get("prior_reports", [])
    billables = context.get("billables", [])
    privacy = context.get("privacy", {})
    guidance = get_field_guidance(field_name)

    style_lines = [
        "Write in a professional medical case management tone.",
        "Match the writing style observed in prior reports.",
        f"Average sentence length target: ~{int(round(tone.avg_sentence_len))} words.",
    ]
    if tone.uses_bullets_often:
        style_lines.append("Bullets are acceptable when helpful.")
    if tone.common_phrases:
        style_lines.append("Try to sound natural by using similar phrasing seen before.")
        style_lines.append("Examples of common phrasing (do not copy names/identifiers):")
        for p in tone.common_phrases[:6]:
            style_lines.append(f"- {p}")

    guardrails = [
        "Do NOT include claimant name, date of birth, claim number, address, phone, fax, or email.",
        "Do NOT invent facts.",
        "Use ONLY the provided context (header, current report fields, billables, prior reports).",
        "If information is missing, do NOT write generic filler like 'pending evaluation' or repeat 'not provided' unless truly necessary.",
        "When details are unavailable, write a single concise sentence stating what was not documented and what is being awaited (if the timeline implies it).",
        f"Provider names allowed: {bool(privacy.get('allow_provider_names'))}",
    ]

    parts: List[str] = []
    parts.append("You are drafting one field for a medical case management report.")
    parts.append("")
    parts.append("FIELD TO DRAFT:")
    parts.append(field_name)
    parts.append("")

    parts.append("FIELD GUIDANCE:")
    parts.append(f"Label: {guidance.get('label')}")
    parts.append(f"Purpose: {guidance.get('purpose')}")

    include = guidance.get("include") or []
    avoid = guidance.get("avoid") or []
    if include:
        parts.append("Include (when supported by context):")
        for x in include:
            parts.append(f"- {x}")
    if avoid:
        parts.append("Avoid:")
        for x in avoid:
            parts.append(f"- {x}")
    parts.append("")

    if user_prompt.strip():
        parts.append("USER INSTRUCTIONS:")
        parts.append(user_prompt.strip())
        parts.append("")

    parts.append("STYLE GUIDANCE:")
    parts.extend(style_lines)
    parts.append("")

    parts.append("GUARDRAILS:")
    parts.extend(guardrails)
    parts.append("")

    parts.append("CONTEXT HEADER (redacted):")
    parts.append(_pretty_kv(header))
    parts.append("")

    parts.append("CURRENT REPORT METADATA:")
    parts.append(_pretty_kv(current_report))
    parts.append("")

    parts.append("CURRENT REPORT FIELDS (redacted) — use these as the current draft and supporting context:")
    parts.append(_pretty_kv(current_fields))
    parts.append("")

    if billables:
        parts.append("BILLABLE TIMELINE (chronological, redacted):")
        parts.append(_pretty_list_of_dicts(billables, keys=("service_date", "activity_code", "description", "notes")))
        parts.append("")

    if prior_reports:
        parts.append("PRIOR REPORTS (most recent first, redacted, key fields):")
        prior_keys = ("report_type", "dos_start", "dos_end", "barriers_to_recovery") + tuple(AI_CONTEXT_FIELDS)
        parts.append(_pretty_list_of_dicts(prior_reports, keys=prior_keys))
        parts.append("")

    parts.append("OUTPUT:")
    parts.append("Return ONLY the drafted text for the requested field.")
    parts.append("Keep it concise and specific. If you must state something is unknown, do it once and move on.")
    parts.append("No headings, no quotes.")

    return "\n".join(parts)


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


# -----------------------------------------------------------------------------
# LLM call (placeholder)
# -----------------------------------------------------------------------------

def call_llm(prompt: str) -> str:
    """Call the configured LLM provider (OpenAI).

    Uses the official OpenAI Python SDK + Responses API.

    Environment variables:
      - OPENAI_DISABLED (optional; if truthy, AI features are disabled)
      - OPENAI_API_KEY (required)
      - OPENAI_MODEL (optional; default: gpt-4o-mini)
      - OPENAI_TEMPERATURE (optional; default: 0.2)
      - OPENAI_MAX_OUTPUT_TOKENS (optional; default: 700)
      - OPENAI_TIMEOUT (optional seconds; default: 45)
      - OPENAI_PROJECT (optional; passed to SDK if supported)
      - OPENAI_ORG (optional; passed to SDK if supported)

    Tip: If you want “more capable,” set OPENAI_MODEL=gpt-4o (or another model you’ve enabled in your OpenAI project).

    Returns:
      Model output text (string).

    Raises:
      RuntimeError on configuration / SDK errors.
    """

    if _ai_globally_disabled():
        raise RuntimeError("AI is globally disabled (OPENAI_DISABLED).")

    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError(
            "Missing OPENAI_API_KEY. Set it in your environment (or .env) before using AI drafts."
        )

    # Default to a strong general model; override via env.
    model = (os.getenv("OPENAI_MODEL") or "gpt-4o").strip()

    # Keep this conservative; tune later.
    try:
        temperature = float(os.getenv("OPENAI_TEMPERATURE") or "0.2")
    except Exception:
        temperature = 0.2
    temperature = max(0.0, min(2.0, temperature))

    try:
        max_output_tokens = int(os.getenv("OPENAI_MAX_OUTPUT_TOKENS") or "700")
    except Exception:
        max_output_tokens = 700

    try:
        timeout_s = float(os.getenv("OPENAI_TIMEOUT") or "45")
    except Exception:
        timeout_s = 45.0

    # Safety: avoid sending absurdly huge prompts due to unexpected data.
    max_prompt_chars = 60_000
    if isinstance(prompt, str) and len(prompt) > max_prompt_chars:
        prompt = prompt[:max_prompt_chars] + "\n\n[TRUNCATED: prompt exceeded max length]"

    # Lazy import so the rest of the app can run without the dependency.
    try:
        from openai import OpenAI
    except Exception as e:
        raise RuntimeError(
            "OpenAI SDK not installed. Run: pip install openai\n" f"Import error: {e}"
        )

    # Some SDK versions accept organization/project; passing unknown kwargs can break.
    client_kwargs: Dict[str, Any] = {"api_key": api_key, "timeout": timeout_s}

    org = (os.getenv("OPENAI_ORG") or "").strip()
    project = (os.getenv("OPENAI_PROJECT") or "").strip()

    try:
        if org:
            client_kwargs["organization"] = org
        if project:
            client_kwargs["project"] = project
        client = OpenAI(**client_kwargs)
    except TypeError:
        # Older/newer SDK that doesn't support org/project kwargs.
        client_kwargs.pop("organization", None)
        client_kwargs.pop("project", None)
        client = OpenAI(**client_kwargs)

    logger = logging.getLogger(__name__)

    # Use the Responses API. Provide a small system message to keep output clean.
    messages = [
        {
            "role": "system",
            "content": "You are a helpful assistant. Return plain text only. Do not add headings unless asked.",
        },
        {"role": "user", "content": prompt},
    ]

    try:
        resp = client.responses.create(
            model=model,
            input=messages,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
    except Exception as e:
        # Don't leak prompt; it may contain sensitive (though redacted) clinical info.
        logger.exception("OpenAI call failed")
        raise RuntimeError(f"AI request failed: {e}")

    # Best-effort usage logging (don’t log prompt/content).
    try:
        usage = getattr(resp, "usage", None)
        if usage:
            logger.info(
                "AI usage model=%s input_tokens=%s output_tokens=%s total_tokens=%s",
                model,
                getattr(usage, "input_tokens", None),
                getattr(usage, "output_tokens", None),
                getattr(usage, "total_tokens", None),
            )
    except Exception:
        pass

    # The SDK provides `output_text` for convenience.
    text = getattr(resp, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()

    # Fallback: extract from structured outputs.
    try:
        out_items = getattr(resp, "output", None) or []
        chunks: List[str] = []
        for item in out_items:
            content = getattr(item, "content", None) or []
            for part in content:
                part_type = getattr(part, "type", None)
                if part_type == "output_text":
                    t = getattr(part, "text", None)
                    if t:
                        chunks.append(str(t))
        if chunks:
            return "\n".join(chunks).strip()
    except Exception:
        pass

    raise RuntimeError("AI request returned no text output.")


# -----------------------------------------------------------------------------
# Small internal helpers
# -----------------------------------------------------------------------------

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
