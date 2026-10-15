---
name: implement
summary: let a peer write code or tests on an enumerated file set, in an isolated worktree
level: negotiate
grill: recommend
triggers: "implement|write (the )?(code|patch|tests?)|write .*tests?|เขียน ?tests?|junit|touch only|-m write|แก้โค้ด|เขียน(เทส|โค้ด)"
form: pipeline
peers: claude
alt_peers: cursor
mode: write
command: bridge-work run <name> -d <repo> -p claude[,cursor] --files '<globs>' --verify '<cmd>' "<task>"
agent: peer-work
inputs: exact file set (`touch only …`) · worktree name · the spec or failing test · build/test command · what must not change
output: "branch bridge/<name> in <repo>/.claude/worktrees/ + work/<name>/report.md (diff stat · verify output · reviewer table · files vs allowed); the user reads `git diff` and commits"
budget: "≈ 5–15 min, $2–8; one peer, never two on the same files"
---
Evidence: 2 direct runs in the window (both claude `-m write -w`); `jp-identity-refactor` was the one panel about
implementation shape and it stayed read-only. Every write delegation in the rules is paired with `-w` and an
explicit file list; a stub or a mint-ahead identifier is not allowed (scaffold discipline).

How: `bridge-work run` is the whole pipeline — worktree → implementer writes (`-m write`, build/test runners
pre-approved) → the verify command runs by script → reviewer peer over the diff → must-fix / red verify go back, at
most `--rounds` times → report. Without `--files` + `--verify` it stops with an agreement card (exit 3): that is the
negotiate gate — fill the card with the user, then run. Commit and merge stay with the user; the bridge never commits.

Agreement: **negotiate** — writes are the only bridged action that can cost someone else's work.
