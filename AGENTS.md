# agent-bridge — notes for coding agents working in this repository

You are editing the bridge itself, not using it. Read [README.md](README.md) for what the commands do,
[SHARED-RULES.md](SHARED-RULES.md) for the protocol they implement, and [CONTRIBUTING.md](CONTRIBUTING.md) for the
change checklist. This file is the short version.

## Layout

- `bin/` — one executable per command, Python 3.11+, **standard library only**. `bin/bridgelib/` is the shared library:
  `core` (paths, constants, hop guard, quota), `adapters` (one class per peer CLI), `runner` (execute a peer run, threads,
  board registration), `ledger` (outcome ledger, playbook loader), `grill`, `evidence`, `workskills`, `protocol` (the
  message envelope of `docs/PROTOCOL.md`: export and schema check), `a2a` (the A2A transport behind `bridge-a2a`: the
  mapping of an A2A task onto a board request, the server handler, the client). `bin/boardlib.py` is the SQLite board
  behind `bridge-board`.
- `roles/`, `playbooks/`, `work-skills/` — markdown with a flat `key: value` frontmatter; loaded at run time, no code change
  needed to add one. `work-skills.local/` is git-ignored but loaded too, so a test that passes only because of a local
  file there is a CI failure. `agents/` — Claude Code subagent wrappers (`peer-*`), symlinked into `~/.claude/agents/` by `bridge-sync`.
- `.claude-plugin/` + `hooks/` — the Claude Code plugin, marketplace and hook manifests (`claude plugin validate .` must
  pass). Two warnings are expected: no `version` (on purpose — a pinned version would freeze plugin users on a cached copy
  until someone bumps it; without one the plugin tracks the commit) and `CLAUDE.md` at the root (it is for contributors'
  agents, not plugin context).
- `tests/` — `unittest`, no third-party packages. Peer CLIs are stub scripts put first on `PATH`; `HOME` is a temp dir.
  `tests/fakebin/` holds one stub per peer binary (driven by `FAKEBIN_*` variables, replaying `fixtures/<binary>.jsonl`);
  `tests/test_adapter_conformance.py` runs every adapter through them — a new adapter is done when it passes there.
- Runtime state is **never** in the checkout: `~/.agent-bridge/state/` (git-ignored). Tests must not read or write it.
  State paths are import-time constants, so patching `HOME` in a test does nothing: patch the constant the code reads
  (`ledger.LEDGER`, `boardlib.BOARD_DIR` / `DB_PATH`, `runner.CONV_DIR`, `core.STATE`) and give any subprocess its own
  `HOME`. CI fails when the suite leaves anything under the runner's `~/.agent-bridge`.

## Working rules

- Run `python3 -m unittest discover -s tests` before and after a change; every command must still answer `--help`.
- A behaviour change in `bin/bridgelib/` or in a pipeline gate (`bridge-work`, `bridge-fix`, `bridge-panel`) comes with a
  test that is red without it. Exercise failure paths through a stub peer binary, never a real CLI (cost, egress).
- A change to an exit code, a rule, a prompt protocol string or a file layout updates `SHARED-RULES.md` in the same change.
  A change to the message envelope — a field, a kind, a header key, the identity grammar, who may close a request —
  updates `docs/PROTOCOL.md`, `docs/protocol/envelope.schema.json` and `docs/protocol/vectors.json` too;
  `tests/test_protocol.py` holds them to the board and fails on drift.
- Do not add a dependency, a build step, a daemon, or a second place for state. Extend the existing adapter / loader before
  adding a layer; see "Adding a peer adapter" in CONTRIBUTING.md for the contract.
- Scripts stay LF and executable. Commits follow Conventional Commits and are never made without the maintainer asking.
- Never run `bridge-sync` (other than `--help`, which exits before any write) or a real `ask-agent` call as part of a
  test: both write to the user's `~/.cursor`, `~/.copilot` and `~/.claude` configuration or send data to a vendor.
