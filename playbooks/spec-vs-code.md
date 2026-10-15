---
name: spec-vs-code
summary: check whether a spec / contract claim matches the code as built (SUPPORTED · CONTRADICTED · NOT-FOUND per claim)
level: routine
grill: skip
triggers: "SUPPORTED|CONTRADICTED|NOT-FOUND|as[- ]built|spec (claim|vs)|ตรงกับโค้ด|matches the code|drift"
form: direct
peers: claude
alt_peers: cursor
mode: ask
inputs: claim list (one per line) · repo + branch/SHA to read (`git show <ref>:<path>`, never the working tree) · the spec section
output: "claim-by-claim table: claim · verdict · spec file:line · code repo@ref:path:line · note"
budget: "≈ 2–5 min, $0.5–3 (claude); split above ~10 claims"
---
Evidence: 26 direct runs (claude 16 · cursor 8 · copilot 2); 10 `peer review: ask-claude` rows in the specs come from
this kind — the best-cited direct job. The `implementation-reviewer` subagent covers the same ground read-only inside
Claude Code; use the bridge when a second family (cursor `contract-checker`) should disagree, or when the claim set
needs a fresh long context.

How: give the claims as a numbered list and pin the ref; ask for the table only. Re-derive every cited line with
`git grep` on the named branch before it enters a SOT — a peer answer is a lead. Not a panel: one reader, one table.

Agreement: routine.
