---
name: design-decision
summary: decide between design options, review a proposed contract / persistence shape, or brainstorm a flow before it is built
level: negotiate
grill: recommend
triggers: "DECISION UNDER DEBATE|option [AB12]|which (option|design|shape)|brainstorm|design review|contract (review|for)|should .* (stay|be) (PROPOSED|the)|ควรเป็น"
form: panel
peers: claude,cursor,copilot
panel_roles: as-built-realist,design-critic,reliability-critic,simplicity-advocate
optional_roles: spec-reader,contract-checker
mode: ask
inputs: the question in one sentence · the options already on the table · the files that define today's shape · the invariant that must hold · who decides
output: "decision table (question · options · recommendation · if undecided) + what a developer must write first per option; recorded as `peer review: panel <name>, <date>` in the decision row"
budget: "4 roles + cross ≈ 8–12 min, $6–12; case file mandatory (>60 KB of sources → split the question)"
---
Evidence: 8 panels (subacct1→2, payment-core-0919→b, bc-teller-flow, jp-identity, jp-identity-refactor,
mf-offsite-readiness, cr219-signers). The only panels ever cited in the specs are of this kind — subacct2 (1),
payment-core-b (2), bc-teller-flow (3) — and the two that failed first (subacct1, payment-core-0919) were re-run after
the question was rewritten and a case file added. `simplicity-advocate` + `reliability-critic` were added on 09-15 for
exactly this job (argue the rejected option; what breaks when the extra call is down).

How: write the agreement first — the question, the options (never let the panel invent option C silently), the
invariant, who decides, the output shape — and embed it in the case file so every role argues the same question.
Then the panel; then the human fills the decision row and cites the panel.

Agreement: **negotiate** — this is the one kind where the framing decides the answer, so the framing is agreed
before any peer runs. The bridge refuses to run a design-decision panel while the agreement still has TODO fields.
