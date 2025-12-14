

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


# -----------------------------------------------------------------------------
# Invoice helpers
# -----------------------------------------------------------------------------

def _generate_invoice_number(prefix: str = "INV") -> str:
    """Generate a human-friendly invoice number.

    Prefer uniqueness but avoid heavy DB coupling; this is "good enough" for a
    single-user local app.
    """
    today = date.today()
    # Example: INV-20251214-4832
    return f"{prefix}-{today.strftime('%Y%m%d')}-{random.randint(1000, 9999)}"


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


# Back-compat public aliases (some modules may import non-underscored names)

def generate_invoice_number(prefix: str = "INV") -> str:
    return _generate_invoice_number(prefix=prefix)


def calculate_invoice_totals(invoice):
    return _calculate_invoice_totals(invoice)


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