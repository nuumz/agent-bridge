# agent-bridge

[![test](https://github.com/nuumz/agent-bridge/actions/workflows/test.yml/badge.svg)](https://github.com/nuumz/agent-bridge/actions/workflows/test.yml)

Let the coding agents on one machine — **Claude Code**, **Cursor Agent**, **GitHub Copilot CLI** and **OpenAI Codex
CLI** — call each other, share a message board, and run multi-agent reviews and fixes. The bridge wraps those CLIs; it
is not a new runtime.

> **Document boundary.** This README owns install, first-run, and the command map.
> Protocol (exit codes, hop guard, egress, session cards): [SHARED-RULES.md](SHARED-RULES.md).
> The message layer agents use to talk across vendors (identities, envelope, kinds, request lifecycle), with a JSON
> Schema and test vectors: [docs/PROTOCOL.md](docs/PROTOCOL.md).
> What leaves the machine and which rail is enforced where: [SECURITY.md](SECURITY.md).
> How it compares with other multi-agent tools: [docs/landscape.md](docs/landscape.md).
> Changing the code: [CONTRIBUTING.md](CONTRIBUTING.md) (agents: [AGENTS.md](AGENTS.md)).

- `ask-agent claude|cursor|copilot|codex|a2a` — one command, one set of options (mode, model, dir, session, worktree, timeout)
  mapped onto each CLI's own flags; the answer comes back as text plus a `[bridge]` line (session id, tokens, log path).
- `bridge-panel` — a leader plus role peers (root-cause, contract-checker, design-critic, …) answer in parallel,
  cross-examine each other's citations, and the leader writes one report.
- `bridge-work` / `bridge-fix` — implement or fix in an isolated git worktree: write → run your verify command →
  a second peer reviews the diff → fix loop → report. The branch is the deliverable; nothing is ever committed.
- `bridge-board` — a SQLite-backed board and task queue, exposed to every agent as an MCP server.

## Why this and not a GUI, an MCP model gateway, or a swarm framework

- **Nothing to run.** No daemon, database, tmux or desktop app: one git clone, Python's standard library, and the CLIs
  you already have. Orchestrator GUIs come and go; a CLI wrapper survives as long as the CLIs do.
- **The subscriptions you already pay for.** No API keys and no credits: each peer runs under its own login, so a
  second opinion from another model family costs what that plan already costs.
- **Rails first, then evidence.** Read-only by default, no commit ever, one hop, and per-vendor consent before a repo
  is shared. A finding without `file:line` is marked UNVERIFIED instead of being averaged into a "consensus".

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
  - [OpenAI Codex CLI](https://github.com/openai/codex) — `codex` (adapter written from the CLI's source and passing
    the conformance kit; not yet verified against a real `codex` run — see SHARED-RULES.md §2)
- Optional: `tmux` for `bridge-watch --tmux`

## Install

Two ways; runtime state goes to `~/.agent-bridge/state/` either way.

**From Claude Code, as a plugin** (one step on Claude Code 2.1.275 or later; Copilot CLI and Codex read the same
marketplace file):

```
/plugin install agent-bridge --marketplace nuumz/agent-bridge
```

The plugin puts `bin/` on the PATH of Claude's Bash tool, ships the `peer-*` subagents (as `agent-bridge:peer-ask` and
so on) and a `UserPromptSubmit` hook that shows open cards. Then run `bridge-sync` once from that Bash tool to mirror
your rules into Cursor/Copilot and register the board MCP; run it again after a plugin update (it re-points the MCP
entry at the new plugin directory). For `ask-agent` from your own terminal, add the plugin's `bin/` to your shell PATH
or use the clone below.

**As a clone** (any shell, contributors):

```bash
git clone https://github.com/nuumz/agent-bridge.git ~/.agent-bridge
echo 'export PATH="$HOME/.agent-bridge/bin:$PATH"' >> ~/.zshrc   # or ~/.bashrc
exec $SHELL             # and restart any running Claude Code session — its Bash tool keeps the old PATH
ask-agent --list        # which peers are installed and logged in
bridge-sync             # wire the peers together (below)
```

`bridge-sync` is idempotent and safe to run from a SessionStart hook. It never edits your Claude-side sources, and it
leaves alone any peer file that does not carry its `GENERATED` marker. What it writes:

| Target | What |
|---|---|
| `~/.agent-bridge/state/mirror/` | your `~/.claude/CLAUDE.md` (plus `@`-imports, minus Claude-only parts) and the protocol note for peers |
| `~/.cursor/rules/00-agent-bridge.mdc` | the bridge note for Cursor (how to reach a peer, one hop, board pull); written even without a `CLAUDE.md` |
| `~/.cursor/rules/`, `~/.copilot/instructions/` | the same rules, in each peer's format (Copilot's file carries the bridge note inline) |
| `~/.cursor/skills-cursor/`, `~/.copilot/skills/` | symlinks to your `~/.claude/skills/*` |
| `~/.claude/agents/` | symlinks to [`agents/`](agents) — `peer-ask`, `peer-panel`, `peer-work`, `peer-fix` subagents (skipped under a plugin install, which ships them itself) |
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

### When a peer needs more information

A bridged run never has the caller's conversation, so the protocol tells every peer to ask for one concrete fact it
cannot read itself rather than guess, and never to stall on a checklist. Three channels, from cheapest to last resort:

| Channel | How the peer uses it | How you (or the calling agent) answer |
|---|---|---|
| Live question while it runs | `board_ask_user` (a fact card) on the thread's topic, then `board_wait` | `bridge-board cards` shows it as seq N; `bridge-board post conv:<name> "<answer>" -k answer --reply-to N` — the run continues and the card closes |
| Ask-back at the end | the reply ends with one line `[[ASK]] <question>`; `ask-agent` exits **5** and prints how to resume | `ask-agent <peer> --conv <name> "<answer>"` (or `--session <id>` for a run without a thread) — same session, nothing is re-sent |
| A decision only a human can make | a **card** (`board_ask_user`): question · options · recommendation · default if nobody answers | `bridge-board cards` lists every open card; `bridge-board post <topic> "…" -k decision --reply-to N` |

`bridge-fix` stops at the same kind of card before it changes anything (exit 3), and every `bridge-panel` report carries
a **Voices** line right after its header — who answered, who answered through a fallback peer, who failed, who asked
back — so a report never silently drops a role (`status.json` carries the same as `degraded: true`). Over time
`bridge-plan learn` shows which role/peer asks back most — a sign that the brief, not the peer, needs work.

To see open cards at your next prompt instead of in a log: the plugin install ships this hook; on a clone install add a
`UserPromptSubmit` hook to `~/.claude/settings.json` with the absolute path (hooks do not get the Bash tool's PATH). It
prints nothing when nothing is waiting, and it quotes a peer's question as data, never as an instruction:

```json
{ "hooks": { "UserPromptSubmit": [ { "hooks": [ { "type": "command", "command": "~/.agent-bridge/bin/bridge-board cards --brief" } ] } ] } }
```

### Giving Copilot a complete task

Start in the target repo (`-d`), not the bridge installation directory. A Copilot run gets a workspace bootstrap
in its saved prompt: the actual working directory, nearest repo root, existing root-to-cwd instruction files,
explicit access roots and the mode's tool limits. Copilot still discovers personal instructions and skills itself;
the inventory does not prove instructions loaded. Check discovery locally with `copilot -C <repo> instruction list`
on CLI versions supporting that command. No generated context directory, automatic folder trust or global path
permission is added. In read-only runs (`ask`/`plan`) the git repo of each absolute path the prompt cites under HOME
is granted automatically and printed as `[bridge] add-dir (cited in prompt)`. For write runs, non-git directories and
paths outside HOME, grant the root yourself with `--add-dir`. Copilot also loads a granted root's `.github/skills` and
`.github/agents`, so cite or grant only repos whose config you trust. `bridge-panel`, `bridge-work` and `bridge-fix`
take the same `--add-dir` and pass it to every peer run they start.

Give analysis tasks a bounded question, source paths/logs, expected versus actual behavior and the output needed
(evidence, root cause, smallest safe fix, remaining uncertainty). In `ask`/`plan`, shell and writes stay denied:
native search/read tools work inside permitted roots, but git/build/test evidence must be supplied by the caller.
The bootstrap directs Copilot not to retry denied tools or guess paths, and to ask back if essential access is missing.

For implementation or a targeted improvement, use the existing isolated pipeline:

```bash
bridge-work run mapper-fix -d ~/src/myrepo -p copilot,claude --implementer copilot --reviewer claude \
  --files 'src/mapper.py,tests/test_mapper.py' --verify 'python3 -m unittest discover -s tests' \
  "Fix the mapper's empty-input regression; add a regression test and preserve other mappings."
```

The script runs verification and an independent review before reporting READY; nothing is committed. Peer
selection remains explicit, and the default does not change to Copilot. Use `bridge-plan learn` to compare task/mode,
API duration, reported premium usage and tool failures; missing historical values stay unknown. Premium usage is
the CLI's reported value, not a dollar estimate or a quality score. Use `bridge-plan mark <run-log> --kept N --rejected M`
after verifying analysis findings, and the work report's verify/review results for implementation quality.

### Across machines (A2A)

The bridge can be one [A2A](https://a2a-protocol.org) agent and can call one. Nothing runs by itself: an inbound task
becomes a request on the board, addressed to a family you allowed, and waits until you run `bridge-board dispatch`.

```bash
# on the machine that answers
bridge-a2a client add laptop          # prints a bearer token once — give it to the other side
bridge-a2a serve --peers claude,cursor -d ~/src/myrepo        # loopback only; reach it through an SSH tunnel
bridge-board dispatch --agents claude,cursor                  # runs what arrived, like any queued turn

# on the machine that asks
export AGENT_BRIDGE_A2A_URL=http://127.0.0.1:8411 AGENT_BRIDGE_A2A_TOKEN=…   # the tunnel's local end
ask-agent a2a -d ~/src/myrepo "Review src/payment/ for retry bugs. Answer as file:line findings."
```

A client sees only its own topic, never chooses the directory, and gets `write` only if the server was started with
`--allow-write`. The protocol mapping is in [docs/PROTOCOL.md](docs/PROTOCOL.md) §10.4, the network surface in
[SECURITY.md](SECURITY.md).

## Commands

| Command | Purpose |
|---|---|
| `ask-agent`, `ask-claude`, `ask-cursor`, `ask-copilot`, `ask-codex` | run one task on a peer; `--conv` threads, `-w` worktree, `--dry-run` |
| `bridge-plan` | pick a playbook for a request, prepare its session card, learn from the outcome ledger |
| `bridge-panel` | role-based multi-peer review with a leader and cross-examination |
| `bridge-work` | implement → verify → review → fix loop in an isolated worktree; the reviewer's `BRIDGE_VERDICT=APPROVE\|REVISE` line decides the loop |
| `bridge-fix` | analyze → human decision → fix, as one resumable flow |
| `bridge-wait` | one capped wait for a detached panel / work / fix run |
| `bridge-board` | board + task queue: MCP server, CLI, `dispatch` (the only spawner) |
| `bridge-live` | persistent Claude peers that talk over the board |
| `bridge-watch` | live view of every bridged run |
| `bridge-sync` | mirror rules, skills, subagents and the board MCP into every peer |
| `bridge-a2a` | across machines: `serve` the bridge as an A2A agent (off by default, loopback + bearer tokens); `send` a task to any A2A agent, which is what `ask-agent a2a` runs |

Every command takes `--help`.

## Repository layout

```
bin/            executables; bin/bridgelib/ is the shared library (core, adapters, runner, ledger, grill, workskills, protocol)
agents/         Claude Code subagent wrappers, linked into ~/.claude/agents/ by bridge-sync
roles/          panel personas (root-cause, contract-checker, design-critic, …)
playbooks/      job kinds for bridge-plan: default roles, peers, session depth
work-skills/    capability packs injected into bridge-work prompts (impl-test, impl-mapper, …)
tests/          unit tests (standard-library unittest)
docs/           the message protocol (PROTOCOL.md + protocol/ schema and vectors), landscape, roadmap
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
