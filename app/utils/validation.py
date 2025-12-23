import re

# -----------------------------
# Phone / Fax
# -----------------------------

PHONE_DIGITS_RE = re.compile(r"\D+")

def normalize_phone(value: str | None) -> str | None:
    """
    Strip all non-digits. Return digits-only string or None.
    """
    if not value:
        return None
    digits = PHONE_DIGITS_RE.sub("", value)
    return digits or None


def is_valid_phone(value: str | None) -> bool:
    """
    Valid phone numbers:
    - empty / None → valid
    - exactly 10 digits → valid
    """
    if not value:
        return True
    digits = normalize_phone(value)
    return bool(digits and len(digits) == 10)

def validate_phone_or_fax(value: str | None) -> bool:
    return is_valid_phone(value)


# -----------------------------
# Email
# -----------------------------

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def is_valid_email(value: str | None) -> bool:
    """
    Empty email is allowed.
    Basic sanity check, not RFC insanity.
    """
    if not value:
        return True
    return bool(EMAIL_RE.match(value.strip()))

def validate_email(value: str | None) -> bool:
    return is_valid_email(value)


# -----------------------------
# ZIP Code
# -----------------------------

ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")

def is_valid_zip(value: str | None) -> bool:
    """
    Valid:
    - empty
    - 12345
    - 12345-6789
    """
    if not value:
        return True
    return bool(ZIP_RE.match(value.strip()))

def validate_postal_code(value: str | None) -> bool:
    return is_valid_zip(value)


# -----------------------------
# Error helpers
# -----------------------------

def validate_fields(field_map: dict[str, tuple[str | None, callable]]):
    """
    field_map = {
        "Phone": (phone_value, is_valid_phone),
        "Email": (email_value, is_valid_email),
    }

    Returns: list[str] of error messages
    """
    errors = []
    for label, (value, validator) in field_map.items():
        try:
            if not validator(value):
                errors.append(f"{label} is invalid")
        except Exception:
            errors.append(f"{label} is invalid")
    return errors
