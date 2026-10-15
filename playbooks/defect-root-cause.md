---
name: defect-root-cause
summary: find the root cause of a defect or incident and the smallest safe fix (optionally a patch sketch)
level: confirm
grill: recommend
triggers: "root cause|incident|symptom|defect|P32-[A-Z]+-[0-9]+|bug|why does|ทำไม.*ผิด"
form: panel
peers: claude,cursor
panel_roles: as-built-realist,spec-reader,design-critic
optional_roles: root-cause,reliability-critic
mode: ask
inputs: symptom (what happened, expected vs actual) · repro or log · BRD line for the behaviour · spec section · repo + branch/SHA
output: "report: ranked root causes with file:line · trigger vs contributing factors · decisions for the user · next actions; `--fix` adds patch.md (diff sketch, `git apply --check` verdict)"
budget: "3 roles + cross ≈ 5–8 min, $4–8; +1 leader run for --fix"
---
Evidence: 26 direct runs and ~6 panels (sgn07-email, p32-sgn04-backnav, flow-card-ready→2, mf-fail-2-1-14-2, the
cutoff series). The dedicated `root-cause` role was used in 5 panels and never in the ones that reached a decision;
`as-built-realist` + `spec-reader` settled the disputes (spec-reader ran the code to break 4 ties, 2026-09-12).
`flow-card-ready` died first without a case file and succeeded as `flow-card-ready2` with one — build the case first.

How: name the files in the problem so the panel quotes them at fixed line numbers; keep the first pass blind;
`--fix` only after the cause is agreed. Verify the patch locally before anyone applies it.

Agreement: **confirm** — the card shows roles, the files that will be quoted, whether `--fix` is on, and the budget;
one word starts it. Escalate to negotiate when the symptom itself is disputed (then it is a design decision).
