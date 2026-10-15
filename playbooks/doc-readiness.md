---
name: doc-readiness
summary: second reader for a spec section — contradictions between sections or against its SOT, before a version bump
level: routine
grill: skip
triggers: "readiness|second reader|contradiction|§|end to end|consisten|ready to (bump|publish)|พร้อม(ประกาศ|bump)"
form: direct
peers: claude
mode: ask
inputs: the section list (§ numbers) · the SOT sibling to compare against · what to ignore (register, typos)
output: "table `§X claim A` vs `§Y claim B` with file:line, or NONE"
budget: "≈ 3–8 min per section pair, $1–4; never one run over a whole 100 KB+ document"
---
Evidence: 3 direct runs cited in specs; the one panel of this kind (167 KB spec + 5 SOTs + 2 repos)
killed both workers at 600 s and produced nothing (2026-09-17). The fix that worked: `ask-claude -m ask -t 1500`
per role with one section + one sibling per run.

How: one section pair per run, table output, run as `peer-ask`. Open both claims before calling it a contradiction.

Agreement: routine — but the section split is part of the request; a request without a section list gets the card
back with the split proposed.
