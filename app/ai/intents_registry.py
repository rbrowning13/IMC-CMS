

"""
Intent Registry

This file defines the *behavioral contract* for each semantic intent.
An intent declares:
- what data it is allowed to see
- what analytics it requires
- whether LLM reasoning is permitted
- how responses should be framed

This is the single source of truth for meaning, not routing.
"""

from typing import Callable, Dict, Any



class IntentSpec:
    def __init__(
        self,
        name: str,
        analytics_fn: Callable[..., Dict[str, Any]] | None = None,
        domain: str | None = None,
        description: str = "",
        required_models: list[str] | None = None,
        forbidden_models: list[str] | None = None,
        llm_allowed: bool = True,
        prompt_hint: str | None = None,
    ):
        """
        IntentSpec defines what a user question *means* and how it should be handled.

        - analytics_fn: deterministic analytics function (if any)
        - domain: high-level domain (billing, workload, claims, reports, etc.)
        - required_models: models that MUST be present in context
        - forbidden_models: models that MUST NOT be included in context
        - llm_allowed: whether the LLM may be used to elaborate/explain
        - prompt_hint: short instruction passed to LLM for framing
        """
        self.name = name
        self.analytics_fn = analytics_fn
        self.domain = domain
        self.description = description
        self.required_models = required_models or []
        self.forbidden_models = forbidden_models or []
        self.llm_allowed = llm_allowed
        self.prompt_hint = prompt_hint


INTENT_REGISTRY: Dict[str, IntentSpec] = {}


def register_intent(intent: IntentSpec) -> None:
    """
    Register a new intent.
    """
    INTENT_REGISTRY[intent.name] = intent


def get_intent(intent_name: str) -> IntentSpec | None:
    """
    Fetch an intent spec by name.
    """
    return INTENT_REGISTRY.get(intent_name)


def list_intents() -> list[str]:
    """
    Return all registered intent names.
    """
    return sorted(INTENT_REGISTRY.keys())