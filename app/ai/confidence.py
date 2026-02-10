

from typing import Any, Dict, Optional

# -------------------------------------------------------------------
# Confidence calculation helpers
# -------------------------------------------------------------------

def compute_confidence(
    *,
    text: Optional[str] = None,
    had_data: bool = True,
    was_fallback: bool = False,
    partial: bool = False,
) -> float:
    """
    Compute a confidence score for an AI response.

    This mirrors existing implicit behavior:
    - Full deterministic answers → high confidence
    - Missing or partial data → reduced confidence
    - Fallback / unclear cases → lowest confidence
    """
    if not text or not text.strip():
        return 0.3

    if was_fallback:
        return 0.6

    if partial:
        return 0.7

    if had_data:
        return 1.0

    return 0.5


def confidence_payload(
    *,
    confidence: float,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Normalize confidence into a consistent payload shape.
    """
    return {
        "confidence": round(float(confidence), 2),
        "notes": notes,
    }