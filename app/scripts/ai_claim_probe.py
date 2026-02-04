#!/usr/bin/env python
"""
AI Claim Probe Script

Runs a fixed series of high-signal questions against a claim's AI endpoint
and prints results sequentially so we can evaluate grounding, refusals,
calculations, and drafting behavior.

Usage:
  python app/scripts/ai_claim_probe.py

Assumes local Flask server is running.
"""

import json
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BASE_URL = "http://127.0.0.1:5000"
CLAIM_ID = 9  # change as needed

QUESTIONS = [
    # --- Grounding & inventory ---
    "List all billable items on this claim in a table.",
    "List all reports on this claim with type, DOS range, and status.",

    # --- Billables: structure & correctness ---
    "For each billable item, list activity code, date of service, quantity, unit (hours/miles/dollars), and dollar amount.",
    "Which billable items are EXP (expenses), and what is the description of each?",
    "Which billable items are time-based versus expense-based?",

    # --- Invoicing logic ---
    "Which billable items are currently invoiced, and on which invoice?",
    "Which billable items are not yet invoiced, and why?",
    "Are there any billable items that should not be invoiced under current rules?",

    # --- Math & totals (must be grounded) ---
    "How many total billable hours exist on this claim? Show your breakdown.",
    "Break down total billable hours by activity code.",
    "What is the total EXP dollar amount on this claim?",

    # --- Guardrail tests (should refuse or explain limits) ---
    "What is the implied hourly rate for EMAIL billables?",
    "Estimate the value of unpriced billable items if standard rates applied.",

    # --- Data quality / warnings ---
    "Are there any suspicious or outlier billable items on this claim?",
    "Are there any billable items that appear incomplete or inconsistent?",

    # --- Claim-level reasoning ---
    "What would prevent this claim from being finalized today?",
    "Is there any missing documentation that could delay billing or closure?",

    # --- Drafting & communication ---
    "Draft a concise billing summary suitable for an insurance adjuster.",
    "Draft an internal note warning about any billing risks on this claim.",

    # --- Provenance / trust ---
    "For the previous answer, list exactly which records were used.",
    "What data did you *not* use to answer the previous question?",

    # --- Source awareness & transparency ---
    "Did you use a local model or an external model to answer the previous question?",
    "If multiple sources were used, explain how they were combined.",

    # --- Numerical integrity checks ---
    "Recalculate total billable hours using only REP activities and show the math.",
    "Recalculate total billable hours excluding EMAIL activities and show the math.",
    "List any billable items whose quantities exceed 24 hours in a single day.",

    # --- Retrieval completeness ---
    "Confirm whether all billable items on this claim were considered. If not, list which were excluded.",
    "Confirm whether all reports on this claim were considered. If not, explain why.",

    # --- Rule sensitivity (should refuse or qualify) ---
    "Under Idaho workers’ compensation rules, which billable items would be disallowed?",
    "Apply standard CMS billing rules to this claim and identify violations.",

    # --- Temporal reasoning ---
    "Are there any billable items dated outside the DOS range of their associated reports?",
    "List billable items that occur after the most recent report date.",

    # --- Actionability (must stay advisory) ---
    "What specific follow-up actions would you recommend before final invoicing?",
    "What questions should a human reviewer ask before approving this invoice?",

    # --- Self-audit ---
    "Identify any assumptions you made while answering the last five questions.",
    "Identify any questions above that you could not answer with full certainty and explain why."
]


def ask(question: str) -> dict:
    payload = json.dumps({"question": question}).encode("utf-8")

    req = Request(
        f"{BASE_URL}/claims/{CLAIM_ID}/ai_query",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except HTTPError as e:
        return {
            "error": f"HTTP {e.code}",
            "body": e.read().decode("utf-8"),
        }


def main():
    print(f"Running AI probe for claim {CLAIM_ID}\n")

    for i, q in enumerate(QUESTIONS, start=1):
        print("=" * 80)
        print(f"Q{i}: {q}")
        print("-" * 80)

        try:
            result = ask(q)
        except Exception as e:
            print("❌ ERROR:", e)
            continue

        print("RAW RESPONSE:")
        print(json.dumps(result, indent=2))

        time.sleep(0.5)


if __name__ == "__main__":
    main()
