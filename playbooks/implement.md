---
name: implement
summary: let a peer write code or tests on an enumerated file set, in an isolated worktree
level: negotiate
grill: recommend
triggers: "implement|write (the )?(code|patch|tests?)|write .*tests?|เขียน ?tests?|junit|touch only|-m write|แก้โค้ด|เขียน(เทส|โค้ด)"
form: pipeline
peers: claude
alt_peers: cursor,copilot
mode: write
command: bridge-work run <name> -d <repo> -p claude[,cursor] --files '<globs>' --verify '<cmd>' "<task>"
agent: peer-work
inputs: exact file set (`touch only …`) · worktree name · the spec or failing test · build/test command · what must not change
output: "branch bridge/<name> in <repo>/.claude/worktrees/ + work/<name>/report.md (diff stat · verify output · reviewer table · files vs allowed); the user reads `git diff` and commits"
budget: "≈ 5–15 min, $2–8; one peer, never two on the same files"
---
Evidence: 2 direct runs in the window (both claude `-m write -w`); the one panel about implementation shape
stayed read-only. Every write delegation in the rules is paired with `-w` and an
explicit file list; a stub or a mint-ahead identifier is not allowed (scaffold discipline).

How: `bridge-work run` is the whole pipeline — worktree → implementer writes (`-m write`, build/test runners
pre-approved) → the verify command runs by script → reviewer peer over the diff → must-fix / red verify go back, at
most `--rounds` times → report. Without `--files` + `--verify` it stops with an agreement card (exit 3): that is the
negotiate gate — fill the card with the user, then run. Commit and merge stay with the user; the bridge never commits.

Agreement: **negotiate** — writes are the only bridged action that can cost someone else's work.

Copilot can be the implementer, not only a reviewer. Select it explicitly, for example
`bridge-work run <name> -d <repo> -p copilot,claude --implementer copilot --reviewer claude --files '<globs>' --verify '<cmd>' "<task>"`.
The task must include the failing behavior or requested improvement, source files, constraints and acceptance cases.
Use the existing work-skill pack when applicable; do not send a broad "improve everything" request. The pipeline
provides the isolated worktree and runs verification independently of the implementer's claims. Read-only review
cannot run shell commands under Copilot; include the diff and verification output in the case, as bridge-work does.
Compare verified findings (`bridge-plan mark`) and work READY results with duration/usage (`bridge-plan learn`);
rc=0 and a large answer are not evidence of useful work. Keep the default peer until measured outcomes justify changing it.
