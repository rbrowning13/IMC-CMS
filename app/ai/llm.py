"""
LLM adapter layer.

Purpose:
- Single, clean interface for calling language models
- Supports local-first execution with optional remote fallback
- Keeps all provider-specific logic out of routes/services

Architecture notes:
- This module is intentionally *independent* of ai_service.
- Orchestration lives in app/services/ai_service.py.
- This file should never import from app.services to avoid circular deps.

This file does NOT:
- Know about claims, reports, billables, or permissions
- Perform retrieval or embeddings
- Handle prompt construction
"""

from __future__ import annotations

# ----------------------------
# Numeric-discipline rules enforced at LLM layer
# ----------------------------
#
# When calling the LLM, we enforce strict numeric rules to ensure accuracy:
# - Do not invent numbers.
# - If totals are requested, compute them explicitly from provided data.
# - If required fields are missing, the model should say it cannot compute the answer.
# - Always include units (hours, miles, dollars).
#

import os
import time
from typing import Optional, Dict, Any, List
import json
import re


# ----------------------------
# JSON helpers
# ----------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", flags=re.S)
_JSON_OBJ_RE = re.compile(r"\{.*?\}", flags=re.S)

def extract_json(text: str) -> dict:
    """
    Extract the first JSON object from model output.
    Raises ValueError if none found or invalid.
    """
    raw = text or ""
    m = _JSON_FENCE_RE.search(raw)
    if not m:
        m = _JSON_OBJ_RE.search(raw)
    if not m:
        raise ValueError(f"No JSON object found in LLM response: {raw!r}")
    try:
        return json.loads(m.group(1) if m.lastindex else m.group(0))
    except Exception as e:
        raise ValueError(f"Invalid JSON from LLM: {raw!r}") from e


# ----------------------------
# Normalized response
# ----------------------------

class LLMResponse:
    def __init__(
        self,
        text: str,
        model: str,
        usage: Optional[Dict[str, Any]] = None,
        latency_ms: Optional[int] = None,
        provider: Optional[str] = None,
    ):
        self.text = text
        self.model = model
        self.usage = usage or {}
        self.latency_ms = latency_ms
        self.provider = provider
        self.backend = provider


# ----------------------------
# Base interface
# ----------------------------

class BaseLLM:
    name: str = "base"

    def available(self) -> bool:
        raise NotImplementedError

    def supports_roles(self) -> bool:
        return True

    def call(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        expect_json: bool = False,
    ) -> LLMResponse:
        raise NotImplementedError

    def embed(self, texts: List[str]) -> List[List[float]]:
        raise NotImplementedError


# ----------------------------
# Mock Local LLM (dev fallback)
# ----------------------------

class MockLocalLLM(BaseLLM):
    name = "mock"

    def available(self) -> bool:
        return True

    def supports_roles(self) -> bool:
        return True

    def call(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        expect_json: bool = False,
    ) -> LLMResponse:
        # Find last user message
        user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_msg = m.get("content", "")
                break

        text = (
            "This is a mock Florence response.\n\n"
            f"You asked:\n{user_msg}\n\n"
            "No local LLM backend is running yet. "
            "This response confirms end-to-end wiring, retrieval, and UI flow."
        )

        return LLMResponse(
            text=text,
            model="mock-llm",
            latency_ms=0,
            provider="mock",
        )

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [[0.0] * 8 for _ in texts]


# ----------------------------
# Local (Ollama / llama.cpp)
# ----------------------------

class LocalLLM(BaseLLM):
    name = "local"

    def __init__(self):
        self.model = os.getenv("LOCAL_LLM_MODEL", "llama3.1")
        self.base_url = os.getenv("LOCAL_LLM_URL", "http://localhost:11434")
        self.timeout = int(os.getenv("LOCAL_LLM_TIMEOUT", "120"))

    def available(self) -> bool:
        try:
            import requests
            r = requests.get(f"{self.base_url}/api/tags", timeout=0.5)
            return r.ok
        except Exception:
            return False

    def warmup(self) -> None:
        """
        Send a tiny no-op request to warm the model and avoid first-call latency.
        Safe to call multiple times.
        """
        try:
            import requests
            payload = {
                "model": self.model,
                "prompt": "ping",
                "stream": False,
            }
            requests.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=2,
            )
        except Exception:
            pass

    def supports_roles(self) -> bool:
        return False

    def call(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        expect_json: bool = False,
    ) -> LLMResponse:
        import requests

        parts: list[str] = []
        for m in messages:
            content = m.get("content")
            if not content:
                continue
            role = m.get("role")
            if role and role != "user":
                parts.append(f"[{role.upper()}]\n{content}")
            else:
                parts.append(content)

        prompt = "\n\n".join(parts)

        start = time.time()
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
        }

        r = requests.post(
            f"{self.base_url}/api/generate",
            json=payload,
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        latency_ms = int((time.time() - start) * 1000)

        text = (data.get("response") or "").strip()

        if expect_json:
            obj = extract_json(text)
            text = json.dumps(obj)

        return LLMResponse(
            text=text,
            model=self.model,
            latency_ms=latency_ms,
            provider="local",
        )

    def embed(self, texts: List[str]) -> List[List[float]]:
        import requests

        payload = {
            "model": self.model,
            "input": texts,
        }
        r = requests.post(
            f"{self.base_url}/api/embeddings",
            json=payload,
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        return [d["embedding"] for d in data.get("data", [])]


# ----------------------------
# Router + public API
# ----------------------------

class LLMRouter:
    """
    Chooses the best available LLM.

    Priority:
      1) Local (if enabled and reachable)
      2) Remote (OpenAI)
    """

    def __init__(self):
        self.local = LocalLLM()
        self.mock = MockLocalLLM()

        # Best-effort warmup to reduce first-call latency
        if self.local.available():
            self.local.warmup()

    def _numeric_guard(self, messages):
        """
        Ensure numeric guardrails are always applied.
        Accepts either a raw prompt string or a list of messages.
        """

        # Normalize input to OpenAI-style messages
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        elif isinstance(messages, dict):
            messages = [messages]

        numeric_rules_msg = {
            "role": "system",
            "content": (
                "NUMERIC RULES:\n"
                "- Do not invent numbers.\n"
                "- If totals are requested, compute them explicitly from provided data.\n"
                "- If required fields are missing, say you cannot compute the answer.\n"
                "- Always include units (hours, miles, dollars)."
            ),
        }

        if os.getenv("LLM_NUMERIC_GUARD", "1") == "1":
            return [numeric_rules_msg] + messages

        return messages

    def status(self) -> dict:
        """
        Return availability and configuration of LLM backends.
        Safe to expose for diagnostics.
        """
        active = "local" if self.local.available() else "mock"
        return {
            "backend": active,
            "model": getattr(self.local, "model", None) if active == "local" else "mock-llm",
            "available": True,
            "external_inference": False,
        }

    def _select_backend(self) -> BaseLLM:
        if self.local.available():
            return self.local
        return self.mock

    def _call_with_fallback(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        expect_json: bool = False,
    ) -> LLMResponse:
        backend = self._select_backend()
        return backend.call(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            expect_json=expect_json,
        )

    def call_text(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        guarded_messages = self._numeric_guard(messages)
        backend = self._select_backend()
        msgs = guarded_messages
        if not backend.supports_roles():
            msgs = [{"role": "user", "content": m.get("content")} for m in guarded_messages]
        return self._call_with_fallback(
            msgs,
            temperature=temperature,
            max_tokens=max_tokens,
            expect_json=False,
        )

    def call_json(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> dict:
        guarded_messages = self._numeric_guard(messages)
        backend = self._select_backend()
        msgs = guarded_messages
        if not backend.supports_roles():
            msgs = [{"role": "user", "content": m.get("content")} for m in guarded_messages]
        resp = self._call_with_fallback(
            msgs,
            temperature=temperature,
            max_tokens=max_tokens,
            expect_json=True,
        )
        try:
            return extract_json(resp.text)
        except Exception as e:
            raise RuntimeError(
                f"LLM JSON parse failed (provider={resp.provider}, model={resp.model})"
            ) from e

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not self.local.available():
            raise RuntimeError("Local embedding backend unavailable")
        return self.local.embed(texts)



# Shared singleton
llm = LLMRouter()


# ----------------------------
# Helper: Expose active backend info
# ----------------------------
def get_active_llm_info() -> dict:
    """
    Return metadata about the currently selected LLM backend.
    Safe for diagnostics, UI display, and provenance tracking.
    """
    active = "local" if llm.local.available() else "mock"
    return {
        "backend": active,
        "provider": active,
        "model": getattr(llm.local, "model", None) if active == "local" else "mock-llm",
        "local": True,
    }


def call_llm(
    messages,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    expect_json: bool = False,
):
    """
    Thin convenience wrapper used by ai_service.

    Returns:
      - dict when expect_json=True
      - str when expect_json=False
    """
    if expect_json:
        return llm.call_json(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    resp = llm.call_text(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return resp.text


# ----------------------------
# call_llm_with_meta: returns both text and model metadata
# ----------------------------
def call_llm_with_meta(
    messages,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    expect_json: bool = False,
):
    """
    Like call_llm(), but returns metadata for UI/debugging.

    Returns:
      {
        "text": str | dict,
        "model_source": "local" | "openai",
        "model": str,
        "latency_ms": int | None,
      }
    """
    if expect_json:
        resp = llm._call_with_fallback(
            llm._numeric_guard(messages),
            temperature=temperature,
            max_tokens=max_tokens,
            expect_json=True,
        )
        try:
            parsed = extract_json(resp.text)
        except Exception:
            parsed = {}
        return {
            "text": parsed,
            "model_source": resp.provider,
            "model": resp.model,
            "latency_ms": resp.latency_ms,
        }

    resp = llm.call_text(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return {
        "text": resp.text,
        "model_source": resp.provider,
        "model": resp.model,
        "latency_ms": resp.latency_ms,
    }


# Explicit export list for stable, intentional imports
__all__ = [
    "LLMResponse",
    "BaseLLM",
    "MockLocalLLM",
    "LocalLLM",
    "LLMRouter",
    "llm",
    "call_llm",
    "call_llm_with_meta",
]