import json
import os
from typing import Any, Dict, Optional

from app.services import ai_service

RESPONSE_SCHEMA = """
{
  "answer": "string",
  "citations": ["string"],
  "is_guess": boolean
}
"""

def ask_florence(
    question: str,
    claim_id: Optional[int] = None,
    top_k: int = 5,
    filters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Core Florence entry point.
    Retrieves structured facts (if any) and synthesizes an answer.
    """

    retrieval_result = ai_service.retrieve(
        claim_id=claim_id,
        query=question,
    )

    facts = retrieval_result.get("facts", []) or []
    chunks = retrieval_result.get("chunks", []) or []
    sources = retrieval_result.get("sources", []) or []

    if os.getenv("FLORENCE_DEBUG"):
        print("[Florence][retrieval]", {
            "facts_len": len(facts),
            "chunks_len": len(chunks),
            "sources_len": len(sources),
        })

    authoritative_facts = [
        f for f in facts
        if f.get("authority") == "authoritative" and "value" in f
    ]

    if len(authoritative_facts) == 1:
        fact = authoritative_facts[0]
        value = fact.get("value")
        label = fact.get("label")

        answer_text = f"{value} {label}" if label else str(value)

        return {
            "answer": answer_text,
            "citations": [fact.get("source_id")] if fact.get("source_id") else [],
            "is_guess": False,
        }

    if len(authoritative_facts) > 1:
        q_lower = question.lower()
        if any(k in q_lower for k in ["how many", "count", "total"]):
            total = sum(
                f.get("value", 0)
                for f in authoritative_facts
                if isinstance(f.get("value"), (int, float))
            )
            source_ids = [
                f.get("source_id")
                for f in authoritative_facts
                if f.get("source_id")
            ]

            return {
                "answer": str(total),
                "citations": source_ids,
                "is_guess": False,
            }

    synthesized_prompt = (
        f"{RESPONSE_SCHEMA.strip()}\n\n"
        "TASK:\n"
        "Answer the user's question directly using the facts provided.\n"
        "Do NOT summarize the facts.\n"
        "Do NOT describe what data you see.\n"
        "Produce a clear, direct answer to the question.\n\n"
        f"USER QUESTION:\n{question}\n\n"
        "AVAILABLE CONTEXT:\n"
        + (
            "\n".join(f"- {json.dumps(f)}" for f in facts)
            if facts
            else "- No structured context was retrieved.\n"
        )
        + f"\nSOURCE IDS YOU MAY CITE: {sources}\n\n"
        "RULES:\n"
        "- If the facts fully answer the question, set is_guess=false.\n"
        "- If no structured context exists for a numeric question, say the data is not available and set is_guess=true.\n"
        "- Do NOT invent totals, counts, or calculations.\n"
        "- Cite ONLY from the provided source IDs.\n"
        "- Respond ONLY in valid JSON matching the schema.\n"
    )

    return ai_service.generate(prompt=synthesized_prompt)