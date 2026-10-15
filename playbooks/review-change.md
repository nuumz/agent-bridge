---
name: review-change
summary: review a diff, MR, or uncommitted worktree for defects, or adjudicate an existing set of review findings
level: routine
grill: skip
level_when_adjudicating: confirm
triggers: "\\bMR\\b|merge request|![0-9]{3,4}|review (this|the|my) (diff|change|worktree)|รีวิว|review|git diff|adjudicat|review findings|open review threads"
form: direct
peers: cursor
mode: ask
panel_roles: design-critic,as-built-realist,reliability-critic
inputs: repo root · branch or SHA · the diff or MR id · (adjudication) the finding list verbatim
output: "table `file:line — finding — severity — must-fix (yes/no) — why`; NONE when clean; adjudication: per finding SUPPORTED / REJECTED + the smallest fix"
budget: "direct ≈ 2–5 min, $0–1.5 · panel 3 roles ≈ 6–8 min, $3–6"
---
Evidence (14-day ledger, 2026-09-26): the most common job — 47 direct runs (cursor 28 · copilot 10 · claude 9) and
15 of 33 panels (adjudicating findings on !3431/!3432/!3454/!3491/!781/!593, draft MRs, CR219). None of those panels
was cited in a spec (`peer review: panel …` = 0) because the result lands in the MR thread and the code, not in a doc.

How: one peer from another model family reads the diff with `-m ask` and answers as the table above; run it as a
subagent (`peer-ask`) and verify every `file:line` before acting. Escalate to a panel only when there is already a
finding list to adjudicate (which findings must become code) — then the roles are the critic, the as-built realist
(what exists on the branch today) and the reliability critic (money-adjacent paths); root-cause adds nothing here.

Agreement: routine for a review; **confirm** for adjudication — show the roles, the finding list and the budget and
wait for one word, because a panel over the wrong finding set is the most expensive way to say "no change".
