from __future__ import annotations

"""
embeddings.py

Purpose:
- Semantic indexing for AI features (OPTIONAL, gated).
- Safe-by-default: system works fully without embeddings.
- Embeddings are for recall/search/summarization ONLY.
"""

import hashlib
import math
from typing import Iterable, List, Optional

# Optional import: llm layer (may be unavailable during early bring-up)
try:
    from app.ai import llm
except Exception:
    llm = None


# -------------------------------------------------------------------
# Feature gates
# -------------------------------------------------------------------

def is_embeddings_enabled(settings=None) -> bool:
    """
    Global embeddings feature gate.
    """
    if settings is not None:
        return bool(getattr(settings, "ai_embeddings_enabled", False))
    return True


def _can_use_local_embeddings(settings=None) -> bool:
    """
    Local embeddings require BOTH:
      - embeddings enabled
      - local embeddings explicitly enabled
    """
    if settings is not None:
        return bool(getattr(settings, "ai_embeddings_enabled", False)) and bool(
            getattr(settings, "ai_local_embeddings_enabled", False)
        )
    return False


def embeddings_available(settings=None) -> bool:
    """
    Guard used by retrieval layer.
    """
    return is_embeddings_enabled(settings)


# -------------------------------------------------------------------
# Data container
# -------------------------------------------------------------------

FALLBACK_DIM = 64  # Small + fast deterministic fallback


class EmbeddingRecord:
    """
    Lightweight container for an embedded text chunk.
    NOT a database model.
    """

    def __init__(
        self,
        *,
        source_type: str,
        source_id: int | None,
        text: str,
        embedding: Optional[List[float]] = None,
        metadata: Optional[dict] = None,
    ):
        self.source_type = source_type
        self.source_id = source_id
        self.text = text
        self.embedding = embedding
        self.metadata = metadata or {}


# -------------------------------------------------------------------
# Math utilities
# -------------------------------------------------------------------

def cosine_similarity(a: List[float], b: List[float]) -> float:
    """
    Compute cosine similarity between two vectors.
    Returns 0.0 if vectors are mismatched or degenerate.
    """
    if not a or not b or len(a) != len(b):
        return 0.0

    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    return dot / (norm_a * norm_b)


def batch(iterable, size: int):
    """
    Yield successive lists of length `size` from iterable.
    """
    buf = []
    for item in iterable:
        buf.append(item)
        if len(buf) == size:
            yield buf
            buf = []
    if buf:
        yield buf


# -------------------------------------------------------------------
# Deterministic fallback embeddings (NO AI, NO NETWORK)
# -------------------------------------------------------------------

def _hash_embedding(text: str, dim: int = FALLBACK_DIM) -> List[float]:
    """
    Create a deterministic pseudo-embedding from text.
    Used when no embedding model is available.
    """
    h = hashlib.sha256(text.encode("utf-8")).digest()
    vals: List[float] = []

    for i in range(dim):
        b = h[i % len(h)]
        vals.append((b / 255.0) * 2.0 - 1.0)

    norm = math.sqrt(sum(v * v for v in vals)) or 1.0
    return [v / norm for v in vals]


# -------------------------------------------------------------------
# Embedding generation
# -------------------------------------------------------------------

def embed_texts(texts: Iterable[str], settings=None) -> List[List[float]]:
    """
    Generate embeddings for a list of texts.

    Priority order:
      Tier 1: Local embedding model (if enabled + available)
      Tier 2: Remote embedding model (if embeddings enabled)
      Tier 3: Deterministic hash embeddings (always available)
    """
    clean = [t.strip() for t in texts if t and t.strip()]
    if not clean:
        return []

    # Tier 1: Local embeddings
    if _can_use_local_embeddings(settings) and llm and hasattr(llm, "embed"):
        try:
            vecs = llm.embed(clean)
            if vecs and len(vecs) == len(clean):
                return vecs
        except Exception:
            pass

    # Tier 2: Remote embeddings
    if is_embeddings_enabled(settings) and llm and hasattr(llm, "embed"):
        try:
            vecs = llm.embed(clean)
            if vecs and len(vecs) == len(clean):
                return vecs
        except Exception:
            pass

    # Tier 3: Deterministic fallback
    return [_hash_embedding(t) for t in clean]


def build_embedding_records(
    *,
    source_type: str,
    source_id: int | None,
    texts: Iterable[str],
    metadata: Optional[dict] = None,
    settings=None,
) -> List[EmbeddingRecord]:
    """
    Convert raw text into EmbeddingRecord objects.
    Embeddings may be generated immediately or deferred.
    """
    clean_texts = [t.strip() for t in texts if t and t.strip()]
    if not clean_texts:
        return []

    embeddings = embed_texts(clean_texts, settings=settings)

    records: List[EmbeddingRecord] = []
    for text, emb in zip(clean_texts, embeddings):
        records.append(
            EmbeddingRecord(
                source_type=source_type,
                source_id=source_id,
                text=text,
                embedding=emb,
                metadata=metadata,
            )
        )

    return records

# -------------------------------------------------------------------
# Public helpers expected by retrieval layer
# -------------------------------------------------------------------

def embed_query(text: str, settings=None) -> Optional[List[float]]:
    """
    Embed a single query string.
    Safe wrapper used by retrieval.
    """
    if not text or not text.strip():
        return None
    vecs = embed_texts([text], settings=settings)
    return vecs[0] if vecs else None


def similarity(query_vec: List[float], records: Iterable[EmbeddingRecord]):
    """
    Compute similarity scores between a query vector and embedding records.

    Returns:
        List of (record, score) sorted by score desc.
    """
    if not query_vec:
        return []

    scored = []
    for r in records:
        emb = getattr(r, "embedding", None)
        if not emb:
            continue
        score = cosine_similarity(query_vec, emb)
        scored.append((r, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored
