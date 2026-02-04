"""
AI Vector Store

Purpose:
- Persist embeddings for claims, reports, billables, and documents
- Perform similarity search to retrieve relevant context for LLM queries

Design goals:
- Simple, explicit, swappable backend
- No business logic
- No permissions
- No prompt knowledge
"""

from __future__ import annotations

from typing import List, Dict, Any, Tuple
import json
import math
import os
import sqlite3

# ---- Configuration ----

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(__file__),
    "vector_store.sqlite3",
)

# ---- Utilities ----

def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(y * y for y in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


# ---- Store ----

class VectorStore:
    """
    Minimal persistent vector store using SQLite.

    Each row represents:
      - a chunk of text
      - its embedding
      - metadata describing where it came from
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._ensure_schema()

    # ---- Schema ----

    def _ensure_schema(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS embeddings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    namespace TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    embedding TEXT NOT NULL,
                    metadata TEXT,
                    UNIQUE(namespace, source_id, text)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_namespace ON embeddings(namespace)"
            )
            conn.commit()

    # ---- Writes ----

    def upsert(
        self,
        *,
        namespace: str,
        source_id: str,
        text: str,
        embedding: List[float],
        metadata: Dict[str, Any] | None = None,
    ) -> None:
        """
        Insert or replace an embedding entry.
        Caller is responsible for deciding when to overwrite.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO embeddings
                (namespace, source_id, text, embedding, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    namespace,
                    source_id,
                    text,
                    json.dumps(embedding),
                    json.dumps(metadata or {}),
                ),
            )
            conn.commit()

    def delete_by_source(self, namespace: str, source_id: str) -> None:
        """Delete all embeddings for a given source."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM embeddings WHERE namespace = ? AND source_id = ?",
                (namespace, source_id),
            )
            conn.commit()

    def clear_namespace(self, namespace: str) -> None:
        """Delete all embeddings in a namespace."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM embeddings WHERE namespace = ?",
                (namespace,),
            )
            conn.commit()

    # ---- Reads ----

    def similarity_search(
        self,
        *,
        namespace: str,
        query_embedding: List[float],
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Return the top_k most similar entries within a namespace.
        """
        results: List[Tuple[float, Dict[str, Any]]] = []

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT source_id, text, embedding, metadata
                FROM embeddings
                WHERE namespace = ?
                """,
                (namespace,),
            )

            for source_id, text, emb_json, meta_json in cursor.fetchall():
                emb = json.loads(emb_json)
                score = _cosine_similarity(query_embedding, emb)
                results.append(
                    (
                        score,
                        {
                            "source_id": source_id,
                            "text": text,
                            "metadata": json.loads(meta_json or "{}"),
                            "score": score,
                        },
                    )
                )

        results.sort(key=lambda x: x[0], reverse=True)
        return [r[1] for r in results[:top_k]]