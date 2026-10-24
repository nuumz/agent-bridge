## What and why

<!-- one paragraph; link the issue if there is one -->

## Checklist

- [ ] `python3 -m unittest discover -s tests` passes; every command still answers `--help`
- [ ] a behaviour change has a test that is red without it, exercised through a stub peer binary (no real CLI call)
- [ ] exit codes, rules, protocol strings or file layout changed → `SHARED-RULES.md` updated in this PR
- [ ] standard library only; no new state location outside `~/.agent-bridge/state/`
- [ ] scripts stay LF and executable; commits follow Conventional Commits
