# Contributing

## Setup

Clone to `~/.agent-bridge` (see the [README](README.md#install)). No dependencies beyond Python 3.11+ and `git`.

## Before opening a pull request

```bash
python3 -m unittest discover -s tests
```

- Standard library only — the bridge must run on a fresh machine with nothing to install.
- Add a test for every branch you add or change in `bin/bridgelib/` or a pipeline gate; tests must not touch
  `~/.agent-bridge` (use a temporary directory, as the existing tests do).
- Failure paths are exercised with stub `claude` / `cursor-agent` / `copilot` scripts put first on `PATH` — no real
  peer call, no cost.
- A change to a rule, exit code or file layout updates [SHARED-RULES.md](SHARED-RULES.md) in the same pull request.
- Scripts stay LF (`.gitattributes` enforces it) and executable (`git update-index --chmod=+x`).

## Commits

[Conventional Commits](https://www.conventionalcommits.org/): `feat|fix|test|refactor|docs|chore(scope): …`,
one logical change per commit.
