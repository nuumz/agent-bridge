# agent-bridge

[![test](https://github.com/nuumz/agent-bridge/actions/workflows/test.yml/badge.svg)](https://github.com/nuumz/agent-bridge/actions/workflows/test.yml)

Let the coding agents on one machine — **Claude Code**, **Cursor Agent**, and **GitHub Copilot CLI** — call each other,
share a message board, and run multi-agent reviews and fixes. The bridge wraps those CLIs; it is not a new runtime.

> **Document boundary.** This README owns install, first-run, and the command map.
> Protocol (exit codes, hop guard, egress, session cards): [SHARED-RULES.md](SHARED-RULES.md).
> Changing the code: [CONTRIBUTING.md](CONTRIBUTING.md).

- `ask-agent claude|cursor|copilot` — one command, one set of options (mode, model, dir, session, worktree, timeout)
  mapped onto each CLI's own flags; the answer comes back as text plus a `[bridge]` line (session id, tokens, log path).
- `bridge-panel` — a leader plus role peers (root-cause, contract-checker, design-critic, …) answer in parallel,
  cross-examine each other's citations, and the leader writes one report.
- `bridge-work` / `bridge-fix` — implement or fix in an isolated git worktree: write → run your verify command →
  a second peer reviews the diff → fix loop → report. The branch is the deliverable; nothing is ever committed.
- `bridge-board` — a SQLite-backed board and task queue, exposed to every agent as an MCP server.

## Safety rails

| Rail | How it is enforced |
|---|---|
| Read-only by default | `-m ask` / `-m plan` map to each CLI's read-only mode; only `-m write` may touch files |
| No commit / push / tag | denied on every peer that supports a deny list, through that list |
| One hop only | the peer gets `AGENT_BRIDGE_HOP=1`; `ask-agent` and every pipeline that spawns peers refuse to run under it |
| Isolated writes | `bridge-work` writes in `<repo>/.claude/worktrees/bridge-<name>` on branch `bridge/<name>` |
| Quota fail-fast | a peer that reports an exhausted plan is blocked for 30 min instead of failing run after run (exit 7) |

**Data egress:** every bridged call sends the prompt and every file the peer reads to that peer's vendor. Decide per
vendor what your code may be shared with — `bridge-panel`, `bridge-work` and `bridge-fix` take `-p` as the list of
peers allowed to see the repo.

## Requirements

- macOS or Linux (uses `fcntl` and process groups), `git`, Python 3.11+ — standard library only, nothing to `pip install`
- At least one peer CLI, installed and logged in:
  - [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — `claude`
  - [Cursor CLI](https://docs.cursor.com/en/cli/overview) — `cursor-agent`
  - [GitHub Copilot CLI](https://github.com/github/copilot-cli) — `copilot`
- Optional: `tmux` for `bridge-watch --tmux`

## Install

The bridge lives at `~/.agent-bridge` — the subagent wrappers and the peer prompts refer to that path, and runtime state
is written there.

```bash
git clone https://github.com/nuumz/agent-bridge.git ~/.agent-bridge
echo 'export PATH="$HOME/.agent-bridge/bin:$PATH"' >> ~/.zshrc   # or ~/.bashrc
exec $SHELL
ask-agent --list        # which peers are installed and logged in
bridge-sync             # wire the peers together (below)
```

`bridge-sync` is idempotent and safe to run from a SessionStart hook. It never edits your Claude-side sources, and it
leaves alone any peer file that does not carry its `GENERATED` marker. What it writes:

| Target | What |
|---|---|
| `~/.agent-bridge/state/mirror/` | your `~/.claude/CLAUDE.md` (plus `@`-imports, minus Claude-only parts) and the protocol note for peers |
| `~/.cursor/rules/`, `~/.copilot/instructions/` | the same rules, in each peer's format |
| `~/.cursor/skills-cursor/`, `~/.copilot/skills/` | symlinks to your `~/.claude/skills/*` |
| `~/.claude/agents/` | symlinks to [`agents/`](agents) — `peer-ask`, `peer-panel`, `peer-work`, `peer-fix` subagents |
| MCP `bridge_board` | registered for Claude (`claude mcp add -s user`) and Cursor (`~/.cursor/mcp.json`) |

## Quick start

```bash
# a second opinion from another model family, read-only
ask-cursor -d ~/src/myrepo "Review the diff in src/payment/ for retry bugs. Answer as file:line findings."

# keep talking to the same peer session
ask-agent claude --conv design-x "Propose a schema for ..."
ask-agent claude --conv design-x "What breaks if two writers race?"

# a panel on a defect, detached, then wait for it
bridge-panel run rc-timeout -p claude,cursor -r root-cause,contract-checker -d ~/src/myrepo --detach "<problem>"
bridge-wait panel rc-timeout
bridge-panel show rc-timeout

# let the bridge implement something on its own branch
bridge-work run add-mapper-test -d ~/src/myrepo -p claude --files 'src/test/**' --verify 'make test' "<task>"

# watch every running peer live
bridge-watch
```

Inside Claude Code, the `peer-*` subagents run the same commands in the background and relay the result verbatim.

## Commands

| Command | Purpose |
|---|---|
| `ask-agent`, `ask-claude`, `ask-cursor`, `ask-copilot` | run one task on a peer; `--conv` threads, `-w` worktree, `--dry-run` |
| `bridge-plan` | pick a playbook for a request, prepare its session card, learn from the outcome ledger |
| `bridge-panel` | role-based multi-peer review with a leader and cross-examination |
| `bridge-work` | implement → verify → review → fix loop in an isolated worktree |
| `bridge-fix` | analyze → human decision → fix, as one resumable flow |
| `bridge-wait` | one capped wait for a detached panel / work / fix run |
| `bridge-board` | board + task queue: MCP server, CLI, `dispatch` (the only spawner) |
| `bridge-live` | persistent Claude peers that talk over the board |
| `bridge-watch` | live view of every bridged run |
| `bridge-sync` | mirror rules, skills, subagents and the board MCP into every peer |

Every command except `bridge-sync` takes `--help`.

## Repository layout

```
bin/            executables; bin/bridgelib/ is the shared library (core, adapters, runner, ledger, grill, workskills)
agents/         Claude Code subagent wrappers, linked into ~/.claude/agents/ by bridge-sync
roles/          panel personas (root-cause, contract-checker, design-critic, …)
playbooks/      job kinds for bridge-plan: default roles, peers, session depth
work-skills/    capability packs injected into bridge-work prompts (impl-test, impl-mapper, …)
tests/          unit tests (standard-library unittest)
SHARED-RULES.md the protocol every peer follows
```

Runtime state lives in `~/.agent-bridge/state/` — git-ignored, stays on this machine: `runs/` (every prompt and
transcript), `panel/`, `work/`, `conv/`, `live/`, `board/`, `mirror/`, `quota/`, `ledger.jsonl`. Put machine-local
skill packs in `work-skills.local/` (also ignored).

## Configuration

| Variable | Effect |
|---|---|
| `AGENT_BRIDGE_LANG` | reply language when no request text is at hand (default `English`); otherwise the reply follows the request — Thai script → Thai, anything else → English |
| `BRIDGE_DOC_ROOTS` | `os.pathsep`-separated markdown trees that `bridge-plan learn` scans for peer-review rows |

## Development

```bash
python3 -m unittest discover -s tests
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
