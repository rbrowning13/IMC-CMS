

"""Shared route helpers.

This module exists to keep route modules small and avoid duplicating common
logic across claims/reports/invoices/settings/documents.

Intentionally **no Blueprint routes** should live here.
"""

from __future__ import annotations

import os
import re
import subprocess
import random
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple


from flask import current_app
from markupsafe import Markup, escape

# Public exports used by route modules and templates
__all__ = [
    "parse_mmddyyyy",
    "_parse_mmddyyyy",
    "parse_iso_or_mmddyyyy",
    "_parse_date",
    "parse_date",
    "validate_email",
    "_validate_email",
    "validate_phone",
    "_validate_phone",
    "validate_postal_code",
    "_validate_postal_code",
    "safe_filename",
    "documents_root",
    "_documents_root",
    "get_claim_folder",
    "_get_claim_folder",
    "get_report_folder",
    "_get_report_folder",
    "_ensure_settings",
    "BILLABLE_ACTIVITY_CHOICES",
    "_billable_is_complete",
    "generate_invoice_number",
    "_generate_invoice_number",
    "calculate_invoice_totals",
    "compute_invoice_financials",
    "_calculate_invoice_totals",
    "STATE_CHOICES",
    "STATE_CODE_TO_NAME",
    "state_options",
    "_state_options",
    "open_folder_in_file_manager",
    "shutil_which",
    "build_basic_ics",
    "_coerce_float",
]

# Canonical US state list used across the app.
# Keep this in one place so templates/routes never drift.
STATE_CHOICES: list[tuple[str, str]] = [
    ("AL", "Alabama"),
    ("AK", "Alaska"),
    ("AZ", "Arizona"),
    ("AR", "Arkansas"),
    ("CA", "California"),
    ("CO", "Colorado"),
    ("CT", "Connecticut"),
    ("DE", "Delaware"),
    ("DC", "District of Columbia"),
    ("FL", "Florida"),
    ("GA", "Georgia"),
    ("HI", "Hawaii"),
    ("ID", "Idaho"),
    ("IL", "Illinois"),
    ("IN", "Indiana"),
    ("IA", "Iowa"),
    ("KS", "Kansas"),
    ("KY", "Kentucky"),
    ("LA", "Louisiana"),
    ("ME", "Maine"),
    ("MD", "Maryland"),
    ("MA", "Massachusetts"),
    ("MI", "Michigan"),
    ("MN", "Minnesota"),
    ("MS", "Mississippi"),
    ("MO", "Missouri"),
    ("MT", "Montana"),
    ("NE", "Nebraska"),
    ("NV", "Nevada"),
    ("NH", "New Hampshire"),
    ("NJ", "New Jersey"),
    ("NM", "New Mexico"),
    ("NY", "New York"),
    ("NC", "North Carolina"),
    ("ND", "North Dakota"),
    ("OH", "Ohio"),
    ("OK", "Oklahoma"),
    ("OR", "Oregon"),
    ("PA", "Pennsylvania"),
    ("RI", "Rhode Island"),
    ("SC", "South Carolina"),
    ("SD", "South Dakota"),
    ("TN", "Tennessee"),
    ("TX", "Texas"),
    ("UT", "Utah"),
    ("VT", "Vermont"),
    ("VA", "Virginia"),
    ("WA", "Washington"),
    ("WV", "West Virginia"),
    ("WI", "Wisconsin"),
    ("WY", "Wyoming"),
]

STATE_CODE_TO_NAME: dict[str, str] = {code: name for code, name in STATE_CHOICES}

# -----------------------------------------------------------------------------
# Legacy/back-compat names
# -----------------------------------------------------------------------------
# The app is being refactored from a single mega routes.py into modules. Some
# modules still import helper names with leading underscores from the legacy
# file. Keep these helpers here (with both public and legacy names) so route
# modules can stay small and imports stay stable.


# -----------------------------------------------------------------------------
# Dates
# -----------------------------------------------------------------------------

def parse_mmddyyyy(value: str, field_label: str = "Date") -> Tuple[Optional[date], Optional[str]]:
    """Parse MM/DD/YYYY into a date.

    Returns: (parsed_date_or_None, error_message_or_None)
    """
    raw = (value or "").strip()
    if not raw:
        return None, None
    try:
        return datetime.strptime(raw, "%m/%d/%Y").date(), None
    except ValueError:
        return None, f"{field_label} must be in MM/DD/YYYY format."


def _parse_mmddyyyy(value: str, field_label: str = "Date") -> Tuple[Optional[date], Optional[str]]:
    """Legacy name for parse_mmddyyyy."""
    return parse_mmddyyyy(value, field_label=field_label)



def parse_iso_or_mmddyyyy(value: str) -> Optional[date]:
    """Parse either YYYY-MM-DD or MM/DD/YYYY into a date. Returns None if invalid/blank."""
    raw = (value or "").strip()
    if not raw:
        return None

    # ISO first (flatpickr and HTML date inputs often send ISO)
    if "-" in raw:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            pass

    try:
        return datetime.strptime(raw, "%m/%d/%Y").date()
    except ValueError:
        return None


# Added legacy and public date parsing helpers
def _parse_date(value: str) -> Optional[date]:
    """Legacy date parser used throughout the original monolithic routes.py.

    Accepts either YYYY-MM-DD (HTML/Flatpickr) or MM/DD/YYYY (typed).
    Returns None for blank/invalid values.
    """
    return parse_iso_or_mmddyyyy(value)


def parse_date(value: str) -> Optional[date]:
    """Public alias for legacy _parse_date."""
    return _parse_date(value)


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------

_email_re = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def validate_email(value: str) -> bool:
    """Return True if value looks like a valid email address.

    This is intentionally lightweight (single-user local app). It matches the
    legacy behavior: empty strings are treated as valid ("not provided").
    """
    raw = (value or "").strip()
    if not raw:
        return True
    return bool(_email_re.match(raw))


def _validate_email(value: str) -> bool:
    """Legacy alias for validate_email."""
    return validate_email(value)


_phone_digits_re = re.compile(r"\D+")


def validate_phone(value: str) -> bool:
    """Return True if value looks like a US phone number (10 digits), optionally with extension.

    Empty strings are treated as valid.
    """
    raw = (value or "").strip()
    if not raw:
        return True

    # Allow 'x123' or 'ext 123' as an extension suffix.
    main = re.split(r"\b(?:ext\.?|x)\b", raw, flags=re.IGNORECASE)[0]
    digits = _phone_digits_re.sub("", main)
    return len(digits) == 10


def _validate_phone(value: str) -> bool:
    """Legacy alias for validate_phone."""
    return validate_phone(value)


_postal_re = re.compile(r"^\d{5}(?:-\d{4})?$")


def validate_postal_code(value: str) -> bool:
    """Return True if value is a US ZIP or ZIP+4. Empty strings are valid."""
    raw = (value or "").strip()
    if not raw:
        return True
    return bool(_postal_re.match(raw))


def _validate_postal_code(value: str) -> bool:
    """Legacy alias for validate_postal_code."""
    return validate_postal_code(value)

# -----------------------------------------------------------------------------
# Filenames / paths
# -----------------------------------------------------------------------------

_filename_strip_re = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str, fallback: str = "file") -> str:
    """Create a filesystem-safe filename (simple, cross-platform).

    Keeps letters, numbers, dot, underscore, dash. Collapses the rest to '_'.
    """
    raw = (name or "").strip()
    if not raw:
        return fallback

    # Normalize spaces and weird chars.
    cleaned = _filename_strip_re.sub("_", raw)
    cleaned = cleaned.strip("._ ")
    return cleaned or fallback


def documents_root() -> Path:
    """Return the configured documents root as a Path.

    Prefers app config key `DOCUMENTS_ROOT`.
    Falls back to `instance_path / documents`.
    """
    root = current_app.config.get("DOCUMENTS_ROOT")
    if root:
        return Path(root)
    return Path(current_app.instance_path) / "documents"


def _documents_root() -> Path:
    """Legacy name for documents_root."""
    return documents_root()



def _get_claim_folder(claim) -> Path:
    """Return the on-disk folder for a claim and ensure it exists.

    Expected claim attrs: id, claim_number (optional).
    """
    root = documents_root()
    root.mkdir(parents=True, exist_ok=True)

    claim_num = getattr(claim, "claim_number", None) or f"claim_{getattr(claim, 'id', 'unknown')}"
    claim_num = safe_filename(str(claim_num), fallback=f"claim_{getattr(claim, 'id', 'unknown')}")

    folder = root / claim_num
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def get_claim_folder(claim) -> Path:
    """Public alias for legacy _get_claim_folder."""
    return _get_claim_folder(claim)


def _get_report_folder(report) -> Path:
    """Return the on-disk folder for a report (under its claim) and ensure it exists."""
    claim = getattr(report, "claim", None)
    if claim is None:
        raise ValueError("report.claim is required to resolve report folder")

    claim_folder = _get_claim_folder(claim)
    report_root = claim_folder / "reports"
    report_root.mkdir(parents=True, exist_ok=True)
    return report_root


def get_report_folder(report) -> Path:
    """Public alias for legacy _get_report_folder."""
    return _get_report_folder(report)


# -----------------------------------------------------------------------------
# Settings (shared singleton)
# -----------------------------------------------------------------------------

def _ensure_settings():
    """Return the singleton Settings row; create it if missing.

    Kept here so route modules can import it without circular imports.
    """
    # Local imports to avoid circulars at app import time.
    from app.extensions import db
    from app.models import Settings

    settings = Settings.query.first()
    if not settings:
        settings = Settings()
        db.session.add(settings)
        db.session.commit()
    return settings


# -----------------------------------------------------------------------------
# Billables helpers/constants
# -----------------------------------------------------------------------------

# Fallback choices used when the BillingActivityCode table is empty.
# This mirrors the legacy constant from the original monolithic routes.py.
BILLABLE_ACTIVITY_CHOICES = [
    ("Admin", "Admin"),
    ("Email", "Email"),
    ("Exp", "Exp"),
    ("Fax", "Fax"),
    ("FR", "FR"),
    ("GDL", "GDL"),
    ("LTR", "LTR"),
    ("MR", "MR"),
    ("MTG", "MTG"),
    ("MIL", "MIL"),
    ("REP", "REP"),
    ("RR", "RR"),
    ("TC", "TC"),
    ("TCM", "TCM"),
    ("Text", "Text"),
    ("Travel", "Travel"),
    ("Wait", "Wait"),
    ("NO BILL", "NO BILL"),
]


def _billable_is_complete(activity_code: str, service_date: Optional[date], quantity: Optional[float]) -> bool:
    """Legacy completeness rules.

    - For "NO BILL": requires either date OR quantity.
    - For all others: requires BOTH date AND quantity.
    """
    code = (activity_code or "").strip().upper()
    if code == "NO BILL":
        return bool(service_date or quantity)
    return bool(service_date and quantity)



#
# -----------------------------------------------------------------------------
# Invoice helpers
# -----------------------------------------------------------------------------

# NOTE:
# All invoice/billing/payment math MUST go through compute_invoice_financials().
# Routes and templates should never re-calculate rates, subtotals, or balances.

def _generate_invoice_number(prefix: str = "INV") -> str:
    """Generate a global, per-year sequential invoice number.

    Format:
        INV-YY-###

    Where:
        YY  = 2-digit year
        ### = zero-padded global sequence for that year (001, 002, ...)

    This is system-wide (not per-claim).
    """

    from app.extensions import db
    from app.models import Invoice

    today = date.today()
    year_short = today.strftime("%y")
    year_prefix = f"{prefix}-{year_short}-"

    # Find all invoices for this year by prefix match
    existing = (
        db.session.query(Invoice)
        .filter(Invoice.invoice_number.like(f"{year_prefix}%"))
        .all()
    )

    max_seq = 0
    for inv in existing:
        try:
            parts = inv.invoice_number.split("-")
            if len(parts) >= 3 and parts[1] == year_short:
                seq = int(parts[2])
                if seq > max_seq:
                    max_seq = seq
        except Exception:
            continue

    next_seq = max_seq + 1
    return f"{year_prefix}{next_seq:03d}"


def _iter_invoice_items(invoice) -> Iterable[Any]:
    """Return an iterable of items linked to an invoice (best-effort)."""
    for attr in ("billable_items", "items", "invoice_items"):
        if hasattr(invoice, attr):
            val = getattr(invoice, attr)
            # SQLAlchemy relationship collections act iterable.
            if val is not None:
                return val
    return []


def _calculate_invoice_totals(invoice):
    """Calculate and persist invoice totals (best-effort).

    This mirrors the legacy behavior: totals are stored on the Invoice record so
    they remain stable even if settings/rates change later.

    Returns a dict: {subtotal, mileage_total, total}
    """
    settings = None
    try:
        settings = _ensure_settings()
    except Exception:
        settings = None

    default_rate = getattr(settings, "billing_rate", None) or getattr(settings, "hourly_rate", None) or 0
    mileage_rate = getattr(settings, "mileage_rate", None) or 0

    subtotal = 0.0
    mileage_total = 0.0

    for item in _iter_invoice_items(invoice):
        # Standard fields used around the app.
        code = (getattr(item, "activity_code", None) or "").strip().upper()
        qty = getattr(item, "quantity", None)
        try:
            qty_f = float(qty) if qty is not None else None
        except (TypeError, ValueError):
            qty_f = None

        # If an explicit amount exists, trust it.
        amt = getattr(item, "amount", None)
        if amt is not None:
            try:
                amt_f = float(amt)
            except (TypeError, ValueError):
                amt_f = 0.0
            if code == "MIL":
                mileage_total += amt_f
            else:
                subtotal += amt_f
            continue

        # Otherwise compute from qty * rate.
        if qty_f is None:
            continue

        # Try per-item rate first, then settings defaults.
        rate = (
            getattr(item, "rate", None)
            or getattr(item, "unit_rate", None)
            or getattr(item, "billing_rate", None)
        )
        try:
            rate_f = float(rate) if rate is not None else None
        except (TypeError, ValueError):
            rate_f = None

        if code == "MIL":
            use_rate = rate_f if rate_f is not None else float(mileage_rate or 0)
            mileage_total += qty_f * use_rate
        else:
            use_rate = rate_f if rate_f is not None else float(default_rate or 0)
            subtotal += qty_f * use_rate

    total = float(subtotal) + float(mileage_total)

    # Persist onto invoice if those columns exist.
    for field, value in (
        ("subtotal", subtotal),
        ("subtotal_amount", subtotal),
        ("mileage_total", mileage_total),
        ("mileage_amount", mileage_total),
        ("total", total),
        ("total_amount", total),
    ):
        if hasattr(invoice, field):
            try:
                setattr(invoice, field, round(float(value), 2))
            except Exception:
                pass

    return {"subtotal": round(float(subtotal), 2), "mileage_total": round(float(mileage_total), 2), "total": round(float(total), 2)}


# New: canonical invoice financials computation (for use in detail/print/PDF)
def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


#
# IMPORTANT: Carrier rates ALWAYS override settings when present.
# This function must be used everywhere invoice math is performed.
def _pick_rate_and_source(
    carrier: Any,
    settings: Any,
    carrier_attr_candidates: list[str],
    settings_attr_candidates: list[str],
    fallback: float = 0.0,
) -> tuple[float, str, bool]:
    """Pick a rate from carrier first (if present), else from settings.

    Returns (rate, source_label, present_flag) where:
      - source_label is 'Carrier', 'Default', or 'None'
      - present_flag is True if a value was explicitly present (even if it was 0)

    This lets the app distinguish between "missing" and "explicitly set to 0.00".
    """
    # Carrier override
    if carrier is not None:
        for attr in carrier_attr_candidates:
            if hasattr(carrier, attr):
                v = getattr(carrier, attr)
                # Treat None/blank as missing; 0 is a valid explicit value.
                if v is not None and str(v).strip() != "":
                    return _coerce_float(v, fallback), "Carrier", True

    # Settings default
    if settings is not None:
        for attr in settings_attr_candidates:
            if hasattr(settings, attr):
                v = getattr(settings, attr)
                if v is not None and str(v).strip() != "":
                    return _coerce_float(v, fallback), "Default", True

    return float(fallback), "None", False
#
# Canonical activity-code buckets for invoice rollups.
# Keep these centralized so invoice detail/print/payment/billing never drift.
INVOICE_MILEAGE_CODES = {"MIL", "MILE", "MILEAGE"}
INVOICE_EXPENSE_CODES = {"EXP", "EX", "EXPENSE", "EXPENSES"}
INVOICE_TELEPHONIC_CODES = {"TC", "TCM", "TEL", "PHONE", "TELE", "TELEPHONIC"}


def compute_invoice_financials(
    *,
    invoice: Any,
    claim: Any | None = None,
    items: Iterable[Any] | None = None,
    payments: Iterable[Any] | None = None,
    settings: Any | None = None,
) -> dict[str, Any]:
    """Canonical invoice math used by detail/print/PDF.

    Goal: one source of truth so totals don't drift across routes/templates.

    Inputs are flexible so callers can pass pre-fetched relationships.

    Returns a dict with:
      - hours_total, telephonic_hours_total, miles_total, expenses_total
      - hourly_rate, telephonic_rate, mileage_rate
      - hourly_rate_source, telephonic_rate_source, mileage_rate_source
      - hourly_subtotal, telephonic_subtotal, mileage_subtotal, expenses_subtotal
      - invoice_total (pre-payments)
      - paid_total
      - balance_due
      - rates_used_rows (for the UI table)
    """
    # Resolve claim/carrier
    resolved_claim = claim
    if resolved_claim is None and hasattr(invoice, "claim"):
        resolved_claim = getattr(invoice, "claim")

    carrier = None

    # Prefer claim.carrier if available
    if resolved_claim is not None and hasattr(resolved_claim, "carrier"):
        carrier = getattr(resolved_claim, "carrier")

    # Fallback: invoice may directly reference a carrier
    if carrier is None and hasattr(invoice, "carrier"):
        try:
            carrier = getattr(invoice, "carrier")
        except Exception:
            carrier = None

    # Final fallback: look up carrier by FK if present
    if carrier is None and hasattr(invoice, "carrier_id"):
        try:
            carrier_id = getattr(invoice, "carrier_id")
        except Exception:
            carrier_id = None
        if carrier_id:
            try:
                from app.models import Carrier  # local import to avoid circulars
                carrier = Carrier.query.get(int(carrier_id))
            except Exception:
                carrier = None

    # Resolve settings if not provided
    if settings is None:
        try:
            settings = _ensure_settings()
        except Exception:
            settings = None

    # Items
    if items is None:
        items = _iter_invoice_items(invoice)

    # Payments
    if payments is None:
        if hasattr(invoice, "payments") and getattr(invoice, "payments") is not None:
            payments = getattr(invoice, "payments")
        else:
            payments = []

    # Pick rates (carrier overrides settings)
    hourly_rate, hourly_src, hourly_present = _pick_rate_and_source(
        carrier,
        settings,
        carrier_attr_candidates=["hourly_rate", "billing_rate", "rate_hourly"],
        settings_attr_candidates=["hourly_rate", "billing_rate", "rate_hourly"],
        fallback=0.0,
    )

    tele_rate, tele_src, tele_present = _pick_rate_and_source(
        carrier,
        settings,
        carrier_attr_candidates=["telephonic_rate", "phone_rate", "rate_telephonic"],
        settings_attr_candidates=["telephonic_rate", "phone_rate", "rate_telephonic"],
        fallback=hourly_rate,
    )

    mileage_rate, mileage_src, mileage_present = _pick_rate_and_source(
        carrier,
        settings,
        carrier_attr_candidates=["mileage_rate", "rate_mileage"],
        settings_attr_candidates=["mileage_rate", "rate_mileage"],
        fallback=0.0,
    )

    # Roll up quantities
    hours_total = 0.0
    tele_hours_total = 0.0
    miles_total = 0.0
    expenses_total = 0.0

    for item in items or []:
        code = (getattr(item, "activity_code", None) or "").strip().upper()

        qty_raw = getattr(item, "quantity", None)
        qty = None
        try:
            qty = float(qty_raw) if qty_raw is not None else None
        except (TypeError, ValueError):
            qty = None

        if qty is None:
            continue

        # Mileage
        if code in INVOICE_MILEAGE_CODES:
            miles_total += qty
            continue

        # Expenses (treat quantity as dollars)
        if code in INVOICE_EXPENSE_CODES:
            expenses_total += qty
            continue

        # Telephonic claim billing is handled at the CLAIM level (not per-code).
        # For now, telephonic codes are billed as standard hourly time.
        if code in INVOICE_TELEPHONIC_CODES:
            hours_total += qty
            continue

        # Default: billable hours
        hours_total += qty

    # Subtotals
    hourly_subtotal = round(hours_total * hourly_rate, 2)
    tele_subtotal = 0.0
    mileage_subtotal = round(miles_total * mileage_rate, 2)
    expenses_subtotal = round(expenses_total, 2)

    computed_total = round(hourly_subtotal + tele_subtotal + mileage_subtotal + expenses_subtotal, 2)

    # Canonical rule for UI math: always use computed totals so every screen agrees.
    invoice_total = round(computed_total, 2)

    # Payments totals
    paid_total = 0.0
    for p in payments or []:
        amt = getattr(p, "amount", None)
        paid_total += _coerce_float(amt, 0.0)
    paid_total = round(paid_total, 2)

    balance_due = round(max(invoice_total - paid_total, 0.0), 2)

    rates_used_rows = [
        {
            "label": "Hourly",
            "rate": hourly_rate,
            "source": hourly_src,
            "subtotal": hourly_subtotal,
        },
        {
            "label": "Mileage",
            "rate": mileage_rate,
            "source": mileage_src,
            "subtotal": mileage_subtotal,
        },
    ]

    return {
        "hours_total": round(hours_total, 2),
        "telephonic_hours_total": round(tele_hours_total, 2),
        "miles_total": round(miles_total, 2),
        "expenses_total": round(expenses_total, 2),
        "hourly_rate": round(hourly_rate, 4),
        "telephonic_rate": round(tele_rate, 4),
        "mileage_rate": round(mileage_rate, 6),
        "hourly_rate_source": hourly_src,
        "telephonic_rate_source": tele_src,
        "mileage_rate_source": mileage_src,
        # Back-compat / template-friendly names
        "effective_hourly_rate": round(hourly_rate, 4),
        "effective_telephonic_rate": round(tele_rate, 4),
        "effective_mileage_rate": round(mileage_rate, 6),
        "hourly_source": hourly_src,
        "telephonic_source": tele_src,
        "mileage_source": mileage_src,
        "carrier_overrides": (hourly_src == "Carrier") or (tele_src == "Carrier") or (mileage_src == "Carrier"),
        "missing_rates": [
            name
            for name, present in (
                ("hourly", hourly_present),
                ("telephonic", tele_present),
                ("mileage", mileage_present),
            )
            if not present
        ],
        "hourly_subtotal": hourly_subtotal,
        "telephonic_subtotal": tele_subtotal,
        "mileage_subtotal": mileage_subtotal,
        "expenses_subtotal": expenses_subtotal,
        "invoice_total": invoice_total,
        "paid_total": paid_total,
        "balance_due": balance_due,
        "rates_used_rows": rates_used_rows,
    }


# Back-compat public aliases (some modules may import non-underscored names)

def generate_invoice_number(prefix: str = "INV") -> str:
    return _generate_invoice_number(prefix=prefix)


def calculate_invoice_totals(invoice):
    return _calculate_invoice_totals(invoice)


# -----------------------------------------------------------------------------
# Jinja helpers
# -----------------------------------------------------------------------------

def state_options(selected: str | None = None, default: str = "ID") -> Markup:
    """Return HTML <option> tags for US state dropdowns.

    Templates call this like: {{ state_options(model.state) }}.
    If `selected` is falsy, `default` is selected.
    """
    selected_code = (selected or default or "").strip().upper()

    states = STATE_CHOICES

    parts: list[str] = []
    for code, name in states:
        sel = " selected" if code == selected_code else ""
        parts.append(f'<option value="{escape(code)}"{sel}>{escape(name)}</option>')

    return Markup("\n".join(parts))

def _state_options(selected: str | None = None, default: str = "ID") -> Markup:
    """Legacy alias for state_options."""
    return state_options(selected=selected, default=default)

# -----------------------------------------------------------------------------
# OS helpers
# -----------------------------------------------------------------------------

def open_folder_in_file_manager(path: Path) -> bool:
    """Attempt to open a folder in the OS file manager.

    Returns True if a command was launched; False otherwise.
    """
    try:
        p = Path(path)
        if not p.exists():
            return False

        # macOS
        if os.name == "posix" and subprocess.call(["uname"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
            # If this is macOS, `open` exists.
            if shutil_which("open"):
                subprocess.Popen(["open", str(p)])
                return True

        # Windows
        if os.name == "nt":
            os.startfile(str(p))  # type: ignore[attr-defined]
            return True

        # Linux / other POSIX
        if shutil_which("xdg-open"):
            subprocess.Popen(["xdg-open", str(p)])
            return True

    except Exception:
        return False

    return False


def shutil_which(cmd: str) -> Optional[str]:
    """Tiny local 'which' to avoid importing shutil everywhere."""
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(folder) / cmd
        if os.name == "nt":
            # On Windows try common extensions.
            for ext in ("", ".exe", ".bat", ".cmd"):
                if (Path(str(candidate) + ext)).exists():
                    return str(Path(str(candidate) + ext))
        else:
            if candidate.exists() and os.access(str(candidate), os.X_OK):
                return str(candidate)
    return None


# -----------------------------------------------------------------------------
# ICS
# -----------------------------------------------------------------------------

def build_basic_ics(
    *,
    title: str,
    start_dt: datetime,
    end_dt: datetime,
    description: str = "",
    location: str = "",
    uid: Optional[str] = None,
) -> str:
    """Build a simple RFC5545-ish ICS payload (enough for Apple/Google Calendar)."""

    def _fmt(dt: datetime) -> str:
        # floating local time (no TZ) keeps it simple for single-user local installs
        return dt.strftime("%Y%m%dT%H%M%S")

    uid_val = uid or f"{_fmt(datetime.utcnow())}-{os.getpid()}@impact-cms"

    # Escape commas/semicolons/newlines per spec-ish rules
    def _esc(s: str) -> str:
        s = (s or "")
        s = s.replace("\\", "\\\\")
        s = s.replace(";", "\\;")
        s = s.replace(",", "\\,")
        s = s.replace("\r\n", "\\n").replace("\n", "\\n")
        return s

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Impact Medical CMS//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{_esc(uid_val)}",
        f"DTSTAMP:{_fmt(datetime.utcnow())}",
        f"DTSTART:{_fmt(start_dt)}",
        f"DTEND:{_fmt(end_dt)}",
        f"SUMMARY:{_esc(title)}",
    ]

    if location:
        lines.append(f"LOCATION:{_esc(location)}")
    if description:
        lines.append(f"DESCRIPTION:{_esc(description)}")

    lines.extend(["END:VEVENT", "END:VCALENDAR", ""])
    return "\r\n".join(lines)