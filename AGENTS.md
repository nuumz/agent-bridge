# agent-bridge — notes for coding agents working in this repository

You are editing the bridge itself, not using it. Read [README.md](README.md) for what the commands do,
[SHARED-RULES.md](SHARED-RULES.md) for the protocol they implement, and [CONTRIBUTING.md](CONTRIBUTING.md) for the
change checklist. This file is the short version.

## Layout

- `bin/` — one executable per command, Python 3.11+, **standard library only**. `bin/bridgelib/` is the shared library:
  `core` (paths, constants, hop guard, quota), `adapters` (one class per peer CLI), `runner` (execute a peer run, threads,
  board registration), `ledger` (outcome ledger, playbook loader), `grill`, `evidence`, `workskills`. `bin/boardlib.py` is
  the SQLite board behind `bridge-board`.
- `roles/`, `playbooks/`, `work-skills/` — markdown with a flat `key: value` frontmatter; loaded at run time, no code change
  needed to add one. `agents/` — Claude Code subagent wrappers (`peer-*`), symlinked into `~/.claude/agents/` by `bridge-sync`.
- `.claude-plugin/` + `hooks/` — the Claude Code plugin, marketplace and hook manifests (`claude plugin validate .` must
  pass). Two warnings are expected: no `version` (on purpose — a pinned version would freeze plugin users on a cached copy
  until someone bumps it; without one the plugin tracks the commit) and `CLAUDE.md` at the root (it is for contributors'
  agents, not plugin context).
- `tests/` — `unittest`, no third-party packages. Peer CLIs are stub scripts put first on `PATH`; `HOME` is a temp dir.
- Runtime state is **never** in the checkout: `~/.agent-bridge/state/` (git-ignored). Tests must not read or write it.

## Working rules

- Run `python3 -m unittest discover -s tests` before and after a change; every command must still answer `--help`.
- A behaviour change in `bin/bridgelib/` or in a pipeline gate (`bridge-work`, `bridge-fix`, `bridge-panel`) comes with a
  test that is red without it. Exercise failure paths through a stub peer binary, never a real CLI (cost, egress).
- A change to an exit code, a rule, a prompt protocol string or a file layout updates `SHARED-RULES.md` in the same change.
- Do not add a dependency, a build step, a daemon, or a second place for state. Extend the existing adapter / loader before
  adding a layer; see "Adding a peer adapter" in CONTRIBUTING.md for the contract.
- Scripts stay LF and executable. Commits follow Conventional Commits and are never made without the maintainer asking.
- Never run `bridge-sync` (other than `--help`, which exits before any write) or a real `ask-agent` call as part of a
  test: both write to the user's `~/.cursor`, `~/.copilot` and `~/.claude` configuration or send data to a vendor.
