# Contributing

## Setup

Clone to `~/.agent-bridge` (see the [README](README.md#install)). No dependencies beyond Python 3.11+ and `git`.

## Before opening a pull request

```bash
python3 -m unittest discover -s tests
# once more the way CI sees it: a fresh clone and an empty HOME
git clone -q . /tmp/ab && HOME=$(mktemp -d) python3 -m unittest discover -s /tmp/ab/tests
```

- Standard library only — the bridge must run on a fresh machine with nothing to install.
- Add a test for every branch you add or change in `bin/bridgelib/` or a pipeline gate; tests must not touch
  `~/.agent-bridge` — state paths are import-time constants, so patch the one the code reads (`ledger.LEDGER`,
  `boardlib.BOARD_DIR` / `DB_PATH`, `runner.CONV_DIR`), not `HOME`; CI fails on anything left under the runner's
  `~/.agent-bridge`.
- Failure paths are exercised with stub `claude` / `cursor-agent` / `copilot` scripts put first on `PATH` — no real
  peer call, no cost.
- A change to a rule, exit code or file layout updates [SHARED-RULES.md](SHARED-RULES.md) in the same pull request.
- Scripts stay LF (`.gitattributes` enforces it) and executable (`git update-index --chmod=+x`).

## Commits

[Conventional Commits](https://www.conventionalcommits.org/): `feat|fix|test|refactor|docs|chore(scope): …`,
one logical change per commit.

## Adding things

Roles, playbooks and work-skills are markdown files with a flat `key: value` frontmatter (one line per key, no
nesting). In playbooks and work-skills a quoted value is read JSON-style — the quotes go and `\\b` reaches the regex as
`\b`; roles keep values verbatim. They are read at run time — no code change, no registry.

| Kind | Directory | Required keys | Optional keys | Body |
|---|---|---|---|---|
| Panel role | `roles/` | `name`, `summary`, `peer` | `model`, `mode`, `fallback_peer`, `timeout` (seconds, only ever raises `-t`) | the persona prompt; demand `file:line` or `UNVERIFIED` |
| Playbook | `playbooks/` | `name`, `summary`, `triggers` (regex scored against the request), `level` (`routine` / `confirm` / `negotiate` — anything else is a KeyError in `bridge-plan`) | `form` (direct / panel / pipeline / fix-flow), `grill`, `peers`, `alt_peers`, `mode`, `panel_roles`, `optional_roles`, `command`, `agent`, `level_when_adjudicating`, `inputs`, `output`, `budget` | when and how to run it; evidence for the defaults |
| Work-skill | `work-skills/` | `name`, `summary`, `triggers` | `paths` (regex on the `--files` set), `verify_hint` | `### implementer` and `### reviewer` sections, injected into those prompts |

Keep a new file in the same shape as its neighbours and add a row to the matching table in `SHARED-RULES.md`.

### Adding a peer adapter

A peer is one class in `bin/bridgelib/adapters.py` registered in `ADAPTERS`. The contract (base class `Adapter`):

- `name`, `binary` — the CLI name and the executable `find_bin` looks for; `skip_bin_substr` skips look-alike shims.
- `argv(o, prompt)` / `resume_argv(o, session, prompt)` — map the common options (`mode`, `model`, `dir`, `add_dir`,
  `worktree`, `deny`, `board`) onto the CLI's flags. `ask` and `plan` must come out read-only through the CLI's own
  permission mechanism. When the CLI has a deny list (`supports_deny`), `write` denies `GIT_DENY` (commit/push/tag) and
  `BRIDGE_CMDS`; when it has none, say so in `SECURITY.md` — the prompt and the hop guard are then the only lines.
- `parse(raw)` → `Parsed(result, session, tok_in, tok_out, is_error, extra, metrics)` from the CLI's machine-readable
  output; `session` is what `--session` / `--conv` resume with; `metrics` (duration, premium requests, tool failures…)
  feeds the ledger.
- `prepare(o, dry)` / `finish(o)` — anything the run needs around it (linking rules into the workspace, holding a deny
  set); `finish` is idempotent and also runs from `atexit`. `preamble(o)` is text prepended to the prompt when a rules
  file cannot be injected. `auth_hint()` is the status column of `ask-agent --list`.
- Set `supports_worktree`, `supports_deny`, `recovery`, `rules_mirror` truthfully: `ask-agent --list` and the §2 table of
  `SHARED-RULES.md` are generated from them in spirit, not in code — update the table.

Then: a `PEERS` entry in `bin/bridge-sync` (rules file, header, skills dir) if the CLI reads user-level instructions;
`DISABLED` in `core.py` is where a machine switches a peer off. Tests go through a stub executable: a shell script named
like the binary, put first on `PATH`, that records its argv and prints a canned result in the CLI's real output format
— see `tests/test_copilot.py` and `tests/test_cursor_deny.py`. Cover: read-only flags for `ask`, the git denies for
`write`, `parse` on a real captured transcript, and one failure path (non-zero exit, quota message).
