---
name: impl-spec-edit
summary: edit design-spec markdown/CSV under specs/ — parser tools, single-anchor edits, lint-delta
triggers: "specs/|spec-edit\\.py|spec-cell\\.py|lint-delta"
paths: "specs/"
verify_hint: "python tools/spec-edit.py lint-delta <files>"
---
### implementer
- Prefer `tools/spec-edit.py apply` / `tools/spec-cell.py set` over raw string replace on tables.
- Every anchor must match exactly once; 0 or 2+ hits → re-read, do not retry blind.
- After edit: re-read the region; bump version/changelog when the doc uses them; run lint-delta on touched paths.
- Do not invent project IDs — cite an existing owning row or leave an open item.

### reviewer
- must-fix=yes if a table cell was shifted, a version bump is missing, or a new ID was minted without an owning SOT row.
- Flag whole-file Thai↔English rewrites and lint-driven Englify of narrative.
- Confirm citations still resolve (heading / symbol), not bare line numbers alone.
