---
name: impl-test
summary: write or extend automated tests — behaviour through public API, no mock-only green
triggers: "Test\\.java|_test\\.py|\\.test\\.(ts|tsx|js)|bun test|pytest|@Test|เขียน ?tests?|junit|vitest|jest|เพิ่ม ?เทส|เขียนเทส"
paths: "src/test/|/tests?/|Test\\.(java|ts|tsx|js)$|_test\\.py$|\\.test\\.(ts|tsx|js)$"
verify_hint: "mvn -o -q test -Dtest=<Name> | bun test <path> | pytest -q <path>"
---
### implementer
- Cover meaningful behaviour (branch, mapping, regression) — skip DTO/getter/existence-only asserts.
- Prefer the real client/decoder path for error fixtures; do not invent HTML bodies for a JSON-only host.
- Name the test after the behaviour; one logical case per test method when practical.
- If the task names a failing test, make that test fail without the fix and pass with it (prove the gate).

### reviewer
- Flag tests that only assert mocks were called, or that invent fixture shapes the contract never returns.
- must-fix=yes if the new test cannot fail when the production bug is reintroduced.
- out-of-agreement if the implementer edited production code when the task was test-only (unless the task said so).
