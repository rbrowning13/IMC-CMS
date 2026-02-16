

"""Claim-related routes.

This module was split out of the old monolithic routes.py.

Notes during transition:
- Some small helpers are duplicated here temporarily (date parsing, settings loader)
  until we consolidate them into app/routes/helpers.py.
- Claim-level PCP has been removed. PCP will live on Initial Reports only.
"""

from __future__ import annotations

from datetime import timedelta

import os
from pathlib import Path
from inspect import signature

from flask import abort, redirect, render_template, request, url_for, flash, send_file, current_app, jsonify
from sqlalchemy import bindparam, inspect, text, select, or_
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import (
    BillingActivityCode,
    Carrier,
    Claim,
    ClaimDocument,
    Contact,
    Employer,
    Invoice,
    Report,
    Settings,
    BillableItem,
    Provider,
    now,
    today,
)

from . import bp

#
# Canonical invoice math (same as invoice detail/print)
try:
    from .helpers import compute_invoice_financials as _helpers_compute_invoice_financials  # type: ignore
except Exception:  # pragma: no cover
    _helpers_compute_invoice_financials = None  # type: ignore

# AI claim query helper
from ..services import ai_service




# ---- helpers (temporary duplicates; will move to routes/helpers.py) ----

def _parse_date(value: str | None):
    """Parse UI date input.

    Accepts 'YYYY-MM-DD' (native date input) or 'MM/DD/YYYY' (text input).
    Returns datetime.date or None.
    """

    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _ensure_settings() -> Settings:
    """Return the singleton Settings row, creating it if needed."""

    settings = Settings.query.first()
    if not settings:
        settings = Settings(
            business_name="Impact Medical Consulting, PLLC",
            state="ID",
            hourly_rate=50.0,
            telephonic_rate=50.0,
            mileage_rate=0.50,
        )
        db.session.add(settings)
        db.session.commit()
    return settings




def _table_exists(table_name: str) -> bool:
    """Return True if the given table exists in the current DB."""

    try:
        return inspect(db.engine).has_table(table_name)
    except Exception:
        # If inspection fails for any reason, be conservative and assume it exists
        # so we don't silently skip deletes.
        return True


# ---- claim table schema helpers ----

def _claim_has_is_closed_column() -> bool:
    """Return True if the `claim` table has an `is_closed` boolean column.

    We must not rely on `hasattr(Claim, 'is_closed')` because the DB may be migrated
    ahead of the ORM model, and we still need correct filtering/updates.
    """
    try:
        insp = inspect(db.engine)
        cols = [c.get("name") for c in insp.get_columns("claim")]
        cols_set = {str(c).lower() for c in cols if c}
        return "is_closed" in cols_set
    except Exception:
        # Be conservative: if inspection fails, do NOT assume it exists.
        return False


# ---- claim-level treating providers (join table) ----

def _claim_provider_table_name() -> str | None:
    """Best-effort: return the join table name used for claim<->provider.

    We support multiple historical names to avoid breaking older DBs.
    Expected columns (best case): claim_id, provider_id, sort_order
    """

    candidates = [
        "claim_treating_provider",
        "claim_provider",
        "claim_approved_provider",
    ]
    for t in candidates:
        if _table_exists(t):
            return t

    # Fallback: auto-detect a join table that has BOTH claim_id and provider_id.
    try:
        insp = inspect(db.engine)
        table_names = insp.get_table_names()
        best = None
        best_score = -1

        for tname in table_names:
            try:
                cols = [c.get("name") for c in insp.get_columns(tname)]
            except Exception:
                continue
            if not cols:
                continue

            cols_set = {str(c).lower() for c in cols if c}
            if "claim_id" in cols_set and "provider_id" in cols_set:
                score = 0
                low = tname.lower()
                if "claim" in low:
                    score += 2
                if "provider" in low:
                    score += 2
                if "treat" in low or "approved" in low:
                    score += 1
                if low.endswith("_provider") or low.endswith("_providers"):
                    score += 1

                if score > best_score:
                    best = tname
                    best_score = score

        if best:
            return best
    except Exception:
        pass

    return None


def _claim_load_provider_ids(claim_id: int) -> list[int]:
    """Return provider IDs for the claim, preserving sort order when available."""

    t = _claim_provider_table_name()
    if not t:
        return []

    # Prefer sort_order if present; fall back to provider_id ordering.
    try:
        rows = db.session.execute(
            text(
                f"""
                SELECT provider_id
                FROM {t}
                WHERE claim_id = :claim_id
                ORDER BY sort_order NULLS LAST, provider_id
                """
            ),
            {"claim_id": claim_id},
        ).fetchall()
        return [int(r[0]) for r in rows if r and r[0] is not None]
    except Exception:
        db.session.rollback()
        rows = db.session.execute(
            text(
                f"""
                SELECT provider_id
                FROM {t}
                WHERE claim_id = :claim_id
                ORDER BY provider_id
                """
            ),
            {"claim_id": claim_id},
        ).fetchall()
        return [int(r[0]) for r in rows if r and r[0] is not None]


def _claim_set_provider_ids(claim_id: int, provider_ids: list[int]) -> bool:
    """Replace the claim's provider list in the join table.

    This is intentionally SQL-based so we can support legacy DBs without relying on
    ORM relationship names.

    Returns True if the operation succeeded (or there was nothing to save), False if
    the join table could not be found.

    IMPORTANT: Do NOT rely on a failing INSERT to detect legacy schemas. In Postgres,
    a failed statement aborts the current transaction, and subsequent statements will
    raise InFailedSqlTransaction until we rollback.
    """

    t = _claim_provider_table_name()
    if not t:
        return False

    # If a prior statement in this request failed, the session can be left in an
    # aborted transaction state. Clear it so we don't mask the real error.
    try:
        db.session.rollback()
    except Exception:
        pass

    # De-dupe while preserving order
    cleaned: list[int] = []
    seen: set[int] = set()
    for pid in provider_ids:
        try:
            ipid = int(pid)
        except Exception:
            continue
        if ipid <= 0 or ipid in seen:
            continue
        seen.add(ipid)
        cleaned.append(ipid)

    # Detect whether this join table supports sort_order
    has_sort_order = False
    try:
        insp = inspect(db.engine)
        cols = [c.get("name") for c in insp.get_columns(t)]
        cols_set = {str(c).lower() for c in cols if c}
        has_sort_order = "sort_order" in cols_set
    except Exception:
        has_sort_order = False

    try:
        # Clear existing rows
        db.session.execute(
            text(f"DELETE FROM {t} WHERE claim_id = :claim_id"),
            {"claim_id": claim_id},
        )

        # Nothing selected is a valid state (we just cleared rows)
        if not cleaned:
            return True

        if has_sort_order:
            for idx, pid in enumerate(cleaned, start=1):
                db.session.execute(
                    text(
                        f"""
                        INSERT INTO {t} (claim_id, provider_id, sort_order)
                        VALUES (:claim_id, :provider_id, :sort_order)
                        """
                    ),
                    {"claim_id": claim_id, "provider_id": pid, "sort_order": idx},
                )
        else:
            for pid in cleaned:
                db.session.execute(
                    text(
                        f"""
                        INSERT INTO {t} (claim_id, provider_id)
                        VALUES (:claim_id, :provider_id)
                        """
                    ),
                    {"claim_id": claim_id, "provider_id": pid},
                )

        return True

    except Exception:
        # Roll back so the caller doesn't hit InFailedSqlTransaction and so the
        # real underlying error can surface.
        db.session.rollback()
        try:
            current_app.logger.exception("Failed to save claim treating providers")
        except Exception:
            pass
        raise



# ---- claim-level surgery dates (multi) ----

def _claim_surgery_table_name() -> str | None:
    """Return the table name used for claim surgery dates (multi-surgery).

    Expected columns: claim_id, surgery_date, description, sort_order
    """

    candidates = [
        "claim_surgery",
        "claim_surgeries",
        "claim_surgery_date",
    ]
    for t in candidates:
        if _table_exists(t):
            return t
    return None


def _claim_load_surgeries(claim_id: int) -> list[dict]:
    """Return surgeries for the claim as a list of dicts.

    Each dict may contain: surgery_date (date) and description (str).
    Preserves sort_order when available.
    """

    t = _claim_surgery_table_name()
    if not t:
        return []

    # Prefer sort_order if present; tolerate legacy tables by falling back.
    try:
        rows = db.session.execute(
            text(
                f"""
                SELECT surgery_date, description
                FROM {t}
                WHERE claim_id = :claim_id
                ORDER BY sort_order NULLS LAST, surgery_date NULLS LAST
                """
            ),
            {"claim_id": claim_id},
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            if not r:
                continue
            out.append({
                "surgery_date": r[0],
                "description": (r[1] or "") if len(r) > 1 else "",
            })
        return out
    except Exception:
        db.session.rollback()
        rows = db.session.execute(
            text(
                f"""
                SELECT surgery_date, description
                FROM {t}
                WHERE claim_id = :claim_id
                ORDER BY surgery_date NULLS LAST
                """
            ),
            {"claim_id": claim_id},
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            if not r:
                continue
            out.append({
                "surgery_date": r[0],
                "description": (r[1] or "") if len(r) > 1 else "",
            })
        return out


def _claim_set_surgeries(claim_id: int, surgeries: list[dict]) -> bool:
    """Replace the claim's surgery list in the surgeries table.

    `surgeries` expects dicts with keys: surgery_date (date) and description (str).
    Returns False if the table cannot be found.
    """

    t = _claim_surgery_table_name()
    if not t:
        return False

    # If a prior statement in this request failed, the session can be left in an
    # aborted transaction state. Clear it so we don't mask the real error.
    try:
        db.session.rollback()
    except Exception:
        pass

    cleaned: list[dict] = []
    for s in surgeries:
        d = s.get("surgery_date")
        if not d:
            continue
        desc = (s.get("description") or "").strip()
        cleaned.append({"surgery_date": d, "description": desc})

    # Clear existing rows
    db.session.execute(
        text(f"DELETE FROM {t} WHERE claim_id = :claim_id"),
        {"claim_id": claim_id},
    )

    if not cleaned:
        return True

    # Insert with sort_order when available; fallback if legacy table lacks columns.
    try:
        for idx, s in enumerate(cleaned, start=1):
            db.session.execute(
                text(
                    f"""
                    INSERT INTO {t} (claim_id, surgery_date, description, sort_order)
                    VALUES (:claim_id, :surgery_date, :description, :sort_order)
                    """
                ),
                {
                    "claim_id": claim_id,
                    "surgery_date": s["surgery_date"],
                    "description": s["description"],
                    "sort_order": idx,
                },
            )
    except Exception:
        db.session.rollback()
        try:
            db.session.execute(
                text(f"DELETE FROM {t} WHERE claim_id = :claim_id"),
                {"claim_id": claim_id},
            )
            for s in cleaned:
                db.session.execute(
                    text(
                        f"""
                        INSERT INTO {t} (claim_id, surgery_date, description)
                        VALUES (:claim_id, :surgery_date, :description)
                        """
                    ),
                    {
                        "claim_id": claim_id,
                        "surgery_date": s["surgery_date"],
                        "description": s["description"],
                    },
                )
        except Exception:
            db.session.rollback()
            try:
                current_app.logger.exception("Failed to save claim surgeries")
            except Exception:
                pass
            raise

    return True

def _claimant_title_name(raw: str | None) -> str:
    """Best-effort formatting for tab titles.

    Stored data is typically one string (often "Last, First" or "First Last").
    We normalize to "Last, First" when possible.
    """

    name = (raw or "").strip()
    if not name:
        return "Claim"

    # Already "Last, First" style
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        last = parts[0]
        first = parts[1] if len(parts) > 1 else ""
        return f"{last}, {first}".strip().strip(",")

    # Try "First Last" -> "Last, First"
    bits = [b for b in name.split() if b.strip()]
    if len(bits) >= 2:
        first = " ".join(bits[:-1])
        last = bits[-1]
        return f"{last}, {first}"

    return name


def _claim_page_title(claim: Claim, suffix: str | None = None) -> str:
    # Prefer structured name when available
    base_raw = None
    try:
        base_raw = getattr(claim, "claimant_sort_last_first", None)
    except Exception:
        base_raw = None
    if not base_raw:
        base_raw = getattr(claim, "claimant_name", None)

    base = _claimant_title_name(base_raw)
    if suffix:
        return f"{base} — {suffix}"
    return base


# ---- carry-forward helper ----

def _apply_report_carry_forward(*, claim_id: int, new_report: Report) -> None:
    """Carry forward barriers from the most recent prior report.

    - Barriers are stored on Report.barriers_json.
    """

    prev = (
        Report.query.filter_by(claim_id=claim_id)
        .order_by(Report.created_at.desc())
        .first()
    )
    if not prev:
        return

    # Carry forward barriers
    try:
        if getattr(prev, "barriers_json", None):
            new_report.barriers_json = prev.barriers_json
    except Exception:
        pass

# ---- routes ----


@bp.route("/")
@bp.route("/claims")
def claims_list():
    # Optional filters
    activity_filter = (
        (request.args.get("activity") or request.args.get("status") or "").strip().lower()
    )
    billing_filter = (request.args.get("billing") or "").strip().lower()
    # Closed-claim filter (default: hide closed).
    # The UI may send different param names depending on the control.
    _show_closed_raw = (
        request.args.get("show_closed")
        or request.args.get("include_closed")
        or request.args.get("showClosed")
        or request.args.get("closed")
        or ""
    )
    show_closed = str(_show_closed_raw).strip().lower() in ("1", "true", "yes", "on")

    # Base query
    q = Claim.query


    claims = (
        q.order_by(
            Claim.claimant_last_name.asc().nullslast(),
            Claim.claimant_first_name.asc().nullslast(),
            Claim.id.desc(),
        ).all()
    )

    if activity_filter not in ("active", "dormant"):
        activity_filter = "all"
    if billing_filter not in ("none", "open", "closed"):
        billing_filter = "all"

    # Dormant status calculation
    dormant_info = {}
    dormant_threshold_days = _ensure_settings().dormant_claim_days or 0
    for c in claims:
        last_date = None

        r = (
            Report.query.filter_by(claim_id=c.id)
            .order_by(Report.created_at.desc())
            .first()
        )
        if r and r.created_at:
            last_date = r.created_at.date()

        b = (
            BillableItem.query.filter_by(claim_id=c.id)
            .order_by(BillableItem.created_at.desc())
            .first()
        )
        if b and b.created_at:
            d = b.created_at.date()
            if not last_date or d > last_date:
                last_date = d

        inv = (
            Invoice.query.filter_by(claim_id=c.id)
            .order_by(Invoice.id.desc())
            .first()
        )
        if inv and inv.invoice_date:
            d = inv.invoice_date
            if not last_date or d > last_date:
                last_date = d

        doc = (
            ClaimDocument.query.filter_by(claim_id=c.id)
            .order_by(ClaimDocument.uploaded_at.desc())
            .first()
        )
        if doc and doc.uploaded_at:
            d = doc.uploaded_at.date()
            if not last_date or d > last_date:
                last_date = d

        if last_date:
            delta = (today() - last_date).days
            is_dormant = dormant_threshold_days > 0 and delta >= dormant_threshold_days
        else:
            is_dormant = False

        dormant_info[c.id] = {
            "is_dormant": is_dormant,
            "last_activity": last_date,
        }

    # Billing summary per-claim
    billing_summary = {}
    if claims:
        claim_ids = [c.id for c in claims]
        invoices = Invoice.query.filter(Invoice.claim_id.in_(claim_ids)).all()

        for cid in claim_ids:
            billing_summary[cid] = {"total": 0, "open": 0, "closed": 0}

        for inv in invoices:
            cid = inv.claim_id
            status = (inv.status or "Draft")
            entry = billing_summary.get(cid)
            if not entry:
                entry = {"total": 0, "open": 0, "closed": 0}
                billing_summary[cid] = entry

            entry["total"] += 1
            if status in ("Paid", "Void"):
                entry["closed"] += 1
            else:
                entry["open"] += 1

    # Add claim_status_map (Open/Closed) for each claim
    claim_status_map: dict[int, str] = {c.id: "Open" for c in claims}
    if claims and _claim_has_is_closed_column():
        claim_ids = [c.id for c in claims]
        try:
            rows = db.session.execute(
                text(
                    """
                    SELECT id, COALESCE(is_closed, FALSE) AS is_closed
                    FROM claim
                    WHERE id = ANY(:ids)
                    """
                ),
                {"ids": claim_ids},
            ).fetchall()
            for rid, is_closed in rows:
                try:
                    claim_status_map[int(rid)] = "Closed" if bool(is_closed) else "Open"
                except Exception:
                    continue
        except Exception:
            db.session.rollback()

    # ------------------------------------------------------------
    # Next Report Due per claim (latest report only)
    # ------------------------------------------------------------
    next_report_due_map: dict[int, date | None] = {}

    for c in claims:
        latest_report = (
            Report.query.filter_by(claim_id=c.id)
            .order_by(Report.created_at.desc())
            .first()
        )
        if latest_report and getattr(latest_report, "next_report_due", None):
            next_report_due_map[c.id] = latest_report.next_report_due
        else:
            next_report_due_map[c.id] = None

    today_value = today()

    # Apply filters
    filtered_claims = []
    for c in claims:
        info = dormant_info.get(c.id, {})
        is_dormant = bool(info.get("is_dormant", False))

        if activity_filter == "active" and is_dormant:
            continue
        if activity_filter == "dormant" and not is_dormant:
            continue

        summary = billing_summary.get(c.id, {"total": 0, "open": 0, "closed": 0})
        total_inv = summary["total"]
        open_inv = summary["open"]
        closed_inv = summary["closed"]

        if billing_filter == "none" and total_inv != 0:
            continue
        if billing_filter == "open" and open_inv <= 0:
            continue
        if billing_filter == "closed" and closed_inv <= 0:
            continue

        filtered_claims.append(c)

    return render_template(
        "claims_list.html",
        active_page="claims",
        page_title="Claims",
        claims=filtered_claims,
        billing_summary=billing_summary,
        dormant_info=dormant_info,
        activity_filter=activity_filter,
        status_filter=activity_filter,  # For backward compatibility in template
        billing_filter=billing_filter,
        show_closed=show_closed,
        claim_status_map=claim_status_map,
        next_report_due_map=next_report_due_map,
        today=today_value,
    )


@bp.route("/claims/new", methods=["GET", "POST"])
def new_claim():
    carriers = Carrier.query.order_by(Carrier.name).all()
    employers = Employer.query.order_by(Employer.name).all()
    providers = Provider.query.order_by(Provider.name).all()

    carrier_contacts = (
        Contact.query.filter(Contact.carrier_id.isnot(None))
        .order_by(Contact.name)
        .all()
    )

    error = None
    claim_surgeries: list[dict] = []

    if request.method == "POST":
        claimant_first_name = (request.form.get("claimant_first_name") or "").strip() or None
        claimant_last_name = (request.form.get("claimant_last_name") or "").strip() or None
        claimant_name_legacy = (request.form.get("claimant_name") or "").strip()

        # Keep legacy claimant_name in sync for downstream compatibility.
        if claimant_first_name or claimant_last_name:
            claimant_name = " ".join([x for x in [claimant_first_name or "", claimant_last_name or ""] if x]).strip()
        else:
            claimant_name = claimant_name_legacy

        claim_number = (request.form.get("claim_number") or "").strip()

        dob_raw = (request.form.get("dob") or "").strip()
        doi_raw = (request.form.get("doi") or "").strip()
        # surgery_date_raw = (request.form.get("surgery_date") or "").strip()

        claim_state = (request.form.get("claim_state") or "").strip() or None

        injured_body_part = (request.form.get("injured_body_part") or "").strip() or None

        claimant_address1 = (request.form.get("claimant_address1") or "").strip() or None
        claimant_address2 = (request.form.get("claimant_address2") or "").strip() or None
        claimant_city = (request.form.get("claimant_city") or "").strip() or None
        claimant_state = (request.form.get("claimant_state") or "").strip() or None
        claimant_postal_code = (request.form.get("claimant_postal_code") or "").strip() or None
        claimant_phone = (request.form.get("claimant_phone") or "").strip() or None
        claimant_email = (request.form.get("claimant_email") or "").strip() or None

        carrier_id_raw = (request.form.get("carrier_id") or "").strip()
        employer_id_raw = (request.form.get("employer_id") or "").strip()
        carrier_contact_id_raw = (request.form.get("carrier_contact_id") or "").strip()

        provider_ids_raw = request.form.getlist("provider_ids") or request.form.getlist("provider_id")

        dob = _parse_date(dob_raw)
        doi = _parse_date(doi_raw)
        # Multi-surgery dates
        surgery_dates_raw = request.form.getlist("surgery_date")
        surgery_desc_raw = request.form.getlist("surgery_desc")

        surgeries_payload: list[dict] = []
        for idx, draw in enumerate(surgery_dates_raw):
            d = _parse_date((draw or "").strip())
            if not d:
                continue
            desc = ""
            if idx < len(surgery_desc_raw):
                desc = (surgery_desc_raw[idx] or "").strip()
            surgeries_payload.append({"surgery_date": d, "description": desc})

        # For re-render on validation errors
        claim_surgeries = surgeries_payload

        # Legacy single-column (keep for backward compatibility)
        surgery_date = surgeries_payload[0]["surgery_date"] if surgeries_payload else None

        if not claimant_name or not claim_number:
            error = "Claimant name and claim number are required."
        else:
            existing = Claim.query.filter_by(claim_number=claim_number).first()
            if existing:
                error = "A claim with that claim number already exists."
            else:
                claim_kwargs = dict(
                    claimant_name=claimant_name,
                    claimant_first_name=claimant_first_name,
                    claimant_last_name=claimant_last_name,
                    claim_number=claim_number,
                    dob=dob,
                    doi=doi,
                    surgery_date=surgery_date,
                    injured_body_part=injured_body_part,
                    claim_state=claim_state,
                    is_telephonic=False,
                    claimant_address1=claimant_address1,
                    claimant_address2=claimant_address2,
                    claimant_city=claimant_city,
                    claimant_state=claimant_state,
                    claimant_postal_code=claimant_postal_code,
                    claimant_phone=claimant_phone,
                    claimant_email=claimant_email,
                )
                # Default to open if DB supports it (model may lag migrations)
                if _claim_has_is_closed_column() and hasattr(Claim, "is_closed"):
                    claim_kwargs["is_closed"] = False
                claim = Claim(**claim_kwargs)
                if _claim_has_is_closed_column() and not hasattr(Claim, "is_closed"):
                    # ORM model is behind; DB default will apply, but keep this explicit if possible.
                    try:
                        setattr(claim, "is_closed", False)
                    except Exception:
                        pass

                if carrier_id_raw:
                    try:
                        claim.carrier_id = int(carrier_id_raw)
                    except ValueError:
                        pass

                if employer_id_raw:
                    try:
                        claim.employer_id = int(employer_id_raw)
                    except ValueError:
                        pass

                if carrier_contact_id_raw:
                    try:
                        claim.carrier_contact_id = int(carrier_contact_id_raw)
                    except ValueError:
                        pass

                db.session.add(claim)
                try:
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
                    error = "A claim with that claim number already exists."
                else:
                    if provider_ids_raw is not None:
                        try:
                            _claim_set_provider_ids(
                                claim.id,
                                [int(x) for x in provider_ids_raw if str(x).strip().isdigit()],
                            )
                            db.session.commit()
                        except Exception:
                            db.session.rollback()
                            flash("Could not save treating providers. Check server logs for details.", "error")
                    # Save multi-surgery list if the table exists
                    if surgeries_payload and _claim_surgery_table_name():
                        _claim_set_surgeries(claim.id, surgeries_payload)
                        db.session.commit()
                    elif _claim_surgery_table_name():
                        # Field present but empty list is authoritative
                        _claim_set_surgeries(claim.id, [])
                        db.session.commit()
                    return redirect(url_for("main.claim_detail", claim_id=claim.id))

    return render_template(
        "claim_new.html",
        active_page="claims",
        page_title="New Claim",
        carriers=carriers,
        employers=employers,
        carrier_contacts=carrier_contacts,
        providers=providers,
        error=error,
        claim_surgeries=claim_surgeries,
    )


@bp.route("/claims/<int:claim_id>/edit", methods=["GET", "POST"])
def claim_edit(claim_id: int):
    claim = Claim.query.get_or_404(claim_id)
    error = None

    carriers = Carrier.query.order_by(Carrier.name).all()
    employers = Employer.query.order_by(Employer.name).all()
    providers = Provider.query.order_by(Provider.name).all()
    selected_provider_ids = _claim_load_provider_ids(claim.id)
    claim_surgeries = _claim_load_surgeries(claim.id)

    carrier_contacts = []
    if claim.carrier_id:
        carrier_contacts = (
            Contact.query.filter_by(carrier_id=claim.carrier_id)
            .order_by(Contact.name)
            .all()
        )

    if request.method == "POST":
        claimant_first_name = (request.form.get("claimant_first_name") or "").strip() or None
        claimant_last_name = (request.form.get("claimant_last_name") or "").strip() or None
        claimant_name_legacy = (request.form.get("claimant_name") or "").strip()

        if claimant_first_name or claimant_last_name:
            claimant_name = " ".join([x for x in [claimant_first_name or "", claimant_last_name or ""] if x]).strip()
        else:
            claimant_name = claimant_name_legacy

        claim_number = (request.form.get("claim_number") or "").strip()

        dob = _parse_date(request.form.get("dob"))
        doi = _parse_date(request.form.get("doi"))
        # surgery_date = _parse_date(request.form.get("surgery_date"))

        # Multi-surgery dates
        surgery_dates_raw = request.form.getlist("surgery_date")
        surgery_desc_raw = request.form.getlist("surgery_desc")

        surgeries_payload: list[dict] = []
        for idx, draw in enumerate(surgery_dates_raw):
            d = _parse_date((draw or "").strip())
            if not d:
                continue
            desc = ""
            if idx < len(surgery_desc_raw):
                desc = (surgery_desc_raw[idx] or "").strip()
            surgeries_payload.append({"surgery_date": d, "description": desc})

        # For re-render on validation errors
        if error:
            claim_surgeries = surgeries_payload

        # Legacy single-column (keep for backward compatibility)
        surgery_date = surgeries_payload[0]["surgery_date"] if surgeries_payload else None

        claim_state = (request.form.get("claim_state") or "").strip() or None

        injured_body_part = (request.form.get("injured_body_part") or "").strip() or None

        claimant_address1 = (request.form.get("claimant_address1") or "").strip() or None
        claimant_address2 = (request.form.get("claimant_address2") or "").strip() or None
        claimant_city = (request.form.get("claimant_city") or "").strip() or None
        claimant_state = (request.form.get("claimant_state") or "").strip() or None
        claimant_postal_code = (request.form.get("claimant_postal_code") or "").strip() or None
        claimant_phone = (request.form.get("claimant_phone") or "").strip() or None
        claimant_email = (request.form.get("claimant_email") or "").strip() or None

        carrier_id_raw = (request.form.get("carrier_id") or "").strip()
        employer_id_raw = (request.form.get("employer_id") or "").strip()
        carrier_contact_id_raw = (request.form.get("carrier_contact_id") or "").strip()

        provider_ids_raw = request.form.getlist("provider_ids") or request.form.getlist("provider_id")

        carrier_id = None
        if carrier_id_raw:
            try:
                carrier_id = int(carrier_id_raw)
            except ValueError:
                carrier_id = None

        employer_id = None
        if employer_id_raw:
            try:
                employer_id = int(employer_id_raw)
            except ValueError:
                employer_id = None

        carrier_contact_id = None
        if carrier_contact_id_raw:
            try:
                carrier_contact_id = int(carrier_contact_id_raw)
            except ValueError:
                carrier_contact_id = None

        effective_carrier_id = carrier_id if carrier_id is not None else claim.carrier_id
        carrier_contacts = []
        if effective_carrier_id:
            carrier_contacts = (
                Contact.query.filter_by(carrier_id=effective_carrier_id)
                .order_by(Contact.name)
                .all()
            )

        # Handle is_closed (claim status) update
        is_closed_val = request.form.get("is_closed")
        claim_status_val = request.form.get("claim_status")
        closed_raw = is_closed_val if is_closed_val is not None else claim_status_val
        is_closed_bool = None
        if closed_raw is not None and _claim_has_is_closed_column():
            closed_str = str(closed_raw).strip().lower()
            if closed_str in ("1", "true", "yes", "on", "closed"):
                is_closed_bool = True
            elif closed_str in ("0", "false", "no", "off", "open"):
                is_closed_bool = False
            # else leave as None (do not update)

        if not claimant_name or not claim_number:
            error = "Claimant name and claim number are required."
        else:
            claim.claimant_name = claimant_name
            try:
                claim.claimant_first_name = claimant_first_name
                claim.claimant_last_name = claimant_last_name
            except Exception:
                pass

            claim.claim_number = claim_number

            claim.dob = dob
            claim.doi = doi
            claim.surgery_date = surgery_date
            claim.injured_body_part = injured_body_part

            claim.claim_state = claim_state

            claim.claimant_address1 = claimant_address1
            claim.claimant_address2 = claimant_address2
            claim.claimant_city = claimant_city
            claim.claimant_state = claimant_state
            claim.claimant_postal_code = claimant_postal_code
            claim.claimant_phone = claimant_phone
            claim.claimant_email = claimant_email

            claim.carrier_id = carrier_id
            claim.employer_id = employer_id
            claim.carrier_contact_id = carrier_contact_id

            # Set is_closed if present and parsed
            if is_closed_bool is not None and _claim_has_is_closed_column():
                if hasattr(claim, "is_closed"):
                    try:
                        claim.is_closed = is_closed_bool
                    except Exception:
                        pass
                else:
                    try:
                        db.session.execute(
                            text("UPDATE claim SET is_closed = :v WHERE id = :cid"),
                            {"v": bool(is_closed_bool), "cid": claim.id},
                        )
                    except Exception:
                        db.session.rollback()
                        raise

            db.session.commit()
            if provider_ids_raw is not None:
                # If the field exists in the form, treat it as authoritative.
                try:
                    _claim_set_provider_ids(
                        claim.id,
                        [int(x) for x in provider_ids_raw if str(x).strip().isdigit()],
                    )
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                    flash("Could not save treating providers. Check server logs for details.", "error")
            if _claim_surgery_table_name():
                _claim_set_surgeries(claim.id, surgeries_payload)
                db.session.commit()
            return redirect(url_for("main.claim_edit", claim_id=claim.id))

    title = _claim_page_title(claim, "Edit Claim")
    return render_template(
        "claim_edit.html",
        active_page="claims",
        page_title=title,
        claim=claim,
        error=error,
        carriers=carriers,
        employers=employers,
        carrier_contacts=carrier_contacts,
        providers=providers,
        selected_provider_ids=selected_provider_ids,
        claim_surgeries=claim_surgeries,
    )


@bp.route("/claims/<int:claim_id>", methods=["GET", "POST"])
def claim_detail(claim_id: int):
    claim = Claim.query.get_or_404(claim_id)
    settings = _ensure_settings()

    # Handle quick-add Billable Item form (POSTs back to this same page)
    if request.method == "POST":
        # Be tolerant of older/newer template field names
        dos_raw = (
            (request.form.get("date_of_service") or "")
            or (request.form.get("service_date") or "")
            or (request.form.get("date") or "")
        ).strip()
        activity_code = (
            (request.form.get("activity_code") or "")
            or (request.form.get("activity") or "")
            or (request.form.get("code") or "")
        ).strip()
        description = (
            (request.form.get("description") or "")
            or (request.form.get("short_desc") or "")
        ).strip() or None
        qty_raw = (
            (request.form.get("quantity") or "")
            or (request.form.get("qty") or "")
        ).strip()
        notes = (
            (request.form.get("notes") or "")
            or (request.form.get("note") or "")
        ).strip() or None

        if not activity_code:
            flash("Select an activity before adding a billable item.", "error")
            return redirect(url_for("main.claim_detail", claim_id=claim.id))

        dos = _parse_date(dos_raw)
        qty = None
        if qty_raw:
            try:
                qty = float(qty_raw)
            except ValueError:
                flash("Quantity must be a number.", "error")
                return redirect(url_for("main.claim_detail", claim_id=claim.id))

        item = BillableItem(claim_id=claim.id)

        # Set attributes defensively (model field names changed during migrations)
        if hasattr(item, "date_of_service"):
            setattr(item, "date_of_service", dos)
        elif hasattr(item, "service_date"):
            setattr(item, "service_date", dos)

        if hasattr(item, "activity_code"):
            setattr(item, "activity_code", activity_code)
        elif hasattr(item, "activity"):
            setattr(item, "activity", activity_code)

        if hasattr(item, "description"):
            setattr(item, "description", description)
        elif hasattr(item, "short_desc"):
            setattr(item, "short_desc", description)

        if hasattr(item, "quantity"):
            setattr(item, "quantity", qty)
        elif hasattr(item, "qty"):
            setattr(item, "qty", qty)

        if hasattr(item, "notes"):
            setattr(item, "notes", notes)

        db.session.add(item)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash("Could not save billable item. Check required fields and try again.", "error")
            return redirect(url_for("main.claim_detail", claim_id=claim.id))

        flash(f"Billable item added — {claim.claimant_name} ({claim.claim_number})", "success")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    billable_items = (
        BillableItem.query.filter_by(claim_id=claim.id)
        .order_by(
            BillableItem.date_of_service.asc().nullslast(),
            BillableItem.created_at.asc(),
            BillableItem.id.asc(),
        )
        .all()
    )

    reports = (
        Report.query.filter_by(claim_id=claim.id)
        .order_by(Report.created_at.desc())
        .all()
    )

    # -----------------------------------------------------------------
    # Report numbering
    # Initial report should be #1, then count up chronologically across
    # all report types within the claim.
    # We attach the computed value to each report as `display_report_number`
    # so templates can use it without re-implementing math.
    #
    # Sort key preference: DOS start -> created_at -> id
    # -----------------------------------------------------------------

    def _report_sort_key(r: Report):
        dos = getattr(r, "dos_start", None)
        created = getattr(r, "created_at", None)
        rid = getattr(r, "id", 0) or 0
        # Normalize None values so sorting is stable
        return (
            dos or date.min,
            created or datetime.min,
            rid,
        )

    # Build id -> display number map
    report_number_map: dict[int, int] = {}
    if reports:
        chronological = sorted(reports, key=_report_sort_key)
        n = 0
        for r in chronological:
            n += 1
            if getattr(r, "id", None) is not None:
                report_number_map[int(r.id)] = n

        # Attach to the report objects (works for both print/PDF and table display)
        for r in reports:
            rid = getattr(r, "id", None)
            if rid is not None:
                setattr(r, "display_report_number", report_number_map.get(int(rid)))
            else:
                setattr(r, "display_report_number", None)

    documents = (
        ClaimDocument.query.filter_by(claim_id=claim.id)
        .order_by(ClaimDocument.uploaded_at.desc())
        .all()
    )

    invoices = (
        Invoice.query.filter_by(claim_id=claim.id)
        .order_by(
            Invoice.invoice_date.asc().nullslast(),
            Invoice.id.asc(),
        )
        .all()
    )

    def _call_invoice_financials(inv: Invoice):
        """Return a dict of computed invoice totals.

        IMPORTANT:
        - This project may not have an invoice_item table; invoice contents are often
          represented by BillableItem rows with BillableItem.invoice_id = invoice.id.
        - The most reliable calculator for this schema is helpers.compute_invoice_financials
          (used by invoice detail/print). Some helpers-based calculators depend on ORM
          relationships that may not exist.

        Preference order:
          1) helpers.compute_invoice_financials
          2) helpers.calculate_invoice_totals (only if it returns a sane dict)
        """

        # 1) Prefer helpers.compute_invoice_financials (canonical; matches invoice detail/print)
        fn2 = _helpers_compute_invoice_financials
        if callable(fn2):
            # Try a few signatures to avoid tight coupling while the codebase evolves.
            try:
                fin = fn2(invoice=inv, settings=settings, claim=claim)
                if isinstance(fin, dict):
                    return fin
            except TypeError:
                pass
            try:
                fin = fn2(invoice=inv, settings=settings)
                if isinstance(fin, dict):
                    return fin
            except TypeError:
                pass
            try:
                fin = fn2(inv, settings, claim)
                if isinstance(fin, dict):
                    return fin
            except TypeError:
                pass
            try:
                fin = fn2(inv, settings)
                if isinstance(fin, dict):
                    return fin
            except TypeError:
                pass
            try:
                fin = fn2(inv)
                if isinstance(fin, dict):
                    return fin
            except TypeError:
                pass
            except Exception:
                # Never let totals calculation break claim_detail rendering
                pass

        # 2) Fallback: shared helpers calculator (only if it returns a dict)
        fn1 = _helpers_calculate_invoice_totals
        if callable(fn1):
            try:
                fin = fn1(inv)
                if isinstance(fin, dict):
                    return fin
            except Exception:
                pass

        return None

    for _inv in invoices:
        fin = _call_invoice_financials(_inv)
        if not fin or not isinstance(fin, dict):
            continue

        # Prefer canonical key names from helpers (`invoice_total`).
        # Also tolerate older keys to avoid breaking during refactors.
        total_val = None
        for k in ("invoice_total", "total_amount", "total", "grand_total"):
            if k in fin and fin.get(k) is not None:
                total_val = fin.get(k)
                break

        if total_val is not None:
            try:
                _inv.total_amount = float(total_val or 0.0)
            except Exception:
                pass

        # Commonly useful computed fields for templates/AI (non-persisted)
        try:
            setattr(_inv, "computed_financials", fin)
        except Exception:
            pass

    invoice_map = {inv.id: inv for inv in invoices}
    open_invoice_count = sum(
        1 for inv in invoices if (inv.status or "Draft") not in ("Paid", "Void")
    )

    # Build billable activity choices from the BillingActivityCode table.
    activity_rows = (
        BillingActivityCode.query.order_by(
            BillingActivityCode.sort_order,
            BillingActivityCode.code,
        ).all()
    )
    if activity_rows:
        billable_activity_choices = [(r.code, r.label) for r in activity_rows]
    else:
        billable_activity_choices = []

    # ---- claim-level treating providers for display ----
    claim_provider_ids = _claim_load_provider_ids(claim.id)
    claim_providers = []
    if claim_provider_ids:
        claim_providers = Provider.query.filter(Provider.id.in_(claim_provider_ids)).all()
        # Preserve the join-table order
        claim_providers_by_id = {p.id: p for p in claim_providers}
        claim_providers = [claim_providers_by_id.get(pid) for pid in claim_provider_ids if claim_providers_by_id.get(pid)]

    claim_surgeries = _claim_load_surgeries(claim.id)

    title = _claim_page_title(claim, "Claim")
    return render_template(
        "claim_detail.html",
        active_page="claims",
        page_title=title,
        claim=claim,
        settings=settings,
        billable_items=billable_items,
        reports=reports,
        documents=documents,
        invoices=invoices,
        invoice_map=invoice_map,
        open_invoice_count=open_invoice_count,
        billable_activity_choices=billable_activity_choices,
        report_number_map=report_number_map,
        claim_providers=claim_providers,
        claim_surgeries=claim_surgeries,
    )


# ---- claim-scoped AI Q&A endpoint ----

@bp.route("/claims/<int:claim_id>/ai_query", methods=["POST"]) 
def claim_ai_query(claim_id: int):
    """Claim-scoped AI Q&A (single-claim, PHI-safe).

    We DO NOT send claimant name/DOB/address/phone/email, claim number, or any carrier/employer names.
    We provide the model with redacted, claim-scoped text snippets and require source citations.

    Response JSON:
      {"answer": str, "citations": [{"id": str, "label": str}], "is_guess": bool}
    """

    claim = Claim.query.get_or_404(claim_id)
    settings = _ensure_settings()

    payload = request.get_json(silent=True) or {}
    question = (payload.get("question") or "").strip()
    if not question:
        return jsonify({"answer": "", "citations": [], "is_guess": False}), 200

    # AI global/tenant gate
    if not settings or not bool(getattr(settings, "ai_enabled", False)):
        return jsonify({"answer": "AI is disabled in Settings.", "citations": [], "is_guess": False}), 200

    ql = question.lower()

    # -----------------------
    # Build claim-scoped sources (NO direct identifiers)
    # -----------------------
    sources: list[dict] = []

    def _clamp(tv: str, n: int = 6000) -> str:
        tv = (tv or "").strip()
        return tv[:n]

    # Use ai_service redaction rules (best-effort)
    try:
        rules = ai_service.AIPrivacyRules(
            allow_provider_names=bool(getattr(settings, "ai_allow_provider_names", False))
        )

        def _scrub(s: str) -> str:
            return ai_service.scrub_text(s, rules=rules)

    except Exception:

        def _scrub(s: str) -> str:
            return s

    def _add_source(src_id: str, label: str, text_value: str | None):
        tv = (text_value or "").strip()
        if not tv:
            return
        tv = _scrub(tv)
        sources.append({"id": src_id, "label": label, "text": _clamp(tv)})

    # ---- Claim surgeries (claim_surgery_date) ----
    surgeries = _claim_load_surgeries(claim.id)  # list[dict]: surgery_date, description, sort_order
    if surgeries:
        lines: list[str] = []
        for s in surgeries:
            d = s.get("surgery_date")
            desc = (s.get("description") or "").strip()
            d_txt = d.strftime("%m/%d/%Y") if hasattr(d, "strftime") and d else (str(d) if d else "")
            if d_txt and desc:
                lines.append(f"- {d_txt} — {desc}")
            elif d_txt:
                lines.append(f"- {d_txt}")
            elif desc:
                lines.append(f"- {desc}")
        _add_source("CLAIM.surgeries", "Claim – Surgery Date(s)", "\n".join(lines))

    # Deterministic surgery-date answers (no model)
    if ("surgery" in ql) and ("date" in ql or "dates" in ql or "when" in ql):
        if surgeries:
            out: list[str] = []
            for s in surgeries:
                d = s.get("surgery_date")
                desc = (s.get("description") or "").strip()
                d_txt = d.strftime("%m/%d/%Y") if hasattr(d, "strftime") and d else (str(d) if d else "")
                if d_txt and desc:
                    out.append(f"{d_txt} — {desc}")
                elif d_txt:
                    out.append(d_txt)
                elif desc:
                    out.append(desc)
            return jsonify(
                {
                    "answer": "\n".join(out),
                    "citations": [{"id": "CLAIM.surgeries", "label": "Claim – Surgery Date(s)"}],
                    "is_guess": False,
                }
            ), 200

        return jsonify(
            {
                "answer": "No surgery dates recorded on this claim.",
                "citations": [{"id": "CLAIM.surgeries", "label": "Claim – Surgery Date(s)"}] if sources else [],
                "is_guess": False,
            }
        ), 200

    # Reports (chronological)
    reports = (
        Report.query.filter_by(claim_id=claim.id)
        .order_by(Report.created_at.asc(), Report.id.asc())
        .all()
    )
    for r in reports:
        rid = getattr(r, "id", None)
        rtype = (getattr(r, "report_type", None) or "").strip() or "report"
        dos_start = getattr(r, "dos_start", None)
        dos_end = getattr(r, "dos_end", None)
        dos_label = ""
        if dos_start or dos_end:
            dos_label = f" ({dos_start or ''}–{dos_end or ''})"

        _add_source(
            f"R{rid}.status_plan",
            f"Report {rid} {rtype}{dos_label} – status/treatment plan",
            getattr(r, "status_treatment_plan", None),
        )
        _add_source(
            f"R{rid}.work_status",
            f"Report {rid} {rtype}{dos_label} – work status",
            getattr(r, "work_status", None),
        )
        _add_source(
            f"R{rid}.case_mgmt_plan",
            f"Report {rid} {rtype}{dos_label} – case management plan",
            getattr(r, "case_management_plan", None),
        )
        _add_source(
            f"R{rid}.barriers",
            f"Report {rid} {rtype}{dos_label} – barriers",
            getattr(r, "barriers_json", None),
        )

    # Deterministic: status / treatment plan (prefer latest report content)
    if (
        ("status" in ql and ("treatment" in ql or "plan" in ql))
        or ("status/treatment plan" in ql)
        or ("current status" in ql)
    ):
        # Find the most recent report (by created_at/id) with a non-empty status_treatment_plan
        latest_text = ""
        latest_rid = None
        for r in reversed(reports):
            txt = (getattr(r, "status_treatment_plan", None) or "").strip()
            if txt:
                latest_text = txt
                latest_rid = getattr(r, "id", None)
                break

        if latest_text and latest_rid is not None:
            # Keep it short; user can ask follow-ups.
            ans = latest_text.strip()
            if len(ans) > 1200:
                ans = ans[:1200].rstrip() + "…"
            return (
                jsonify(
                    {
                        "answer": ans,
                        "citations": [
                            {"id": f"R{latest_rid}.status_plan", "label": f"Report {latest_rid} – status/treatment plan"}
                        ],
                        "is_guess": False,
                    }
                ),
                200,
            )

        return (
            jsonify(
                {
                    "answer": "I cannot find a status/treatment plan in the reports for this claim.",
                    "citations": [],
                    "is_guess": False,
                }
            ),
            200,
        )

    # Billables (include qty + units so AI can answer hours / dollars / miles questions)
    billables = (
        BillableItem.query.filter_by(claim_id=claim.id)
        .order_by(BillableItem.id.asc())
        .all()
    )
    for b in billables:
        bid = getattr(b, "id", None)
        dos = getattr(b, "date_of_service", None) or getattr(b, "service_date", None)
        act_raw = getattr(b, "activity_code", None) or getattr(b, "activity", None) or ""
        act = str(act_raw).strip()
        act_u = act.upper() if act else ""

        qty = getattr(b, "quantity", None)
        desc = getattr(b, "description", None) or getattr(b, "short_desc", None)
        notes = getattr(b, "notes", None)

        # Interpret units by activity code
        # MIL = miles, EXP = dollars, everything else = hours
        if act_u == "MIL":
            units = "miles"
            meaning = "mileage"
        elif act_u == "EXP":
            units = "dollars"
            meaning = "expense"
        else:
            units = "hours"
            meaning = "time"

        parts: list[str] = []
        if dos:
            parts.append(f"Date: {dos}")
        if act_u:
            parts.append(f"Activity: {act_u}")

        if qty is not None:
            try:
                qf = float(qty)
                if units == "dollars":
                    parts.append(f"Quantity: {qf:.2f} {units} ({meaning})")
                elif units == "miles":
                    parts.append(f"Quantity: {qf:.1f} {units} ({meaning})")
                else:
                    parts.append(f"Quantity: {qf:.2f} {units} ({meaning})")
            except Exception:
                parts.append(f"Quantity: {qty} {units} ({meaning})")

        if desc:
            parts.append(f"Description: {desc}")
        if notes:
            parts.append(f"Notes: {notes}")

        line = " | ".join(parts)
        _add_source(f"B{bid}", f"Billable {bid}", line)

    # Invoices (non-PHI)
    invoices = Invoice.query.filter_by(claim_id=claim.id).order_by(Invoice.id.asc()).all()
    for inv in invoices:
        iid = getattr(inv, "id", None)
        inv_no = getattr(inv, "invoice_number", None)
        status = getattr(inv, "status", None)
        inv_date = getattr(inv, "invoice_date", None)
        total = getattr(inv, "total_amount", None)

        line = f"Invoice: {inv_no or iid}"
        if inv_date:
            line += f" Date: {inv_date}"
        if status:
            line += f" Status: {status}"
        if total is not None:
            line += f" Total: {total}"

        _add_source(f"I{iid}", f"Invoice {inv_no or iid}", line)

    # Claim-level neutral facts (avoid claim_number)
    _add_source("C.doi", "Claim – date of injury (DOI)", str(getattr(claim, "doi", "") or ""))
    _add_source("C.body_part", "Claim – injured body part", str(getattr(claim, "injured_body_part", "") or ""))
    _add_source("C.state", "Claim – claim state", str(getattr(claim, "claim_state", "") or ""))
    if _claim_has_is_closed_column():
        is_closed = bool(getattr(claim, "is_closed", False))
        _add_source("C.open_closed", "Claim – open/closed", "Closed" if is_closed else "Open")

    # Nothing to search
    if not sources:
        return (
            jsonify(
                {
                    "answer": "No claim content to search yet (no reports/billables/invoices).",
                    "citations": [],
                    "is_guess": False,
                }
            ),
            200,
        )

    # -----------------------
    # Build prompt + call model (reuse ai_service call)
    # -----------------------
    system_rules = (
        "You are a clinical case-management assistant for an internal tool.\n"
        "RULES:\n"
        "- DO NOT invent facts. If not found, say you cannot find it.\n"
        "- Answer using ONLY the provided SOURCES.\n"
        "- If you infer, mark it explicitly as a guess and keep it brief.\n"
        "- Return STRICT JSON ONLY with keys: answer (string), citations (array of source ids), is_guess (boolean).\n"
        "- Citations must be source ids from the SOURCES list.\n"
        "- Do not include any names or identifiers not present in SOURCES.\n"
    )

    sources_block_lines: list[str] = []
    for s in sources:
        sources_block_lines.append(f"[{s['id']}] {s['label']}\n{s['text']}")
    sources_block = "\n\n".join(sources_block_lines)

    prompt = f"{system_rules}\n\nQUESTION:\n{question}\n\nSOURCES:\n{sources_block}\n"

    try:
        raw = ai_service.call_llm(prompt)
    except Exception:
        current_app.logger.exception("Claim AI query failed")
        return jsonify({"answer": "AI request failed. Check server logs.", "citations": [], "is_guess": False}), 200

    # -----------------------
    # Parse JSON result (best-effort)
    # -----------------------
    answer = ""
    citations: list[dict] = []
    is_guess = False

    try:
        import json
        import re

        raw_text = raw if isinstance(raw, str) else ""
        # Tolerate ```json ... ``` wrappers
        m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw_text, flags=re.S)
        if m:
            raw_text = m.group(1)

        data = json.loads(raw_text) if raw_text else {}
        answer = str(data.get("answer") or "").strip()
        is_guess = bool(data.get("is_guess", False))

        cited_ids = data.get("citations") or []
        if isinstance(cited_ids, str):
            cited_ids = [cited_ids]

        if isinstance(cited_ids, list):
            label_by_id = {s["id"]: s["label"] for s in sources}
            for cid in cited_ids:
                cid_s = str(cid)
                if cid_s in label_by_id:
                    citations.append({"id": cid_s, "label": label_by_id[cid_s]})

    except Exception:
        answer = (str(raw) if raw is not None else "").strip()
        is_guess = True
        citations = []

    return jsonify({"answer": answer, "citations": citations, "is_guess": is_guess}), 200


@bp.route("/claims/<int:claim_id>/delete", methods=["GET", "POST"])
def claim_delete(claim_id: int):
    """Two-step delete for a claim: confirm on GET, actually delete on POST."""

    claim = Claim.query.get_or_404(claim_id)

    if request.method == "POST":
        try:
            # --- Delete children in a FK-safe order ---

            # Collect report IDs for this claim
            report_ids = [rid for (rid,) in (
                db.session.query(Report.id).filter_by(claim_id=claim.id).all()
            )]

            if report_ids:
                # Report join tables / children that reference report.id
                if _table_exists("report_approved_provider"):
                    db.session.execute(
                        text("DELETE FROM report_approved_provider WHERE report_id IN :report_ids")
                        .bindparams(bindparam("report_ids", expanding=True)),
                        {"report_ids": report_ids},
                    )

                # Report-level documents (route name: report_document_*)
                if _table_exists("report_document"):
                    db.session.execute(
                        text("DELETE FROM report_document WHERE report_id IN :report_ids")
                        .bindparams(bindparam("report_ids", expanding=True)),
                        {"report_ids": report_ids},
                    )

            # Collect invoice IDs for this claim
            invoice_ids = [iid for (iid,) in (
                db.session.query(Invoice.id).filter_by(claim_id=claim.id).all()
            )]

            if invoice_ids:
                # Invoice items table (used by invoice add/remove item routes)
                if _table_exists("invoice_item"):
                    db.session.execute(
                        text("DELETE FROM invoice_item WHERE invoice_id IN :invoice_ids")
                        .bindparams(bindparam("invoice_ids", expanding=True)),
                        {"invoice_ids": invoice_ids},
                    )

            # Claim-level children
            BillableItem.query.filter_by(claim_id=claim.id).delete(synchronize_session=False)
            ClaimDocument.query.filter_by(claim_id=claim.id).delete(synchronize_session=False)

            # Reports (after report-level cleanup)
            Report.query.filter_by(claim_id=claim.id).delete(synchronize_session=False)

            # Invoices (after invoice_item cleanup)
            Invoice.query.filter_by(claim_id=claim.id).delete(synchronize_session=False)

            # Finally the claim
            db.session.delete(claim)
            db.session.commit()

            flash("Claim deleted.", "success")
            return redirect(url_for("main.claims_list"))

        except IntegrityError:
            db.session.rollback()
            flash(
                "Could not delete claim because related records still exist (FK constraint). "
                "See server logs for details.",
                "error",
            )
            return redirect(url_for("main.claim_delete", claim_id=claim.id))

    return render_template(
        "claim_delete_confirm.html",
        active_page="claims",
        claim=claim,
    )


@bp.route("/claims/<int:claim_id>/reports/new", methods=["POST"])
def report_new_from_claim(claim_id: int):
    """Create a new Report from the Claim Detail page.

    This is the backend for the report-type dropdown + “New Report” button.
    It must:
      - create the report with sensible default DOS dates
      - carry forward barriers from the most recent prior report
      - redirect to the report edit screen
    """

    claim = Claim.query.get_or_404(claim_id)

    report_type = (request.form.get("report_type") or request.form.get("type") or "").strip().lower()
    if report_type not in ("initial", "progress", "closure"):
        flash("Select a report type before creating a new report.", "error")
        return redirect(url_for("main.claim_detail", claim_id=claim.id))

    today_value = today()

    # Find the most recent report for DOS defaults and carry-forward
    last = (
        Report.query.filter_by(claim_id=claim.id)
        .order_by(Report.created_at.desc())
        .first()
    )

    # DOS defaults
    if report_type == "initial":
        dos_start = claim.referral_date or today_value
        dos_end = today_value
    else:
        # progress/closure: start = day after last DOS end (fallback to today)
        if last and last.dos_end:
            dos_start = last.dos_end + timedelta(days=1)  # type: ignore[attr-defined]
        else:
            dos_start = today_value
        dos_end = today_value

    # Create
    rpt = Report(
        claim_id=claim.id,
        report_type=report_type,
        created_at=now(),
        updated_at=now(),
        referral_date=claim.referral_date,
        dos_start=dos_start,
        dos_end=dos_end,
    )

    db.session.add(rpt)
    db.session.flush()  # ensures rpt.id exists for join-table inserts

    _apply_report_carry_forward(claim_id=claim.id, new_report=rpt)

    db.session.commit()

    # If closure is created, mark claim closed (can be reopened elsewhere)
    if report_type == "closure" and _claim_has_is_closed_column():
        # Use SQL UPDATE so this works even if the ORM model hasn't been updated yet.
        try:
            db.session.execute(
                text("UPDATE claim SET is_closed = TRUE WHERE id = :cid"),
                {"cid": claim.id},
            )
            db.session.commit()
        except Exception:
            db.session.rollback()

    return redirect(url_for("main.report_edit", claim_id=claim.id, report_id=rpt.id))