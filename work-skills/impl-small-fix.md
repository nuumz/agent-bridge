---
name: impl-small-fix
summary: smallest safe bugfix — no drive-by refactor; prove with a failing check when possible
triggers: "\\bfix\\b|bugfix|regression|null pointer|NPE|hotfix|แก้บั[คก]|defect"
verify_hint: "the test or command that failed before the fix; must pass after"
---
### implementer
- Change only what the defect requires — no rename, no drive-by cleanup, no new abstraction.
- Prefer a regression test (or the named failing command) that is red without the fix and green with it.
- Correct comments/docstrings the change makes false in the same diff.
- If the root cause is still unclear, stop under OPEN — do not guess a second fix.

### reviewer
- must-fix=yes for extra refactors, speculative null-checks unrelated to the cause, or a fix with no way to observe it.
- Confirm the verify command actually exercises the fixed path (not a neighbouring green suite).
- out-of-agreement if files outside the defect's blast radius were touched without need.
