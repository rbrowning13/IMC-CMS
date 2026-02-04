"""
AI permissions and capability gating.

This module is the single source of truth for what AI is allowed to
SEE, SUGGEST, or WRITE inside the system.

Nothing in AI should directly write to the database unless explicitly
allowed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from flask import current_app

from app.models import Settings


@dataclass(frozen=True)
class AICapabilities:
    """
    Describes what the AI is allowed to do in the current environment.

    These flags are intentionally coarse-grained and conservative.
    """
    enabled: bool

    # Read / context access
    read_claims: bool
    read_reports: bool
    read_billables: bool
    read_documents: bool

    # PHI handling
    allow_phi: bool

    # Suggestions vs writes
    allow_suggestions: bool
    allow_writes: bool  # direct DB writes (VERY dangerous)

    # Infrastructure features
    use_embeddings: bool
    use_local_llm: bool


def _get_settings() -> Optional[Settings]:
    try:
        return Settings.query.first()
    except Exception:
        return None


def get_ai_capabilities() -> AICapabilities:
    """
    Resolve AI capabilities based on Settings, environment, and safety defaults.

    This function should be called once per request and passed downward.
    """

    settings = _get_settings()

    # Hard kill switch via env
    if current_app.config.get("OPENAI_DISABLED") or (
        settings and not getattr(settings, "ai_enabled", False)
    ):
        return AICapabilities(
            enabled=False,
            read_claims=False,
            read_reports=False,
            read_billables=False,
            read_documents=False,
            allow_phi=False,
            allow_suggestions=False,
            allow_writes=False,
            use_embeddings=False,
            use_local_llm=False,
        )

    # Defaults: read-only, suggest-only
    allow_phi = bool(getattr(settings, "ai_allow_phi", False)) if settings else False

    return AICapabilities(
        enabled=True,

        # Read permissions
        read_claims=True,
        read_reports=True,
        read_billables=True,
        read_documents=True,

        # PHI
        allow_phi=allow_phi,

        # Write control
        allow_suggestions=True,
        allow_writes=False,  # NEVER default this to True

        # Infra flags (future-wired)
        use_embeddings=bool(getattr(settings, "ai_use_embeddings", False)) if settings else False,
        use_local_llm=bool(getattr(settings, "ai_use_local_llm", False)) if settings else False,
    )


def require_write_permission(caps: AICapabilities) -> None:
    """
    Guardrail helper: call before ANY AI write action.
    """
    caps = _coerce_caps(caps)
    if not caps or not caps.enabled or not caps.allow_writes:
        raise PermissionError("AI write actions are not permitted in this environment.")



# ---- Fine-grained helpers used by retrieval / tools ----

def _coerce_caps(obj: Any) -> Optional[AICapabilities]:
    """
    Defensive helper:
    Some callers may accidentally pass a model object instead of AICapabilities.

    We never want that to 500 the request; instead we log (in debug) and treat
    capabilities as missing/disabled.
    """
    if obj is None:
        return None
    if isinstance(obj, AICapabilities):
        return obj

    # Duck-typing fallback (only if it actually looks like caps)
    if all(hasattr(obj, attr) for attr in ("enabled", "read_claims", "read_reports", "read_billables", "read_documents")):
        try:
            return AICapabilities(
                enabled=bool(getattr(obj, "enabled")),
                read_claims=bool(getattr(obj, "read_claims")),
                read_reports=bool(getattr(obj, "read_reports")),
                read_billables=bool(getattr(obj, "read_billables")),
                read_documents=bool(getattr(obj, "read_documents")),
                allow_phi=bool(getattr(obj, "allow_phi", False)),
                allow_suggestions=bool(getattr(obj, "allow_suggestions", False)),
                allow_writes=bool(getattr(obj, "allow_writes", False)),
                use_embeddings=bool(getattr(obj, "use_embeddings", False)),
                use_local_llm=bool(getattr(obj, "use_local_llm", False)),
            )
        except Exception:
            return None

    # Wrong type passed; do not explode.
    try:
        if current_app and current_app.debug:
            current_app.logger.warning(
                "AI permissions: expected AICapabilities but got %s; treating as disabled.",
                type(obj).__name__,
            )
    except Exception:
        pass
    return None

def allow_billable(caps: AICapabilities) -> bool:
    """Whether AI may read billable item details (hours, miles, dollars)."""
    caps = _coerce_caps(caps)
    return bool(caps and caps.enabled and caps.read_billables)


def allow_documents(caps: AICapabilities) -> bool:
    """Whether AI may read uploaded documents."""
    caps = _coerce_caps(caps)
    return bool(caps and caps.enabled and caps.read_documents)


def allow_reports(caps: AICapabilities) -> bool:
    """Whether AI may read report content."""
    caps = _coerce_caps(caps)
    return bool(caps and caps.enabled and caps.read_reports)


def allow_claims(caps: AICapabilities) -> bool:
    """Whether AI may read claim-level fields."""
    caps = _coerce_caps(caps)
    return bool(caps and caps.enabled and caps.read_claims)


def allow_embeddings(caps: AICapabilities) -> bool:
    """Whether AI may use embeddings / vector search."""
    caps = _coerce_caps(caps)
    return bool(caps and caps.enabled and caps.use_embeddings)


def allow_local_llm(caps: AICapabilities) -> bool:
    """Whether AI may use a locally hosted LLM."""
    caps = _coerce_caps(caps)
    return bool(caps and caps.enabled and caps.use_local_llm)


def allow_any_ai(caps: AICapabilities) -> bool:
    """Quick check for any AI capability at all."""
    caps = _coerce_caps(caps)
    return bool(caps and caps.enabled)