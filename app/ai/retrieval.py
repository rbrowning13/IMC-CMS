from __future__ import annotations

"""
retrieval.py

Claim‑scoped, permission‑aware context retrieval for AI.

This module NEVER calls an LLM.
It ONLY assembles safe, explicit, unit‑aware context chunks.

NOTE:
This module is intentionally permissive during early Florence development.
It may return MORE context than strictly necessary to allow higher-level
reasoning, cross-claim analysis, and future agent behaviors.
"""

from dataclasses import dataclass
from typing import List, Optional, Any
import re

from sqlalchemy import func
from app import db

# DEBUG: retrieval module loaded
print("[retrieval] module loaded")


from app.models import BillableItem, Claim, Contact, Carrier, Employer, Provider, Invoice

# Reports may vary across branches; import best-effort.
try:
    from app.models import Report
except Exception:  # pragma: no cover
    Report = None

# =========================
# FULL DATABASE SNAPSHOT RETRIEVAL
# =========================
def _retrieve_full_database_snapshot() -> dict:
    """
    FULL SYSTEM SNAPSHOT.
    Returns raw, structured data for *all* major tables.
    Intended for system-level reasoning and analysis.
    """
    return {
        "claims": [c.__dict__ for c in Claim.query.all()],
        "invoices": [i.__dict__ for i in Invoice.query.all()],
        "billables": [b.__dict__ for b in BillableItem.query.all()],
        "carriers": [c.__dict__ for c in Carrier.query.all()],
        "employers": [e.__dict__ for e in Employer.query.all()],
        "providers": [p.__dict__ for p in Provider.query.all()],
        "reports": (
            [r.__dict__ for r in Report.query.all()]
            if Report is not None else []
        ),
    }

# =========================
# Domain-shaped system snapshot (context shaping)
# =========================
def _select_system_snapshot(query: str | None) -> dict:
    """
    Return a domain-shaped system snapshot based on the query.
    This intentionally suppresses unrelated domains to avoid LLM dominance.
    """
    if not query:
        return {}

    q = query.lower()

    billing_terms = {"billing", "invoice", "invoices", "outstanding", "owed", "owe", "due", "ar", "receivable", "paid", "unpaid"}
    workload_terms = {"workload", "busy", "capacity", "work", "reports", "billables"}
    claim_terms = {"claim", "claims", "claimant", "injury", "doi", "dos"}

    tokens = set(re.findall(r"[a-zA-Z0-9]+", q))

    snapshot = {}

    if tokens & billing_terms:
        snapshot["invoices"] = [i.__dict__ for i in Invoice.query.all()]
        snapshot["billables"] = [b.__dict__ for b in BillableItem.query.all()]
        return snapshot

    if tokens & workload_terms:
        snapshot["reports"] = [r.__dict__ for r in Report.query.all()] if Report is not None else []
        snapshot["billables"] = [b.__dict__ for b in BillableItem.query.all()]
        snapshot["claims"] = [c.__dict__ for c in Claim.query.all()]
        return snapshot

    if tokens & claim_terms:
        snapshot["claims"] = [c.__dict__ for c in Claim.query.all()]
        snapshot["reports"] = [r.__dict__ for r in Report.query.all()] if Report is not None else []
        return snapshot

    # Fallback: minimal system context
    snapshot["claims"] = [c.__dict__ for c in Claim.query.all()]
    return snapshot

# =========================
# Helpers for identity and contacts
# =========================

def _claim_identity_chunk(claim: Claim) -> RetrievedChunk:
    def _first_attr(obj, *names):
        for n in names:
            if hasattr(obj, n):
                v = getattr(obj, n)
                if v is not None and v != "":
                    return v
        return ""

    claimant_name = _first_attr(claim, "claimant_name", "claimant", "name")
    dob = _first_attr(claim, "dob", "claimant_dob", "claimant_date_of_birth", "date_of_birth")
    claim_number = _first_attr(claim, "claim_number", "claim_num", "claimno")
    doi = _first_attr(claim, "date_of_incident", "doi", "injury_date")
    claim_state = _first_attr(claim, "claim_state", "state")

    text_lines = [
        f"ClaimantName: {claimant_name}",
        f"DOB: {dob}",
        f"ClaimNumber: {claim_number}",
        f"DOI: {doi}",
        f"ClaimState: {claim_state}",
    ]
    return RetrievedChunk(
        source_id="CLAIM.IDENTITY",
        label="Claimant Identity",
        text="\n".join(text_lines),
        score=1000,
        intent_hint="claim_identity",
        authority="authoritative",
    )

def _contact_chunk(kind: str, obj: Contact) -> RetrievedChunk:
    lines = [
        f"Name: {obj.name or ''}",
        f"Phone: {obj.phone or ''}",
        f"Email: {obj.email or ''}",
        f"Role: {kind}",
    ]
    if obj.fax:
        lines.append(f"Fax: {obj.fax}")

    return RetrievedChunk(
        source_id=f"CONTACT.{kind.upper()}",
        label=f"{kind.capitalize()} Contact",
        text="\n".join(lines),
        score=1000,
        intent_hint="contact",
        authority="authoritative",
    )


# =========================
# Helpers for core entities (Carrier / Employer / Provider)
# =========================

def _carrier_chunk(carrier: Carrier) -> RetrievedChunk:
    def _first_attr(obj, *names):
        for n in names:
            if hasattr(obj, n):
                v = getattr(obj, n)
                if v is not None and v != "":
                    return v
        return ""

    lines = [
        f"CarrierID: {_first_attr(carrier, 'id')}",
        f"CarrierName: {_first_attr(carrier, 'name', 'carrier_name')}",
        f"Phone: {_first_attr(carrier, 'phone')}",
        f"Email: {_first_attr(carrier, 'email')}",
        f"Fax: {_first_attr(carrier, 'fax')}",
        f"Address1: {_first_attr(carrier, 'address1', 'address_line1')}",
        f"Address2: {_first_attr(carrier, 'address2', 'address_line2')}",
        f"City: {_first_attr(carrier, 'city')}",
        f"State: {_first_attr(carrier, 'state', 'carrier_state')}",
        f"Zip: {_first_attr(carrier, 'zip', 'postal_code')}",
        f"Notes: {_first_attr(carrier, 'notes')}",
    ]

    # Remove empty trailing fields for cleanliness
    lines = [ln for ln in lines if not ln.endswith(": ")]

    return RetrievedChunk(
        source_id=f"CARRIER.{carrier.id}",
        label="Carrier",
        text="\n".join(lines),
        score=950,
        intent_hint="carrier",
        authority="authoritative",
    )


def _employer_chunk(employer: Employer) -> RetrievedChunk:
    def _first_attr(obj, *names):
        for n in names:
            if hasattr(obj, n):
                v = getattr(obj, n)
                if v is not None and v != "":
                    return v
        return ""

    lines = [
        f"EmployerID: {_first_attr(employer, 'id')}",
        f"EmployerName: {_first_attr(employer, 'name', 'employer_name')}",
        f"Phone: {_first_attr(employer, 'phone')}",
        f"Email: {_first_attr(employer, 'email')}",
        f"Fax: {_first_attr(employer, 'fax')}",
        f"Address1: {_first_attr(employer, 'address1', 'address_line1')}",
        f"Address2: {_first_attr(employer, 'address2', 'address_line2')}",
        f"City: {_first_attr(employer, 'city')}",
        f"State: {_first_attr(employer, 'state', 'employer_state')}",
        f"Zip: {_first_attr(employer, 'zip', 'postal_code')}",
        f"Notes: {_first_attr(employer, 'notes')}",
    ]

    lines = [ln for ln in lines if not ln.endswith(": ")]

    return RetrievedChunk(
        source_id=f"EMPLOYER.{employer.id}",
        label="Employer",
        text="\n".join(lines),
        score=950,
        intent_hint="employer",
        authority="authoritative",
    )


def _provider_chunk(provider: Provider) -> RetrievedChunk:
    def _first_attr(obj, *names):
        for n in names:
            if hasattr(obj, n):
                v = getattr(obj, n)
                if v is not None and v != "":
                    return v
        return ""

    lines = [
        f"ProviderID: {_first_attr(provider, 'id')}",
        f"ProviderName: {_first_attr(provider, 'name', 'provider_name')}",
        f"Specialty: {_first_attr(provider, 'specialty')}",
        f"Phone: {_first_attr(provider, 'phone')}",
        f"Email: {_first_attr(provider, 'email')}",
        f"Fax: {_first_attr(provider, 'fax')}",
        f"Address1: {_first_attr(provider, 'address1', 'address_line1')}",
        f"Address2: {_first_attr(provider, 'address2', 'address_line2')}",
        f"City: {_first_attr(provider, 'city')}",
        f"State: {_first_attr(provider, 'state', 'provider_state')}",
        f"Zip: {_first_attr(provider, 'zip', 'postal_code')}",
        f"Notes: {_first_attr(provider, 'notes', 'contact_notes')}",
    ]

    lines = [ln for ln in lines if not ln.endswith(": ")]

    return RetrievedChunk(
        source_id=f"PROVIDER.{provider.id}",
        label="Provider",
        text="\n".join(lines),
        score=950,
        intent_hint="provider",
        authority="authoritative",
    )


# =========================
# Report chunk helper (best-effort, Florence context)
# =========================
def _report_chunk(report: Any, idx: int = 0) -> RetrievedChunk:
    """Best-effort report chunk.

    We avoid hard schema assumptions by probing common field names.
    Includes long-text fields when present so Florence can reason over history.
    """

    def _first_attr(obj, *names):
        for n in names:
            if hasattr(obj, n):
                v = getattr(obj, n)
                if v is not None and v != "":
                    return v
        return ""

    report_id = _first_attr(report, "id")
    report_type = _first_attr(report, "report_type", "type")
    created_at = _first_attr(report, "created_at")
    updated_at = _first_attr(report, "updated_at", "last_updated")

    dos_start = _first_attr(report, "dos_start", "date_of_service_start")
    dos_end = _first_attr(report, "dos_end", "date_of_service_end")
    next_due = _first_attr(report, "next_report_due", "next_due")

    treating_provider_name = ""
    tp = getattr(report, "treating_provider", None)
    if tp is not None:
        treating_provider_name = _first_attr(tp, "name", "provider_name")

    # Long-text fields we want Florence to see (best-effort)
    long_text_fields = [
        ("status_treatment_plan", "Status/Treatment Plan"),
        ("status", "Status"),
        ("treatment_plan", "Treatment Plan"),
        ("work_status", "Work Status"),
        ("case_management_plan", "Case Management Plan"),
        ("case_management_impact", "Case Management Impact"),
        ("closure_details", "Closure Details"),
        ("reason_for_closure", "Reason for Closure"),
        ("diagnosis", "Diagnosis"),
        ("mechanism_of_injury", "Mechanism of Injury"),
        ("co_existing_conditions", "Concurrent Conditions"),
        ("surgical_history", "Surgical History"),
        ("medications", "Medications"),
        ("diagnostics", "Diagnostics"),
        ("barriers_json", "Barriers"),
        ("next_appointment", "Next Appointment"),
        ("next_appointment_notes", "Next Appointment Notes"),
        ("employment_status", "Employment Status"),
    ]

    lines = [
        f"ReportID: {report_id}",
        f"ReportType: {report_type}",
        f"CreatedAt: {created_at}",
        f"UpdatedAt: {updated_at}",
        f"DOSStart: {dos_start}",
        f"DOSEnd: {dos_end}",
        f"NextReportDue: {next_due}",
        f"TreatingProvider: {treating_provider_name}",
    ]

    for field_name, label in long_text_fields:
        if hasattr(report, field_name):
            v = getattr(report, field_name)
            if v is not None and v != "":
                # Keep it readable; do not truncate here (LLM/prompt will handle limits)
                lines.append(f"{label}: {v}")

    # Remove empty trailing fields for cleanliness
    lines = [ln for ln in lines if not ln.endswith(": ")]

    rid = report_id if report_id != "" else f"IDX{idx}"

    return RetrievedChunk(
        source_id=f"REPORT.{rid}",
        label="Report",
        text="\n".join(lines),
        score=940,
        intent_hint="report",
        authority="authoritative",
    )


# NEW: Helper to extract the latest report as a single derived chunk
def _latest_report_derived_chunk(reports: list[Any]) -> Optional[RetrievedChunk]:
    """
    Create a single derived chunk summarizing the *latest* report.
    This gives Florence a clear anchor for questions like:
    'what did the latest report say about work status?'
    """
    if not reports:
        return None

    r = reports[0]  # reports are already sorted newest-first
    def _first_attr(obj, *names):
        for n in names:
            if hasattr(obj, n):
                v = getattr(obj, n)
                if v not in (None, ""):
                    return v
        return ""

    lines = [
        "SummaryType: LatestReport",
        f"ReportID: {_first_attr(r, 'id')}",
        f"ReportType: {_first_attr(r, 'report_type', 'type')}",
        f"DOSStart: {_first_attr(r, 'dos_start')}",
        f"DOSEnd: {_first_attr(r, 'dos_end')}",
    ]

    work_status = _first_attr(r, "work_status")
    if work_status:
        lines.append(f"WorkStatus: {work_status}")

    status_plan = _first_attr(r, "status_treatment_plan", "status", "treatment_plan")
    if status_plan:
        lines.append(f"StatusSummary: {status_plan}")

    case_plan = _first_attr(r, "case_management_plan")
    if case_plan:
        lines.append(f"CaseManagementPlan: {case_plan}")

    return RetrievedChunk(
        source_id="REPORT.LATEST.DERIVED",
        label="Latest Report Summary",
        text="\n".join(lines),
        score=5000,
        intent_hint="latest_report_summary",
        authority="derived",
    )


# NEW: Helper to extract the latest report work status as a single derived chunk
def _latest_work_status_derived_chunk(reports: list[Any]) -> Optional[RetrievedChunk]:
    """Create a focused derived chunk for *latest* report work status.

    This exists because users often ask exactly:
      - "what did the latest report say about work status?"

    Keeping this chunk small + high score makes it very likely to survive max_chunks slicing.
    """
    if not reports:
        return None

    r = reports[0]  # reports are sorted newest-first

    def _first_attr(obj, *names):
        for n in names:
            if hasattr(obj, n):
                v = getattr(obj, n)
                if v not in (None, ""):
                    return v
        return ""

    work_status = _first_attr(r, "work_status")

    lines = [
        "SummaryType: LatestReportWorkStatus",
        f"ReportID: {_first_attr(r, 'id')}",
        f"ReportType: {_first_attr(r, 'report_type', 'type')}",
        f"DOSStart: {_first_attr(r, 'dos_start')}",
        f"DOSEnd: {_first_attr(r, 'dos_end')}",
        f"WorkStatus: {work_status}",
    ]

    if not work_status:
        lines.append("NOTE: Latest report has no work_status field/value.")

    return RetrievedChunk(
        source_id="REPORT.LATEST.WORK_STATUS.DERIVED",
        label="Latest Report – Work Status",
        text="\n".join(lines),
        score=5200,
        intent_hint="latest_report_work_status",
        authority="derived",
    )


# NEW: Claim-level derived chunk (high-signal, compact)
def _claim_status_derived_chunk(*, claim: Claim, reports: list[Any]) -> Optional[RetrievedChunk]:
    """Create a compact, high-signal claim status summary.

    This chunk is designed to survive max_chunks slicing and answer common questions like:
      - "summarize this claim"
      - "what's going on with this case?"

    NOTE: This is *derived* from authoritative claim + report fields.
    """
    if claim is None:
        return None

    def _first_attr(obj, *names):
        for n in names:
            if hasattr(obj, n):
                v = getattr(obj, n)
                if v not in (None, ""):
                    return v
        return ""

    carrier_name = ""
    carrier = getattr(claim, "carrier", None)
    if carrier is not None:
        carrier_name = _first_attr(carrier, "name", "carrier_name")

    employer_name = ""
    employer = getattr(claim, "employer", None)
    if employer is not None:
        employer_name = _first_attr(employer, "name", "employer_name")

    # Reports are sorted newest-first by retrieval
    latest = reports[0] if reports else None

    latest_type = _first_attr(latest, "report_type", "type") if latest else ""
    latest_dos_start = _first_attr(latest, "dos_start", "date_of_service_start") if latest else ""
    latest_dos_end = _first_attr(latest, "dos_end", "date_of_service_end") if latest else ""
    latest_next_due = _first_attr(latest, "next_report_due", "next_due") if latest else ""

    # Try to surface work status if present; keep it short
    latest_work_status = ""
    if latest is not None:
        latest_work_status = _first_attr(latest, "work_status")
        if isinstance(latest_work_status, str) and len(latest_work_status) > 360:
            latest_work_status = latest_work_status[:360].rstrip() + "…"

    # Treating providers (best-effort)
    tp_names: list[str] = []
    providers = getattr(claim, "providers", None)
    if providers:
        for p in providers:
            nm = _first_attr(p, "name", "provider_name")
            if nm:
                tp_names.append(str(nm))

    lines = [
        "SummaryType: ClaimStatus",
        f"ClaimID: {_first_attr(claim, 'id')}",
        f"Status: {_first_attr(claim, 'status')}",
        f"ClaimNumber: {_first_attr(claim, 'claim_number', 'claim_num', 'claimno')}",
        f"ClaimState: {_first_attr(claim, 'claim_state', 'state')}",
        f"DOI: {_first_attr(claim, 'date_of_incident', 'doi', 'injury_date')}",
        f"ReferralDate: {_first_attr(claim, 'referral_date', 'referral')}",
        f"InjuredBodyPart: {_first_attr(claim, 'injured_body_part')}",
        f"SurgeryDate: {_first_attr(claim, 'surgery_date')}",
        f"Carrier: {carrier_name}",
        f"Employer: {employer_name}",
        f"TreatingProviders: {', '.join(tp_names) if tp_names else ''}",
        f"ReportCount: {len(reports)}",
        f"LatestReportType: {latest_type}",
        f"LatestDOSStart: {latest_dos_start}",
        f"LatestDOSEnd: {latest_dos_end}",
        f"LatestNextReportDue: {latest_next_due}",
    ]

    if latest_work_status:
        lines.append(f"LatestWorkStatus: {latest_work_status}")

    # Clean empty placeholders
    lines = [ln for ln in lines if not ln.endswith(": ")]

    return RetrievedChunk(
        source_id="CLAIM.STATUS.DERIVED",
        label="Claim Status Summary",
        text="\n".join(lines),
        score=4800,
        intent_hint="claim_status_summary",
        authority="derived",
    )


# NEW: Report-history derived chunk (timeline-ish, compact)
def _care_trajectory_derived_chunk(*, claim: Claim, reports: list[Any], max_lines: int = 10) -> Optional[RetrievedChunk]:
    """Create a compact trajectory/timeline chunk from report history.

    Goal: give Florence a quick, readable 'what changed over time' anchor.
    """
    if claim is None:
        return None

    if not reports:
        return RetrievedChunk(
            source_id="CLAIM.TRAJECTORY.DERIVED",
            label="Care Trajectory",
            text=(
                "SummaryType: CareTrajectory\n"
                f"ClaimID: {getattr(claim, 'id', '')}\n"
                "NOTE: No reports found for this claim."
            ),
            score=4200,
            intent_hint="care_trajectory",
            authority="derived",
        )

    def _first_attr(obj, *names):
        for n in names:
            if hasattr(obj, n):
                v = getattr(obj, n)
                if v not in (None, ""):
                    return v
        return ""

    # reports are newest-first
    newest = reports[0]
    oldest = reports[-1]

    newest_end = _first_attr(newest, "dos_end", "date_of_service_end")
    oldest_start = _first_attr(oldest, "dos_start", "date_of_service_start")

    # Count by type (best-effort)
    type_counts: dict[str, int] = {}
    for r in reports:
        rt = str(_first_attr(r, "report_type", "type") or "").strip() or "(unknown)"
        type_counts[rt] = type_counts.get(rt, 0) + 1

    lines: list[str] = [
        "SummaryType: CareTrajectory",
        f"ClaimID: {_first_attr(claim, 'id')}",
        f"ReportSpanStart: {oldest_start}",
        f"ReportSpanEnd: {newest_end}",
        "ReportTypeCounts:",
    ]

    for k in sorted(type_counts.keys()):
        lines.append(f"  {k}: {type_counts[k]}")

    lines.append("RecentReports:")

    for i, r in enumerate(reports[:max_lines]):
        rt = _first_attr(r, "report_type", "type")
        ds = _first_attr(r, "dos_start", "date_of_service_start")
        de = _first_attr(r, "dos_end", "date_of_service_end")
        ws = _first_attr(r, "work_status")
        if isinstance(ws, str) and len(ws) > 120:
            ws = ws[:120].rstrip() + "…"
        ws_part = f" | Work: {ws}" if ws else ""
        lines.append(f"  - {rt} | {ds} → {de}{ws_part}")

    return RetrievedChunk(
        source_id="CLAIM.TRAJECTORY.DERIVED",
        label="Care Trajectory",
        text="\n".join([ln for ln in lines if ln is not None]),
        score=4300,
        intent_hint="care_trajectory",
        authority="derived",
    )


def _system_carrier_count_chunk() -> RetrievedChunk:
    count = Carrier.query.count()
    return RetrievedChunk(
        source_id="SYSTEM.CARRIERS.COUNT",
        label="System Carrier Count",
        text=f"TotalCarriers: {count}",
        quantity=float(count),
        unit="count",
        score=1000,
        intent_hint="system_carrier_count",
        authority="authoritative",
    )


def _system_employer_count_chunk() -> RetrievedChunk:
    count = Employer.query.count()
    return RetrievedChunk(
        source_id="SYSTEM.EMPLOYERS.COUNT",
        label="System Employer Count",
        text=f"TotalEmployers: {count}",
        quantity=float(count),
        unit="count",
        score=1000,
        intent_hint="system_employer_count",
        authority="authoritative",
    )


def _system_provider_count_chunk() -> RetrievedChunk:
    count = Provider.query.count()
    return RetrievedChunk(
        source_id="SYSTEM.PROVIDERS.COUNT",
        label="System Provider Count",
        text=f"TotalProviders: {count}",
        quantity=float(count),
        unit="count",
        score=1000,
        intent_hint="system_provider_count",
        authority="authoritative",
    )


def _system_carriers_list_chunk(limit: int = 200) -> RetrievedChunk:
    q = Carrier.query
    if hasattr(Carrier, "id"):
        q = q.order_by(Carrier.id.asc())
    carriers = q.limit(limit).all()

    def _safe(obj, *names):
        for n in names:
            if hasattr(obj, n):
                v = getattr(obj, n)
                if v is not None and v != "":
                    return v
        return ""

    lines = ["Carriers:"]
    for c in carriers:
        lines.append(
            f"CarrierID: {c.id} | Name: {_safe(c, 'name', 'carrier_name')} | State: {_safe(c, 'state')} | Phone: {_safe(c, 'phone')} | Email: {_safe(c, 'email')}"
        )

    if len(carriers) >= limit:
        lines.append(f"NOTE: List truncated to first {limit} carriers.")

    return RetrievedChunk(
        source_id="SYSTEM.CARRIERS.LIST",
        label="System Carrier List",
        text="\n".join(lines),
        quantity=float(len(carriers)),
        unit="count",
        score=930,
        intent_hint="system_carrier_list",
        authority="authoritative",
    )


def _system_employers_list_chunk(limit: int = 200) -> RetrievedChunk:
    q = Employer.query
    if hasattr(Employer, "id"):
        q = q.order_by(Employer.id.asc())
    employers = q.limit(limit).all()

    def _safe(obj, *names):
        for n in names:
            if hasattr(obj, n):
                v = getattr(obj, n)
                if v is not None and v != "":
                    return v
        return ""

    lines = ["Employers:"]
    for e in employers:
        carrier_name = ""
        carrier = getattr(e, "carrier", None)
        if carrier is not None:
            carrier_name = _safe(carrier, "name", "carrier_name")

        lines.append(
            f"EmployerID: {e.id} | Name: {_safe(e, 'name', 'employer_name')} | Carrier: {carrier_name} | State: {_safe(e, 'state')} | Phone: {_safe(e, 'phone')} | Email: {_safe(e, 'email')}"
        )

    if len(employers) >= limit:
        lines.append(f"NOTE: List truncated to first {limit} employers.")

    return RetrievedChunk(
        source_id="SYSTEM.EMPLOYERS.LIST",
        label="System Employer List",
        text="\n".join(lines),
        quantity=float(len(employers)),
        unit="count",
        score=930,
        intent_hint="system_employer_list",
        authority="authoritative",
    )


def _system_providers_list_chunk(limit: int = 200) -> RetrievedChunk:
    q = Provider.query
    if hasattr(Provider, "id"):
        q = q.order_by(Provider.id.asc())
    providers = q.limit(limit).all()

    def _safe(obj, *names):
        for n in names:
            if hasattr(obj, n):
                v = getattr(obj, n)
                if v is not None and v != "":
                    return v
        return ""

    lines = ["Providers:"]
    for p in providers:
        lines.append(
            f"ProviderID: {p.id} | Name: {_safe(p, 'name', 'provider_name')} | Specialty: {_safe(p, 'specialty')} | State: {_safe(p, 'state')} | Phone: {_safe(p, 'phone')} | Email: {_safe(p, 'email')}"
        )

    if len(providers) >= limit:
        lines.append(f"NOTE: List truncated to first {limit} providers.")

    return RetrievedChunk(
        source_id="SYSTEM.PROVIDERS.LIST",
        label="System Provider List",
        text="\n".join(lines),
        quantity=float(len(providers)),
        unit="count",
        score=930,
        intent_hint="system_provider_list",
        authority="authoritative",
    )


def _maybe_add_contacts(chunks: List[RetrievedChunk], obj: Any, kind_label: str, attr_names: List[str]):
    """Best-effort add contacts from common attribute names."""
    for a in attr_names:
        c = getattr(obj, a, None)
        if c:
            try:
                chunks.append(_contact_chunk(kind_label, c))
            except Exception:
                pass
            return


# =========================
# Claim header chunk
# =========================

def _claim_header_chunk(claim_id: int) -> RetrievedChunk:
    return RetrievedChunk(
        source_id=f"CLAIM.{claim_id}",
        label="Claim Context",
        text=(
            f"ClaimID: {claim_id}\n"
            "Scope: Billable items, reports, invoices\n"
            "Instruction: Use explicit numeric fields when answering totals"
        ),
        score=1000,
        intent_hint="claim_header",
        authority="authoritative",
    )


@dataclass
class Intent:
    wants_numeric: bool
    wants_list: bool
    wants_summary: bool


# =========================
# Retrieval scope (Florence expansion)
# =========================

@dataclass
class RetrievalScope:
    claim: bool = True
    invoices: bool = True
    reports: bool = True
    cross_claim: bool = False


# =========================
# Data container
# =========================

@dataclass
class RetrievedChunk:
    source_id: str
    label: str
    text: str
    score: Optional[float] = None

    # Explicit numeric exposure
    quantity: Optional[float] = None
    unit: Optional[str] = None           # hours | miles | dollars
    activity_code: Optional[str] = None
    amount: Optional[float] = None
    invoice_id: Optional[int] = None
    is_invoiced: Optional[bool] = None

    intent_hint: Optional[str] = None

    # Authority: "authoritative", "derived", or "contextual"
    authority: Optional[str] = None


# =========================
# Activity semantics
# =========================

EXPENSE_CODES = {"EXP", "EX", "EXPENSE", "EXPENSES"}
MILEAGE_CODES = {"MIL", "MILE", "MILEAGE"}


# =========================
# Helpers
# =========================

def _query_tokens(q: str) -> set[str]:
    return set(re.findall(r"[a-zA-Z0-9]+", (q or "").lower()))


def classify_intent(query: str) -> Intent:
    tokens = _query_tokens(query)

    wants_numeric = bool(tokens & {
        "how", "many", "count", "total", "sum",
        "hours", "hour",
        "miles", "mile", "mil",
        "expenses", "expense", "exp",
        "dollars", "amount", "$",
        "billing", "bill", "billed",
        "invoice", "invoices",
        "outstanding", "owed", "owe", "due",
        "receivable", "ar",
    })

    wants_list = bool(tokens & {"list", "show", "detail", "details", "each", "items"})
    wants_summary = (
        wants_numeric
        or "summary" in tokens
        or "totals" in tokens
        or "billing" in tokens
        or "outstanding" in tokens
        or "invoices" in tokens
    )

    return Intent(
        wants_numeric=wants_numeric,
        wants_list=wants_list,
        wants_summary=wants_summary,
    )


def is_identity_query(query: str) -> bool:
    tokens = _query_tokens(query)
    return bool(tokens & {
        "dob", "dateofbirth", "birth",
        "claimant", "name",
        "claimnumber",
        "doi", "incident",
        "phone", "email", "fax",
        "adjuster", "employer",
    })


# Heuristic: detect system/cross-claim questions
def _is_systemish_query(query: str) -> bool:
    """Heuristic: detect questions that should use system/cross-claim scope.

    Examples:
      - "how many claims do I have"
      - "list all carriers"
      - "total outstanding invoices"

    We bias toward system scope when the user is asking about counts/lists/totals
    of core system entities and is NOT clearly asking about the currently-open claim.
    """
    tokens = _query_tokens(query)

    # Strong signals for system-wide questions
    system_terms = {
        "claims", "claim", "providers", "provider", "employers", "employer",
        "carriers", "carrier", "invoices", "invoice", "billing", "ar",
        "outstanding", "overdue", "paid", "draft", "open", "closed",
        "count", "total", "sum", "list", "show", "all",
    }

    # Strong signals for claim-specific questions
    claim_terms = {
        "this", "current", "claimant", "doi", "dos", "report", "reports",
        "billable", "billables", "work", "status", "injury", "referral",
        "surgery", "appointment",
    }

    # If it looks like an identity query, treat as claim-context (not system)
    if is_identity_query(query):
        return False

    has_system = bool(tokens & system_terms)
    has_claim = bool(tokens & claim_terms)

    # If the user explicitly references "this"/"current" or report/billables/etc, prefer claim.
    if has_claim:
        return False

    return has_system


def _format_billable_chunk(b: BillableItem) -> RetrievedChunk:
    """
    Convert a BillableItem into a deterministic, unit‑aware chunk.
    """
    code = (b.activity_code or "").upper().strip()
    qty = float(b.quantity or 0.0)

    is_invoiced = b.invoice_id is not None

    if code in EXPENSE_CODES:
        unit = "dollars"
        label = "Expense"
        amount = qty
        display = f"${qty:.2f}"
    elif code in MILEAGE_CODES:
        unit = "miles"
        label = "Mileage"
        amount = None
        display = f"{qty:.2f} miles"
    else:
        unit = "hours"
        label = "Hours"
        amount = None
        display = f"{qty:.2f} hours"

    lines = []

    if b.date_of_service:
        lines.append(f"Date: {b.date_of_service}")

    if code:
        lines.append(f"ActivityCode: {code}")

    lines.extend(
        [
            f"Display: {display}",
            f"Quantity: {qty}",
            f"Unit: {unit}",
            f"Hours: {qty if unit == 'hours' else 0}",
            f"Miles: {qty if unit == 'miles' else 0}",
            f"AmountDollars: {qty if unit == 'dollars' else 0}",
            f"Invoiced: {'Yes' if is_invoiced else 'No'}",
        ]
    )

    if b.invoice_id:
        lines.append(f"InvoiceID: {b.invoice_id}")

    if b.description:
        lines.append(f"Description: {b.description}")

    if b.notes:
        lines.append(f"Notes: {b.notes}")

    return RetrievedChunk(
        source_id=f"B{b.id}",
        label=f"Billable {b.id} ({label})",
        text="\n".join(lines),
        quantity=qty,
        unit=unit,
        activity_code=code,
        amount=amount,
        invoice_id=b.invoice_id,
        is_invoiced=is_invoiced,
        authority="authoritative",
    )


# ==============================================================================
# Public API
# ==============================================================================

def _system_claim_count_chunk() -> RetrievedChunk:
    count = Claim.query.count()
    return RetrievedChunk(
        source_id="SYSTEM.CLAIMS.COUNT",
        label="System Claim Count",
        text=f"TotalClaims: {count}",
        quantity=float(count),
        unit="count",
        score=1000,
        intent_hint="system_claim_count",
        authority="authoritative",
    )

# Explicit open claims count chunk

# Explicit open claims count chunk
def _system_open_claim_count_chunk() -> RetrievedChunk:
    status_col = getattr(Claim, "status", None)
    if status_col is None:
        # Fallback if schema changes
        count = Claim.query.count()
    else:
        count = Claim.query.filter(status_col.ilike("%open%")) .count()

    return RetrievedChunk(
        source_id="SYSTEM.CLAIMS.OPEN_COUNT",
        label="Open Claim Count",
        text=f"OpenClaims: {count}",
        quantity=float(count),
        unit="count",
        score=1000,
        intent_hint="system_claim_open_count",
        authority="authoritative",
    )


def _system_closed_claim_count_chunk() -> RetrievedChunk:
    status_col = getattr(Claim, "status", None)
    if status_col is None:
        count = 0
    else:
        count = Claim.query.filter(status_col.ilike("%closed%")) .count()

    return RetrievedChunk(
        source_id="SYSTEM.CLAIMS.CLOSED_COUNT",
        label="Closed Claim Count",
        text=f"ClosedClaims: {count}",
        quantity=float(count),
        unit="count",
        score=1000,
        intent_hint="system_claim_closed_count",
        authority="authoritative",
    )

def _system_invoice_count_chunk() -> RetrievedChunk:
    count = Invoice.query.count()
    return RetrievedChunk(
        source_id="SYSTEM.INVOICES.COUNT",
        label="System Invoice Count",
        text=f"TotalInvoices: {count}",
        quantity=float(count),
        unit="count",
        score=1000,
        intent_hint="system_invoice_count",
        authority="authoritative",
    )

# System-wide: invoices by status + outstanding total (best-effort)

_INVOICE_STATUS_BUCKETS = {
    "draft": {"draft"},
    "open": {"open"},
    "overdue": {"overdue"},
    "paid": {"paid"},
}


def _norm_status(v: Any) -> str:
    s = (str(v) if v is not None else "").strip().lower()
    return s


def _invoice_total_expr():
    # Prefer total_amount, fallback to total
    if hasattr(Invoice, "total_amount"):
        return Invoice.total_amount
    if hasattr(Invoice, "total"):
        return Invoice.total
    return None


def _system_billing_summary_chunk() -> RetrievedChunk:
    status_col = getattr(Invoice, "status", None)
    total_col = _invoice_total_expr()

    # If we can't compute totals, still provide counts.
    total_invoices = Invoice.query.count()

    counts = {k: 0 for k in _INVOICE_STATUS_BUCKETS.keys()}
    totals = {k: 0.0 for k in _INVOICE_STATUS_BUCKETS.keys()}

    if status_col is not None:
        rows = db.session.query(status_col, func.count(Invoice.id)).group_by(status_col).all()
        for st, cnt in rows:
            ns = _norm_status(st)
            bucket = None
            for b, vals in _INVOICE_STATUS_BUCKETS.items():
                if ns in vals:
                    bucket = b
                    break
            if bucket is None:
                # Unknown statuses are treated as "open" for AR visibility.
                bucket = "open"
            counts[bucket] += int(cnt or 0)

    if status_col is not None and total_col is not None:
        rows = db.session.query(status_col, func.coalesce(func.sum(total_col), 0.0)).group_by(status_col).all()
        for st, tot in rows:
            ns = _norm_status(st)
            bucket = None
            for b, vals in _INVOICE_STATUS_BUCKETS.items():
                if ns in vals:
                    bucket = b
                    break
            if bucket is None:
                bucket = "open"
            try:
                totals[bucket] += float(tot or 0.0)
            except Exception:
                pass

    # Outstanding = everything except paid
    outstanding_total = float(totals.get("draft", 0.0) + totals.get("open", 0.0) + totals.get("overdue", 0.0))
    outstanding_count = int(counts.get("draft", 0) + counts.get("open", 0) + counts.get("overdue", 0))

    lines = [
        "SummaryType: SystemBillingSummary",
        f"TotalInvoices: {total_invoices}",
        f"DraftInvoices: {counts.get('draft', 0)} | DraftTotal: ${totals.get('draft', 0.0):.2f}",
        f"OpenInvoices: {counts.get('open', 0)} | OpenTotal: ${totals.get('open', 0.0):.2f}",
        f"OverdueInvoices: {counts.get('overdue', 0)} | OverdueTotal: ${totals.get('overdue', 0.0):.2f}",
        f"PaidInvoices: {counts.get('paid', 0)} | PaidTotal: ${totals.get('paid', 0.0):.2f}",
        f"OutstandingInvoiceCount: {outstanding_count}",
        f"OutstandingInvoiceTotal: ${outstanding_total:.2f}",
        "NOTE: Outstanding totals are invoice totals (not uninvoiced billables).",
    ]

    return RetrievedChunk(
        source_id="SYSTEM.BILLING.SUMMARY",
        label="System Billing Summary",
        text="\n".join(lines),
        score=1000,
        intent_hint="system_billing_summary",
        authority="derived",
    )


# New: System-wide outstanding A/R chunk based on invoice totals
def _system_outstanding_billing_chunk() -> RetrievedChunk:
    """System-wide outstanding A/R based on invoice totals (draft+open+overdue)."""
    status_col = getattr(Invoice, "status", None)
    total_col = _invoice_total_expr()

    counts = {k: 0 for k in _INVOICE_STATUS_BUCKETS.keys()}
    totals = {k: 0.0 for k in _INVOICE_STATUS_BUCKETS.keys()}

    if status_col is not None:
        rows = db.session.query(status_col, func.count(Invoice.id)).group_by(status_col).all()
        for st, cnt in rows:
            ns = _norm_status(st)
            bucket = None
            for b, vals in _INVOICE_STATUS_BUCKETS.items():
                if ns in vals:
                    bucket = b
                    break
            if bucket is None:
                bucket = "open"
            counts[bucket] += int(cnt or 0)

    if status_col is not None and total_col is not None:
        rows = db.session.query(status_col, func.coalesce(func.sum(total_col), 0.0)).group_by(status_col).all()
        for st, tot in rows:
            ns = _norm_status(st)
            bucket = None
            for b, vals in _INVOICE_STATUS_BUCKETS.items():
                if ns in vals:
                    bucket = b
                    break
            if bucket is None:
                bucket = "open"
            try:
                totals[bucket] += float(tot or 0.0)
            except Exception:
                pass

    outstanding_total = float(totals.get("draft", 0.0) + totals.get("open", 0.0) + totals.get("overdue", 0.0))
    outstanding_count = int(counts.get("draft", 0) + counts.get("open", 0) + counts.get("overdue", 0))

    lines = [
        "SummaryType: SystemOutstandingBilling",
        f"OutstandingInvoiceCount: {outstanding_count}",
        f"OutstandingInvoiceTotal: ${outstanding_total:.2f}",
        "NOTE: Outstanding totals are invoice totals (draft/open/overdue), not uninvoiced billables.",
    ]

    return RetrievedChunk(
        source_id="SYSTEM.BILLING.OUTSTANDING",
        label="System Outstanding Billing",
        text="\n".join(lines),
        amount=outstanding_total,
        unit="dollars",
        quantity=float(outstanding_count),
        score=1000,
        intent_hint="system_outstanding_billing",
        authority="derived",
    )


def _system_recent_invoices_chunk(limit: int = 12) -> RetrievedChunk:
    # Best-effort recent list for invoice questions
    q = Invoice.query
    if hasattr(Invoice, "created_at"):
        q = q.order_by(Invoice.created_at.desc().nullslast())
    else:
        q = q.order_by(Invoice.id.desc())

    invs = q.limit(limit).all()

    total_col_name = "total_amount" if hasattr(Invoice, "total_amount") else ("total" if hasattr(Invoice, "total") else None)

    lines = ["RecentInvoices:"]
    for inv in invs:
        inv_no = getattr(inv, "invoice_number", None) or ""
        st = getattr(inv, "status", None) or ""
        tot = getattr(inv, total_col_name, None) if total_col_name else None
        claim_id = getattr(inv, "claim_id", None)
        lines.append(
            f"InvoiceID: {inv.id} | InvoiceNumber: {inv_no} | Status: {st} | ClaimID: {claim_id} | Total: {tot}"
        )

    return RetrievedChunk(
        source_id="SYSTEM.INVOICES.RECENT",
        label="Recent Invoices",
        text="\n".join(lines),
        score=900,
        intent_hint="system_recent_invoices",
        authority="authoritative",
    )

# System-wide: explicit claim list chunk for system-level list queries

def _system_claims_list_chunk(limit: int = 200) -> RetrievedChunk:
    """System-wide claim list for list/count questions.

    IMPORTANT: No claimant identifiers (name/phone/email/address/DOB) are included here.
    This is safe to use from any page.
    """

    q = Claim.query
    if hasattr(Claim, "id"):
        q = q.order_by(Claim.id.asc())

    claims = q.limit(limit).all()

    def _safe(obj, *names):
        for n in names:
            if hasattr(obj, n):
                v = getattr(obj, n)
                if v is not None and v != "":
                    return v
        return ""

    lines = ["Claims:"]
    for c in claims:
        status = _safe(c, "status")
        claim_number = _safe(c, "claim_number", "claim_num", "claimno")

        carrier_name = ""
        carrier = getattr(c, "carrier", None)
        if carrier is not None:
            carrier_name = _safe(carrier, "name", "carrier_name")

        employer_name = ""
        employer = getattr(c, "employer", None)
        if employer is not None:
            employer_name = _safe(employer, "name", "employer_name")

        lines.append(
            f"ClaimID: {c.id} | ClaimNumber: {claim_number} | Status: {status} | Carrier: {carrier_name} | Employer: {employer_name}"
        )

    if len(claims) >= limit:
        lines.append(f"NOTE: List truncated to first {limit} claims.")

    return RetrievedChunk(
        source_id="SYSTEM.CLAIMS.LIST",
        label="System Claim List",
        text="\n".join(lines),
        quantity=float(len(claims)),
        unit="count",
        score=950,
        intent_hint="system_claim_list",
        authority="authoritative",
    )


def _claim_invoice_summary_chunk(claim_id: int) -> RetrievedChunk:
    """Claim-level invoice totals and A/R buckets (best-effort)."""
    status_col = getattr(Invoice, "status", None)
    total_col = _invoice_total_expr()

    q = Invoice.query.filter(Invoice.claim_id == claim_id)

    total_invoices = q.count()
    counts = {k: 0 for k in _INVOICE_STATUS_BUCKETS.keys()}
    totals = {k: 0.0 for k in _INVOICE_STATUS_BUCKETS.keys()}

    if status_col is not None:
        rows = (
            db.session.query(status_col, func.count(Invoice.id))
            .filter(Invoice.claim_id == claim_id)
            .group_by(status_col)
            .all()
        )
        for st, cnt in rows:
            ns = _norm_status(st)
            bucket = None
            for b, vals in _INVOICE_STATUS_BUCKETS.items():
                if ns in vals:
                    bucket = b
                    break
            if bucket is None:
                bucket = "open"
            counts[bucket] += int(cnt or 0)

    if status_col is not None and total_col is not None:
        rows = (
            db.session.query(status_col, func.coalesce(func.sum(total_col), 0.0))
            .filter(Invoice.claim_id == claim_id)
            .group_by(status_col)
            .all()
        )
        for st, tot in rows:
            ns = _norm_status(st)
            bucket = None
            for b, vals in _INVOICE_STATUS_BUCKETS.items():
                if ns in vals:
                    bucket = b
                    break
            if bucket is None:
                bucket = "open"
            try:
                totals[bucket] += float(tot or 0.0)
            except Exception:
                pass

    outstanding_total = float(
        totals.get("draft", 0.0) + totals.get("open", 0.0) + totals.get("overdue", 0.0)
    )
    outstanding_count = int(
        counts.get("draft", 0) + counts.get("open", 0) + counts.get("overdue", 0)
    )

    lines = [
        "SummaryType: ClaimInvoiceSummary",
        f"ClaimID: {claim_id}",
        f"TotalInvoices: {total_invoices}",
        f"DraftInvoices: {counts.get('draft', 0)} | DraftTotal: ${totals.get('draft', 0.0):.2f}",
        f"OpenInvoices: {counts.get('open', 0)} | OpenTotal: ${totals.get('open', 0.0):.2f}",
        f"OverdueInvoices: {counts.get('overdue', 0)} | OverdueTotal: ${totals.get('overdue', 0.0):.2f}",
        f"PaidInvoices: {counts.get('paid', 0)} | PaidTotal: ${totals.get('paid', 0.0):.2f}",
        f"OutstandingInvoiceCount: {outstanding_count}",
        f"OutstandingInvoiceTotal: ${outstanding_total:.2f}",
    ]

    return RetrievedChunk(
        source_id=f"CLAIM.{claim_id}.INVOICES.SUMMARY",
        label="Claim Invoices Summary",
        text="\n".join(lines),
        amount=outstanding_total,
        unit="dollars",
        quantity=float(outstanding_count),
        score=999,
        intent_hint="claim_invoice_summary",
        authority="derived",
    )


# --- DISPATCHER + HELPERS: compatible retrieval_context ---
def _first_attr(obj, *names):
    for n in names:
        if hasattr(obj, n):
            v = getattr(obj, n)
            if v is not None and v != "":
                return v
    return ""


def _retrieve_context_chunks(
    *,
    claim_id: Optional[int],
    query: str,
    max_chunks: int = 40,
    max_reports: int = 25,
    scope: Optional[RetrievalScope] = None,
    mode: Optional[str] = None,
) -> List[RetrievedChunk]:
    """Original chunk-based retrieval (Florence facts/chunks/sources)."""

    # HARD DEBUG: prove retrieval is executing and returning data
    chunks: List[RetrievedChunk] = []
    chunks.append(
        RetrievedChunk(
            source_id="DEBUG.RETRIEVAL",
            label="Retrieval Debug",
            text=f"retrieve_context called | claim_id={claim_id} | query={query} | mode={mode} | scope={type(scope).__name__ if scope else None}",
            score=10000,
            intent_hint="debug",
            authority="authoritative",
        )
    )

    # --- ALWAYS include baseline system context ---
    chunks.extend([
        _system_claim_count_chunk(),
        _system_open_claim_count_chunk(),
        _system_closed_claim_count_chunk(),
        _system_invoice_count_chunk(),
        _system_billing_summary_chunk(),
        _system_outstanding_billing_chunk(),
        _system_carrier_count_chunk(),
        _system_employer_count_chunk(),
        _system_provider_count_chunk(),
    ])

    # Determine whether the caller is explicitly requesting system-scope retrieval.
    # This enables Florence to answer system questions from any page (even when a claim_id is present).
    def _wants_system_scope() -> bool:
        # Explicit modes always win
        if mode in {"system", "system_list", "system_only"}:
            return True

        # Scope flags (from chat_engine/ai_service)
        if scope is not None:
            # If claim=False, we are explicitly saying: do not include claim-scoped data.
            if getattr(scope, "claim", True) is False:
                return True
            # cross_claim means we want system / multi-claim context.
            if getattr(scope, "cross_claim", False):
                return True

        # Heuristic: system-ish questions should be allowed to escape claim context
        # even when a claim_id exists (e.g., user asks "how many claims do I have?").
        return _is_systemish_query(query or "")

    wants_system_scope = _wants_system_scope()

    # Detect system-level queries even when a claim_id is present
    intent = classify_intent(query)

    if scope is None:
        scope = RetrievalScope()

    # System-level retrieval.
    # Triggered when claim_id is None OR when the caller explicitly requests system scope.
    # IMPORTANT: If we are in cross-claim mode (not system-only), we can include system chunks
    # *and* still proceed into claim chunks so Florence can reason across both.
    if claim_id is None or wants_system_scope:
        # Preserve the debug chunk if present, then build system facts.
        dbg = [c for c in chunks if c.source_id == "DEBUG.RETRIEVAL"]
        chunks = []
        if dbg:
            chunks.extend(dbg)

        # Always include the core system summary so the assistant can answer broad questions.
        chunks.extend([
            _system_claim_count_chunk(),
            _system_open_claim_count_chunk(),
            _system_closed_claim_count_chunk(),
            _system_invoice_count_chunk(),
            _system_billing_summary_chunk(),
            _system_outstanding_billing_chunk(),
            _system_carrier_count_chunk(),
            _system_employer_count_chunk(),
            _system_provider_count_chunk(),
        ])

        # For list-y questions OR explicit system_list mode, include index-style lists.
        if intent.wants_list or mode == "system_list":
            chunks.append(_system_claims_list_chunk())
            chunks.append(_system_recent_invoices_chunk())
            chunks.append(_system_carriers_list_chunk())
            chunks.append(_system_employers_list_chunk())
            chunks.append(_system_providers_list_chunk())

        # Decide whether we should stop here (system-only) or continue into claim context.
        system_only = (
            claim_id is None
            or mode == "system_only"
            or (scope is not None and getattr(scope, "claim", True) is False)
        )

        if system_only:
            return chunks

        # Otherwise, we fall through and continue claim-scoped retrieval so the assistant
        # can reason with both system + claim context when appropriate.

    claim = Claim.query.get(claim_id) if isinstance(claim_id, int) and claim_id > 0 else None
    if claim:
        chunks.append(_claim_header_chunk(claim_id))
        chunks.append(_claim_identity_chunk(claim))
        chunks.append(_claim_invoice_summary_chunk(claim_id))

        # --- PATCH: Capture reports once and reuse them ---
        reports = []
        if Report is not None:
            try:
                rq = Report.query
                if hasattr(Report, "claim_id"):
                    rq = rq.filter(Report.claim_id == claim_id)

                if hasattr(Report, "dos_end"):
                    rq = rq.order_by(Report.dos_end.desc().nullslast())
                elif hasattr(Report, "created_at"):
                    rq = rq.order_by(Report.created_at.desc().nullslast())
                else:
                    rq = rq.order_by(Report.id.desc())

                reports = rq.limit(max_reports).all()
                for i, r in enumerate(reports):
                    try:
                        chunks.append(_report_chunk(r, idx=i))
                    except Exception:
                        continue
            except Exception:
                reports = []

        # --- PATCH: Add derived “latest report” chunks (small + high-signal) ---
        latest = _latest_report_derived_chunk(reports)
        if latest is not None:
            chunks.append(latest)

        latest_ws = _latest_work_status_derived_chunk(reports)
        if latest_ws is not None:
            chunks.append(latest_ws)

        # --- PATCH: Add compact claim-level anchors (claim + reports) ---
        try:
            cs = _claim_status_derived_chunk(claim=claim, reports=reports)
            if cs is not None:
                chunks.append(cs)
        except Exception:
            pass

        try:
            traj = _care_trajectory_derived_chunk(claim=claim, reports=reports)
            if traj is not None:
                chunks.append(traj)
        except Exception:
            pass

        # Enrich claim context with associated Carrier/Employer/Providers
        carrier = getattr(claim, "carrier", None)
        if carrier is not None:
            chunks.append(_carrier_chunk(carrier))
            _maybe_add_contacts(
                chunks,
                carrier,
                "carrier",
                [
                    "contact",
                    "main_contact",
                    "billing_contact",
                    "adjuster_contact",
                    "carrier_contact",
                ],
            )

        employer = getattr(claim, "employer", None)
        if employer is not None:
            chunks.append(_employer_chunk(employer))
            _maybe_add_contacts(
                chunks,
                employer,
                "employer",
                [
                    "contact",
                    "main_contact",
                    "billing_contact",
                    "employer_contact",
                ],
            )

        providers = getattr(claim, "providers", None)
        if providers:
            for p in providers:
                try:
                    chunks.append(_provider_chunk(p))
                    _maybe_add_contacts(
                        chunks,
                        p,
                        "provider",
                        [
                            "contact",
                            "main_contact",
                            "office_contact",
                        ],
                    )
                except Exception:
                    continue

    def _maybe_contact_any(obj, attr_names: list[str], kind_label: str):
        for a in attr_names:
            c = getattr(obj, a, None)
            if c:
                chunks.append(_contact_chunk(kind_label, c))
                return

    # Fast-path identity queries (bypass heavy billable context)
    if (is_identity_query(query) or mode == "identity") and claim:
        chunks = [
            _claim_header_chunk(claim_id),
            _claim_identity_chunk(claim),
            _claim_invoice_summary_chunk(claim_id),
        ]

        # Include report history even for identity fast-path (so DOB/claim identity questions can
        # still be answered in context of the overall case).
        reports = []
        if Report is not None:
            try:
                rq = Report.query
                if hasattr(Report, "claim_id"):
                    rq = rq.filter(Report.claim_id == claim_id)
                if hasattr(Report, "dos_end"):
                    rq = rq.order_by(Report.dos_end.desc().nullslast())
                elif hasattr(Report, "created_at"):
                    rq = rq.order_by(Report.created_at.desc().nullslast())
                else:
                    rq = rq.order_by(Report.id.desc())

                reports = rq.limit(25).all()
                for i, r in enumerate(reports):
                    try:
                        chunks.append(_report_chunk(r, idx=i))
                    except Exception:
                        continue

                # Add compact anchors even on identity fast-path
                try:
                    cs = _claim_status_derived_chunk(claim=claim, reports=reports)
                    if cs is not None:
                        chunks.append(cs)
                except Exception:
                    pass

                try:
                    traj = _care_trajectory_derived_chunk(claim=claim, reports=reports)
                    if traj is not None:
                        chunks.append(traj)
                except Exception:
                    pass
            except Exception:
                reports = []

        _maybe_contact_any(claim, ["claimant_contact", "claimantContact", "claimant"], "claimant")
        _maybe_contact_any(claim, ["carrier_adjuster_contact", "adjuster_contact", "adjuster"], "carrier adjuster")
        _maybe_contact_any(claim, ["employer_contact", "employerContact"], "employer")

        if getattr(claim, "carrier", None) and getattr(claim.carrier, "name", None):
            chunks.append(
                RetrievedChunk(
                    source_id="CARRIER.NAME",
                    label="Carrier Name",
                    text=f"CarrierName: {claim.carrier.name}",
                    score=1000,
                    intent_hint="carrier_identity",
                )
            )
        if getattr(claim, "employer", None) and getattr(claim.employer, "name", None):
            chunks.append(
                RetrievedChunk(
                    source_id="EMPLOYER.NAME",
                    label="Employer Name",
                    text=f"EmployerName: {claim.employer.name}",
                    score=1000,
                    intent_hint="employer_identity",
                )
            )

        if hasattr(claim, "providers") and claim.providers:
            for idx, provider in enumerate(claim.providers):
                name = getattr(provider, "name", None)
                if name:
                    chunks.append(
                        RetrievedChunk(
                            source_id=f"PROVIDER.{idx}",
                            label="Treating Provider Name",
                            text=f"ProviderName: {name}",
                            score=1000,
                            intent_hint="provider_identity",
                        )
                    )

        # Include core related entities for identity-style questions
        carrier = getattr(claim, "carrier", None)
        if carrier is not None:
            chunks.append(_carrier_chunk(carrier))
            _maybe_add_contacts(
                chunks,
                carrier,
                "carrier",
                ["contact", "main_contact", "billing_contact", "adjuster_contact", "carrier_contact"],
            )

        employer = getattr(claim, "employer", None)
        if employer is not None:
            chunks.append(_employer_chunk(employer))
            _maybe_add_contacts(
                chunks,
                employer,
                "employer",
                ["contact", "main_contact", "billing_contact", "employer_contact"],
            )

        providers = getattr(claim, "providers", None)
        if providers:
            for p in providers:
                try:
                    chunks.append(_provider_chunk(p))
                    _maybe_add_contacts(chunks, p, "provider", ["contact", "main_contact", "office_contact"])
                except Exception:
                    continue

        return chunks

    if claim:
        _maybe_contact_any(claim, ["claimant_contact", "claimantContact", "claimant"], "claimant")
        _maybe_contact_any(claim, ["carrier_adjuster_contact", "adjuster_contact", "adjuster"], "carrier adjuster")
        _maybe_contact_any(claim, ["employer_contact", "employerContact"], "employer")

    billables = (
        BillableItem.query
        .filter(BillableItem.claim_id == claim_id)
        .order_by(
            BillableItem.date_of_service.asc().nullslast(),
            BillableItem.id.asc(),
        )
        .all()
    )

    # Add invoice chunks for this claim (ALWAYS)
    invoices = Invoice.query.filter(Invoice.claim_id == claim_id).all()
    for inv in invoices:
        carrier_name = ""
        employer_name = ""
        if claim is not None:
            cobj = getattr(claim, "carrier", None)
            eobj = getattr(claim, "employer", None)
            if cobj is not None:
                carrier_name = getattr(cobj, "name", None) or getattr(cobj, "carrier_name", None) or ""
            if eobj is not None:
                employer_name = getattr(eobj, "name", None) or getattr(eobj, "employer_name", None) or ""

        lines = [
            f"InvoiceID: {inv.id}",
            f"InvoiceNumber: {getattr(inv, 'invoice_number', '')}",
            f"Status: {getattr(inv, 'status', '')}",
            f"ClaimID: {getattr(inv, 'claim_id', '')}",
            f"CarrierName: {carrier_name}",
            f"EmployerName: {employer_name}",
            f"DOSStart: {getattr(inv, 'dos_start', '')}",
            f"DOSEnd: {getattr(inv, 'dos_end', '')}",
            f"Total: {getattr(inv, 'total_amount', None) if hasattr(inv, 'total_amount') else getattr(inv, 'total', None)}",
        ]

        chunks.append(
            RetrievedChunk(
                source_id=f"INVOICE.{inv.id}",
                label="Invoice",
                text="\n".join(lines),
                quantity=1.0,
                unit="count",
                score=1000,
                intent_hint="invoice",
                authority="authoritative",
            )
        )


    total_hours = 0.0
    total_miles = 0.0
    total_expenses = 0.0
    activity_counts: dict[str, int] = {}
    invoiced = 0
    uninvoiced = 0

    for b in billables:
        chunk = _format_billable_chunk(b)
        chunk.intent_hint = "billable_item"
        if not chunk.quantity:
            continue

        score = 0.0
        text_l = chunk.text.lower()

        for t in _query_tokens(query):
            if t in text_l:
                score += 1.0

        if chunk.activity_code and chunk.activity_code.lower() in _query_tokens(query):
            score += 2.0

        chunk.score = score
        chunks.append(chunk)

        code = chunk.activity_code or ""
        activity_counts[code] = activity_counts.get(code, 0) + 1

        if chunk.unit == "hours":
            total_hours += chunk.quantity
        elif chunk.unit == "miles":
            total_miles += chunk.quantity
        elif chunk.unit == "dollars":
            total_expenses += chunk.quantity

        if chunk.is_invoiced:
            invoiced += 1
        else:
            uninvoiced += 1

    # Summary chunk (only if asked)
    if billables and intent.wants_summary:
        summary_lines = [
            "SummaryType: DerivedBillableTotals",
            "NOTE: Derived from BillableItem.quantity (non‑authoritative)",
            f"Total Hours: {total_hours:.2f}",
            f"Total Miles: {total_miles:.2f}",
            f"Total Expenses: ${total_expenses:.2f}",
            f"Invoiced Items: {invoiced}",
            f"Uninvoiced Items: {uninvoiced}",
        ]

        if activity_counts:
            summary_lines.append("Counts per Activity Code:")
            for k, v in sorted(activity_counts.items()):
                summary_lines.append(f"  {k}: {v}")

        chunks.append(
            RetrievedChunk(
                source_id="BILLABLES.SUMMARY",
                label="Billables – Summary (Derived)",
                text="\n".join(summary_lines),
                score=999,
                intent_hint="billable_summary",
                authority="derived",
            )
        )

        for code, count in sorted(activity_counts.items()):
            if not code:
                continue

            hours = sum(
                c.quantity or 0
                for c in chunks
                if c.intent_hint == "billable_item"
                and c.activity_code == code
                and c.unit == "hours"
            )

            miles = sum(
                c.quantity or 0
                for c in chunks
                if c.intent_hint == "billable_item"
                and c.activity_code == code
                and c.unit == "miles"
            )

            dollars = sum(
                c.quantity or 0
                for c in chunks
                if c.intent_hint == "billable_item"
                and c.activity_code == code
                and c.unit == "dollars"
            )

            lines = [
                "SummaryType: ActivityCodeTotals",
                f"ActivityCode: {code}",
                f"Count: {count}",
                f"Total Hours: {hours:.2f}",
                f"Total Miles: {miles:.2f}",
                f"Total Expenses: ${dollars:.2f}",
            ]

            chunks.append(
                RetrievedChunk(
                    source_id=f"BILLABLES.ACTIVITY.{code}",
                    label=f"Billables – {code} Summary",
                    text="\n".join(lines),
                    score=950,
                    intent_hint="billable_activity_summary",
                    authority="derived",
                )
            )

    # Boost report-related chunks for work/status questions so they survive max_chunks slicing.
    q_tokens = _query_tokens(query or "")
    if {"work", "status"} & q_tokens:
        for c in chunks:
            if c.intent_hint in {"latest_report_summary", "latest_report_work_status", "report"}:
                c.score = (c.score or 0) + 1500

    chunks.sort(key=lambda c: (c.score or 0), reverse=True)
    return chunks[:max_chunks]


def _retrieve_context_structured(
    *,
    claim_id: Optional[int],
    max_billables: int = 80,
    max_reports: int = 12,
    report=None,
) -> dict:
    """Structured retrieval used for deterministic fast-path answers in ai_service.

    Returns:
      {
        "billables": [ {service_date, activity_code, quantity, description, notes, invoice_id, is_invoiced} ...],
        "summary": {hours_total, miles_total, expense_total, billable_count, invoiced_count, uninvoiced_count, no_bill_count}
      }

    NOTE: Reports are not included here yet (model import may vary across branches).
    """

    claim = None
    if isinstance(claim_id, int) and claim_id > 0:
        claim = Claim.query.get(claim_id)

    # Invoices (structured, best-effort)
    invoices_q = (
        Invoice.query
        .filter(Invoice.claim_id == claim_id)
        .order_by(Invoice.id.asc())
        .all()
    )
    out_invoices: List[dict] = []
    for inv in invoices_q:
        out_invoices.append({
            "id": inv.id,
            "invoice_number": getattr(inv, "invoice_number", None),
            "status": getattr(inv, "status", None),
            "dos_start": str(getattr(inv, "dos_start", "") or "") or None,
            "dos_end": str(getattr(inv, "dos_end", "") or "") or None,
            "total_amount": getattr(inv, "total_amount", None),
            "created_at": str(getattr(inv, "created_at", "") or "") or None,
        })

    billables_q = (
        BillableItem.query
        .filter(BillableItem.claim_id == claim_id)
        .order_by(
            BillableItem.date_of_service.asc().nullslast(),
            BillableItem.id.asc(),
        )
        .all()
    )

    # Cap for UI/deterministic responses
    billables_q = billables_q[-max_billables:] if max_billables and len(billables_q) > max_billables else billables_q

    out_billables: List[dict] = []

    hours_total = 0.0
    miles_total = 0.0
    expense_total = 0.0
    invoiced_count = 0
    uninvoiced_count = 0
    no_bill_count = 0

    for b in billables_q:
        code = (b.activity_code or "").upper().strip()
        qty = float(b.quantity or 0.0)
        is_invoiced = b.invoice_id is not None

        if code in EXPENSE_CODES:
            expense_total += qty
        elif code in MILEAGE_CODES:
            miles_total += qty
        else:
            hours_total += qty

        # --- PATCH: fix no_bill_count logic ---
        if code.replace("_", " ").replace("-", " ").strip() == "NO BILL":
            no_bill_count += 1

        if is_invoiced:
            invoiced_count += 1
        else:
            uninvoiced_count += 1

        out_billables.append({
            "id": b.id,
            "service_date": str(b.date_of_service) if getattr(b, "date_of_service", None) else None,
            "activity_code": code or None,
            "quantity": qty,
            "description": (b.description or None),
            "notes": (b.notes or None),
            "invoice_id": b.invoice_id,
            "is_invoiced": is_invoiced,
        })

    summary = {
        "hours_total": round(hours_total, 2),
        "miles_total": round(miles_total, 2),
        "expense_total": round(expense_total, 2),
        "billable_count": len(out_billables),
        "invoiced_count": invoiced_count,
        "uninvoiced_count": uninvoiced_count,
        "no_bill_count": no_bill_count,
    }

    # Header fields (best-effort, avoid hard schema assumptions)
    header = {}
    if claim:
        header = {
            "claim_id": claim_id,
            "claimant_name": _first_attr(claim, "claimant_name", "claimant", "name"),
            "claim_number": _first_attr(claim, "claim_number", "claim_num", "claimno"),
            "claim_state": _first_attr(claim, "claim_state", "state"),
            "doi": _first_attr(claim, "date_of_incident", "doi", "injury_date"),
            "referral_date": _first_attr(claim, "referral_date", "referral"),
            "surgery_date": _first_attr(claim, "surgery_date"),
            "injured_body_part": _first_attr(claim, "injured_body_part"),
            "status": _first_attr(claim, "status"),
        }

        # Optional linked entities (best-effort)
        carrier = getattr(claim, "carrier", None)
        if carrier is not None:
            header["carrier_name"] = _first_attr(carrier, "name", "carrier_name")

        employer = getattr(claim, "employer", None)
        if employer is not None:
            header["employer_name"] = _first_attr(employer, "name", "employer_name")

        providers = getattr(claim, "providers", None)
        if providers:
            header["treating_providers"] = [
                _first_attr(p, "name", "provider_name") for p in providers if _first_attr(p, "name", "provider_name")
            ]

        # Full associated objects (best-effort) for deterministic logic
        header["carrier"] = None
        header["employer"] = None
        header["providers"] = []

        def _first_attr_local(obj, *names):
            for n in names:
                if hasattr(obj, n):
                    v = getattr(obj, n)
                    if v is not None and v != "":
                        return v
            return ""

        if carrier is not None:
            header["carrier"] = {
                "id": getattr(carrier, "id", None),
                "name": _first_attr_local(carrier, "name", "carrier_name"),
                "phone": _first_attr_local(carrier, "phone"),
                "email": _first_attr_local(carrier, "email"),
                "fax": _first_attr_local(carrier, "fax"),
                "address1": _first_attr_local(carrier, "address1", "address_line1"),
                "address2": _first_attr_local(carrier, "address2", "address_line2"),
                "city": _first_attr_local(carrier, "city"),
                "state": _first_attr_local(carrier, "state"),
                "zip": _first_attr_local(carrier, "zip", "postal_code"),
                "notes": _first_attr_local(carrier, "notes"),
            }

        if employer is not None:
            header["employer"] = {
                "id": getattr(employer, "id", None),
                "name": _first_attr_local(employer, "name", "employer_name"),
                "phone": _first_attr_local(employer, "phone"),
                "email": _first_attr_local(employer, "email"),
                "fax": _first_attr_local(employer, "fax"),
                "address1": _first_attr_local(employer, "address1", "address_line1"),
                "address2": _first_attr_local(employer, "address2", "address_line2"),
                "city": _first_attr_local(employer, "city"),
                "state": _first_attr_local(employer, "state"),
                "zip": _first_attr_local(employer, "zip", "postal_code"),
                "notes": _first_attr_local(employer, "notes"),
            }

        if providers:
            for p in providers:
                header["providers"].append({
                    "id": getattr(p, "id", None),
                    "name": _first_attr_local(p, "name", "provider_name"),
                    "specialty": _first_attr_local(p, "specialty"),
                    "phone": _first_attr_local(p, "phone"),
                    "email": _first_attr_local(p, "email"),
                    "fax": _first_attr_local(p, "fax"),
                    "address1": _first_attr_local(p, "address1", "address_line1"),
                    "address2": _first_attr_local(p, "address2", "address_line2"),
                    "city": _first_attr_local(p, "city"),
                    "state": _first_attr_local(p, "state"),
                    "zip": _first_attr_local(p, "zip", "postal_code"),
                    "notes": _first_attr_local(p, "notes", "contact_notes"),
                })

    # Historical reports (structured, best-effort)
    prior_reports: List[dict] = []
    if Report is not None and isinstance(claim_id, int) and claim_id > 0:
        try:
            rq = Report.query
            if hasattr(Report, "claim_id"):
                rq = rq.filter(Report.claim_id == claim_id)

            if hasattr(Report, "dos_end"):
                rq = rq.order_by(Report.dos_end.desc().nullslast())
            elif hasattr(Report, "created_at"):
                rq = rq.order_by(Report.created_at.desc().nullslast())
            else:
                rq = rq.order_by(Report.id.desc())

            reports = rq.limit(max_reports).all() if max_reports else rq.all()

            # Best-effort long text fields
            long_fields = [
                "status_treatment_plan",
                "status",
                "treatment_plan",
                "work_status",
                "case_management_plan",
                "case_management_impact",
                "closure_details",
                "reason_for_closure",
                "diagnosis",
                "mechanism_of_injury",
                "co_existing_conditions",
                "surgical_history",
                "medications",
                "diagnostics",
                "barriers_json",
                "next_appointment",
                "next_appointment_notes",
                "employment_status",
            ]

            for r in reports:
                item = {
                    "id": getattr(r, "id", None),
                    "report_type": getattr(r, "report_type", None),
                    "created_at": str(getattr(r, "created_at", "") or "") or None,
                    "updated_at": str(getattr(r, "updated_at", "") or "") or None,
                    "dos_start": str(getattr(r, "dos_start", "") or "") or None,
                    "dos_end": str(getattr(r, "dos_end", "") or "") or None,
                    "next_report_due": str(getattr(r, "next_report_due", "") or "") or None,
                }

                # Treating provider (name only)
                tp = getattr(r, "treating_provider", None)
                if tp is not None:
                    item["treating_provider"] = _first_attr(tp, "name", "provider_name")

                for lf in long_fields:
                    if hasattr(r, lf):
                        v = getattr(r, lf)
                        if v is not None and v != "":
                            item[lf] = v

                prior_reports.append(item)
        except Exception:
            pass

    return {
        "billables": out_billables,
        "billable_summary": summary,
        "invoices": out_invoices,
        "header": header,
        "current_report_fields": {},
        "current_report": {},
        "prior_reports": prior_reports,
    }


def retrieve_context(
    *,
    claim_id: Optional[int],
    query: Optional[str] = None,
    max_chunks: int = 40,
    scope: Optional[RetrievalScope] = None,
    mode: Optional[str] = None,
    report=None,
    max_billables: int = 80,
    max_reports: int = 12,
):
    """Compatibility wrapper.

    - If `query` is provided: returns `List[RetrievedChunk]` (original behavior).
    - If `query` is None: returns structured dict used by ai_service fast-paths.
    """

    if query is None:
        # Structured retrieval for fast-path deterministic answers
        return _retrieve_context_structured(
            claim_id=claim_id,
            max_billables=max_billables,
            max_reports=max_reports,
        )

    return _retrieve_context_chunks(
        claim_id=claim_id,
        query=query,
        max_chunks=max_chunks,
        max_reports=max_reports,
        scope=scope,
        mode=mode,
    )


def retrieve(*args, **kwargs):
    """
    Adapter for Florence.
    Converts RetrievedChunk objects into facts/chunks/sources payload.
    """
    # Normalize inputs from Florence (positional or keyword)
    claim_id = kwargs.get("claim_id")
    query = kwargs.get("query")

    if len(args) >= 1 and claim_id is None:
        claim_id = args[0]
    if len(args) >= 2 and query is None:
        query = args[1]

    scope = kwargs.get("scope")
    mode = kwargs.get("mode")

    # Coerce common types
    if isinstance(claim_id, str):
        s = claim_id.strip()
        if s.isdigit():
            claim_id = int(s)

    if isinstance(scope, dict):
        try:
            scope = RetrievalScope(**scope)
        except Exception:
            scope = None

    # Normalize empty/invalid claim_id to None
    if claim_id in ("", 0):
        claim_id = None
    if claim_id is not None and not isinstance(claim_id, int):
        claim_id = None

    # IMPORTANT: Do NOT send full database context unless explicitly needed.
    # Context must be domain-shaped or the LLM will collapse to claims analysis.
    full_snapshot = _select_system_snapshot(query)

    # Retrieve chunked context as supplemental signal (ranking / anchors)
    chunks = retrieve_context(
        claim_id=claim_id,
        query=query,
        scope=scope,
        mode=mode,
    )

    chunk_dicts = []
    if isinstance(chunks, list):
        chunk_dicts = [
            {
                "source_id": c.source_id,
                "label": c.label,
                "text": c.text,
                "score": c.score,
                "quantity": c.quantity,
                "unit": c.unit,
                "activity_code": c.activity_code,
                "amount": c.amount,
                "invoice_id": c.invoice_id,
                "is_invoiced": c.is_invoiced,
                "intent_hint": c.intent_hint,
                "authority": c.authority,
            }
            for c in chunks
        ]

    return {
        "full_snapshot": full_snapshot,
        "facts": chunk_dicts,
        "chunks": chunk_dicts,
        "sources": [c["source_id"] for c in chunk_dicts],
    }
