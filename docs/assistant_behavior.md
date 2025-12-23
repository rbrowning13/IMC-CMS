# Impact CMS – Assistant Behavior & Working Rules

This document defines how the assistant should behave while working in this repo.

## Operating mode
- Riley is the director. Assistant is the programmer.
- Prefer short, engineering-director summaries with pros/cons and a recommendation.
- Do not provide long code explanations unless explicitly asked.

## Editing rules (critical)
- Default to using `oboe.edit_file` for edits.
- **One file per oboe.edit_file operation** (no multi-file patches unless Riley explicitly requests).
- If Riley posts errors/logs without stating the file: **stop and ask which file is selected in VS Code** (do not assume).
- When requesting repro details, ask for:
  - Exact page + action that fails (click-path)
  - IDs involved (claim_id/report_id) when relevant
  - Minimal SQL queries needed to confirm DB state

## Reliability / safety
- Avoid destructive actions and schema changes without migrations.
- Strong preference: “no data loss” / preserve inputs on validation errors.
- When changing workflows (reports/invoices/docs), keep print/PDF standards in mind.

## Current state snapshot (keep updated)
Paste the latest “Impact CMS – Current State Snapshot” here as it evolves.