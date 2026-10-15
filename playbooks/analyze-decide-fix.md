---
name: analyze-decide-fix
summary: a defect end-to-end — panel finds the cause, a human rules, the bridge implements on a branch
level: negotiate
grill: recommend
triggers: "analy[sz]e.*(decision|decide).*fix|root cause.*(then|and) fix|fix .* end.to.end|วิเคราะห์.*ตัดสิน.*แก้|analyze > decision > fix"
form: fix-flow
peers: claude
alt_peers: cursor,copilot
panel_roles: as-built-realist,spec-reader,design-critic
mode: ask
command: bridge-fix run <name> -d <repo> -p claude[,cursor] "<symptom · expected vs actual · files · verify command>"
agent: peer-fix
inputs: symptom (expected vs actual) · the files involved · the command that fails now · who rules on the options
output: "work/<name>/agreement.md (root cause · decision table · your ruling · files · verify) + branch bridge/fix-<name> with work/fix-<name>/report.md; nothing committed"
budget: "panel 5–8 min + fix 2–15 min, $6–15; the flow idles at `decide` for as long as the human takes"
---
Composed from two playbooks: `defect-root-cause` (panel with `--fix`, confirm) and `implement` (bridge-work, negotiate).
The seam between them is the decision: the panel's `Decisions for the user` table becomes the Ruling section of the
agreement, the script stops (exit 3), and only `bridge-fix decide` — an explicit human act, `--answer` per question
or `--go` for "every recommendation as written" — moves the flow to fix. Rationale: the one thing the ledger shows
panels never do well is decide (CLAUDE.md: Decide = main agent / user only), and the one thing bridge-work must not
guess is what "fixed" means (files + verify).

How: `bridge-fix run` → read `work/<name>/agreement.md` with the user (open every file:line the root cause cites) →
`bridge-fix decide <name> --answer '1=B' --files … --verify …` → read `work/fix-<name>/report.md`, `git diff` the branch
→ the user commits. `bridge-fix run <name>` resumes at whatever stage the flow is in.
