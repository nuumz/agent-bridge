# Roadmap

What agent-bridge should take from the projects in [landscape.md](landscape.md), filtered by the concept that does not
change: **wrap the CLIs the developer already has, add no runtime and no key, read-only by default, never commit, one
hop, per-vendor consent, `file:line` or UNVERIFIED.** Items are ordered by value per effort; sizes are S (a day),
M (a few days), L (more). Nothing here is scheduled until it is picked up in an issue.

## Guiding principle: a bridged peer may ask, and should not be boxed in

A bridged run starts without the caller's conversation and often without the data it needs. The protocol therefore
tells a peer to read first, then ask for the one fact it cannot obtain, and never to stall on a checklist or guess at a
decision that belongs to the user (README → "When a peer needs more information"). Every item below keeps that path
open and makes the ask cheaper to see and to answer; none adds a rule that a peer must satisfy before it may ask.

## Now (S) — correctness and trust of what already exists

| # | Item | Borrowed from | What it fixes |
|---|---|---|---|
| 1 | ✅ 2026-10-24 **Machine-readable reviewer verdict** in `bridge-work` (and so `bridge-fix`): the review ends with `BRIDGE_VERDICT=APPROVE\|REVISE`; the loop reads the marker, REVISE without a row is itself a must-fix; a reviewer that dies already ends as "NOT DONE — no review" | ccswarm `SANGHA_DECISION`, oh-my-claudecode verdict files | false READY from a misread review |
| 2 | ✅ 2026-10-24 **Voices line** on every panel report + `degraded` in `status.json` + `[panel] DEGRADED` on stderr. Not done: a distinct exit code — `bridge-fix` and the `peer-panel` wrapper treat non-zero as failure, so that needs both to change first | claude-octopus `agent-summary` | a "consensus" of one real voice and two fallbacks reads like three |
| 3 | **Destructive-command friction**: any bridge command that removes a worktree, branch, board topic or ledger counts what it will remove, refuses without a TTY unless `--yes`, and no error text ever suggests `--force` or `rm -rf` | beads [#2363](https://github.com/gastownhall/beads/issues/2363) | an agent following an error message literally |
| 4 | **`bridge-sync --check` and `--dry-run`**, `.bak` beside every file it overwrites, Copilot MCP target `~/.copilot/mcp-config.json` | rulesync, ruler, mcpport | silent drift; config edits nobody previewed |
| 5 | ✅ 2026-10-24 **Needs-you surface**: `bridge-board cards --brief` (one line per open card, the question quoted as data, silent when none); the plugin ships it as a `UserPromptSubmit` hook (`hooks/hooks.json`), the README has the snippet for a clone | Orchestrator.inc "Needs you" lane, agent-mail overseer, Claude agent view | asks that nobody sees |

Also landed 2026-10-24, outside the borrowed list: **the message protocol as a spec** — [PROTOCOL.md](PROTOCOL.md)
(identities, envelope, header lines, kinds, request lifecycle, topics, markers, what a transport must enforce),
`protocol/envelope.schema.json`, `protocol/vectors.json`, `bridge-board read --json`, and `tests/test_protocol.py`
holding spec, schema and board to each other. It is the first of four steps toward a protocol a fourth vendor can
implement without reading SHARED-RULES.md: next the adapter conformance kit (#11), then a fourth family through it,
then an A2A transport (#16) when a cross-machine peer exists.

## Next (M) — cheaper, safer runs

| # | Item | Borrowed from | What it fixes |
|---|---|---|---|
| 6 | **Budget pre-flight**: estimate prompt + cited files before spawning; stop with the numbers when over a per-peer or per-run ceiling from `bridge-plan`; warn + hard stop on wall-clock and fix-loop rounds | consult7, octopus `/octo:costs`, Paperclip budgets | a quota burnt mid-panel; "half done, then silent" |
| 7 | **Answer to file**: `ask-agent --out FILE` writes the full answer and returns a 10–20 line summary plus the path and a footer (duration, exit reason, tokens); `bridge-watch` already shows the stream | consult7 `output_file`, gemini-mcp-tool [#6](https://github.com/jamubc/gemini-mcp-tool/issues/6) | a peer answer that swallows the caller's context |
| 8 | **Stall supervisor** in panel/work: per-peer log mtime; past the threshold send one nudge on the thread, then mark the role `stalled` in `status.json` and continue | Gas Town witness, agent-of-empires status detection | one quiet peer blocks the whole run until timeout |
| 9 | **Stance per role and vendor-blind cross-examination**: `--stance role=for\|against\|neutral` (default neutral); the leader sees answers without vendor names and must justify every departure from them in a "raw answers vs. synthesis" section | PAL `consensus`, llm-council [#3](https://github.com/karpathy/llm-council/issues/3) | same-family sycophancy; chairman over-influence |
| 10 | **Advisory path leases on the board**: `board_lease paths[] ttl exclusive reason`; `bridge-work` checks them before writing; expired leases are swept, never force-broken | mcp_agent_mail, Paperclip execution lock | two peers editing one file in parallel runs |
| 11 | **Adapter conformance kit**: `tests/fakebin/{claude,cursor-agent,copilot}` stubs that record argv and replay captured real output; one parametrised suite (spawn, parse, timeout, non-zero exit, `--add-dir` pass-through, deny flags per mode) every adapter must pass; version probe per CLI with a clear "unknown version" note | vibe-kanban `qa_mock.rs`, rulesync CONTRIBUTING checklist | adding a CLI, and finding out about a renamed flag from a run log |
| 12 | **Opt-in review gate** as a Claude Code `Stop` hook: a read-only review by another vendor of the uncommitted diff, blocking the stop on severity ≥ HIGH, with a round cap and a usage warning | codex-plugin-cc `--enable-review-gate` | "review from blank context" done by hand |

## Later (M–L) — reach

| # | Item | Borrowed from | Caveat |
|---|---|---|---|
| 13 | ◐ 2026-10-24 **Install in one command**: `.claude-plugin/plugin.json` + `marketplace.json` + `hooks/hooks.json` landed (`/plugin install agent-bridge --marketplace nuumz/agent-bridge`; no `version` field, so the plugin tracks the commit; `bridge-sync` skips the subagent links under a plugin root and re-points the board MCP when the plugin directory changes; MCP stays user-scope so the `mcp__bridge_board__*` tool names stay stable; `bridge-plan` names the subagents `agent-bridge:peer-*` there). Not done: root `plugin.json` (Agent Plugins 1.0 — its `mcp.json` cannot carry the per-client `AGENT_BRIDGE_AGENT` env the board uses for identity) and `pyproject.toml` for `uv tool install` | sync-agents-settings, codex-plugin-cc, uv | state must stay in `~/.agent-bridge/state`, never under the plugin root; `bin/` on PATH applies to Claude's Bash tool only |
| 14 | **Durable thread handle across commands**: one `--conv` usable by ask/panel/fix, with file references deduplicated by path + hash and an explicit turn/TTL cap | PAL `continuation_id`, Codex `threadId` | PAL's stale-file bug is the thing to avoid |
| 15 | **Skills convergence**: link `~/.agents/skills/<name>` to `~/.claude/skills/<name>` once Cursor's duplicate handling is verified; validate `SKILL.md` names against the Agent Skills spec before linking | agentskills.io, agent docs | verify first: Cursor also reads `~/.claude/skills` |
| 16 | **A2A card** exposing the bridge to peers that speak A2A (Gemini `kind: remote`) | a2abridge | only when a real peer needs it; adds an HTTP surface |

## Not planned

- A GUI, a daemon, a database server, or tmux as a dependency (see what happened to the projects that had them).
- Our own API keys or model routing (claude-code-router sits on the other side of the vendors' terms).
- Committing, pushing or opening PRs on the user's behalf.
- Rebuilding what the CLIs now ship natively: Claude subagents/agent teams/worktrees, Codex subagents, Gemini subagents.
