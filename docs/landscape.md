# Landscape — comparable projects and what agent-bridge takes from them

Surveyed 2026-10-24 (stars and activity read from the GitHub API that day; `open issues` is GitHub's count, which
includes pull requests). Links are to the projects themselves or to the issue that documents a claim. This file is
the "why not X" behind the README and the source of the borrowed ideas in [ROADMAP.md](ROADMAP.md).

## Where agent-bridge sits

Three families of tools let a developer use more than one coding agent:

1. **Second-opinion / consensus servers** — an MCP server or plugin that lets one agent consult other models.
2. **Multi-agent workspaces** — a GUI/TUI/daemon that runs several agents in parallel worktrees and merges the result.
3. **Instruction and skill sync** — tools that keep `CLAUDE.md`, `.cursor/rules`, `AGENTS.md` and skills aligned.

agent-bridge is a thin layer across all three with one constraint the others do not share: **it only wraps the CLIs
already installed and logged in**, adds no runtime, no API key and no commit, and treats every peer answer as a lead
that must carry `file:line` or be marked UNVERIFIED. The niche that is genuinely unoccupied is *cross-vendor review
and second opinion with zero runtime, read-only by default, no commits, and explicit per-vendor data-egress consent*.

## 1. Second opinion / consensus

| Project | What it is | Talks to models via | Status 2026-10-24 |
|---|---|---|---|
| [PAL MCP (ex Zen)](https://github.com/BeehiveInnovations/pal-mcp-server) | MCP tools `consensus`, `codereview`, `debug`, `clink` | API keys, or `clink` spawning vendor CLIs with `--yolo`/bypass-sandbox presets | 11.8k★, last push 2025-12 |
| [llm-council](https://github.com/karpathy/llm-council) | answers → anonymised peer ranking → chairman synthesis | OpenRouter key | 25.8k★, one-day hack, unsupported |
| [codex-plugin-cc](https://github.com/openai/codex-plugin-cc) | official Claude Code plugin: `/codex:review`, adversarial review, Stop-hook review gate | the `codex` binary (ChatGPT subscription) | 34.5k★ in ~7 months |
| [gemini-mcp-tool](https://github.com/jamubc/gemini-mcp-tool) | MCP that spawns Gemini CLI; structured-edit mode, chunked responses | the CLI, no key | 2.3k★; switched backend when Gemini CLI was retired |
| [consult7](https://github.com/szeider/consult7) | one tool: files + question → big-context model, with a budget pre-flight | OpenRouter key | 0.3k★ |
| [claude-octopus](https://github.com/nyldn/claude-octopus) | Claude Code plugin: debate/council across 12 providers, cost pre-flight, contributed/degraded/failed ledger | vendor CLIs | 4.3k★ |

Borrowed or planned (see ROADMAP): stance per role and vendor-blind cross-examination (PAL, llm-council
[#3](https://github.com/karpathy/llm-council/issues/3)); contributed / degraded / failed header on every report
(octopus); budget pre-flight that fails before anything is sent (consult7, octopus); answer-to-file with a short summary
(consult7, gemini-mcp-tool [#6](https://github.com/jamubc/gemini-mcp-tool/issues/6)); an opt-in Stop-hook review gate
(codex-plugin-cc).

Pain points we design against: hangs with no signal (PAL [#181](https://github.com/BeehiveInnovations/pal-mcp-server/issues/181));
tool-schema context tax (PAL [#255](https://github.com/BeehiveInnovations/pal-mcp-server/issues/255) — our 12 board tools
are already a cost worth measuring); CLI drift (`codex mcp-server` removed within a year, Gemini CLI retired 2026-06,
`cursor-agent` renamed) — the adapter must probe versions and pass errors through verbatim.

## 2. Multi-agent workspaces and agent-to-agent channels

| Project | What it is | Drives agents via | Status 2026-10-24 |
|---|---|---|---|
| [Paperclip](https://github.com/paperclipai/paperclip) | "company of agents": org chart, budgets, atomic task checkout, heartbeat queue | adapters per CLI, Node + embedded Postgres | 106k★ |
| [vibe-kanban](https://github.com/BloopAI/vibe-kanban) | kanban + worktree per task + diff review | subprocess per executor, JSON profiles | 28k★; company [shut down 2026-04](https://vibekanban.com/blog/shutdown), community-maintained |
| [beads](https://github.com/gastownhall/beads) / [Gas Town](https://github.com/gastownhall/gastown) | issue graph for agents (`bd ready`, atomic claim) / multi-agent town with stall witness and merge queue | Go + Dolt + tmux | 28k★ / 18k★ |
| [claude-squad](https://github.com/smtg-ai/claude-squad), [agent-of-empires](https://github.com/njbrake/agent-of-empires) | TUI: one tmux session + worktree per agent | tmux pane capture | 8.7k★ / 3.4k★ |
| [mcp_agent_mail](https://github.com/Dicklesworthstone/mcp_agent_mail_rust) | mailbox MCP: threads, acknowledgements, advisory file leases with TTL, overseer channel | MCP + SQLite | Python version unmaintained; Rust rewrite active |
| [AgentAPI](https://github.com/coder/agentapi), [Crystal](https://github.com/stravu/crystal) | HTTP over a terminal emulator / Electron runner | screen scraping | both archived or deprecated |
| [a2abridge](https://github.com/vbcherepanov/a2abridge) | A2A 1.0 mesh for Claude Code, Codex, Cursor, Gemini on one machine | MCP stdio + local directory | 16★ — the closest concept to ours, without consent or evidence rules |

What the vendors now ship natively, which a bridge must not rebuild: Claude Code subagents, agent teams, worktrees,
cross-session messaging and `claude agents` (which **commits and pushes** when done — the opposite of our contract);
Codex subagents (inherit the parent sandbox, no worktree isolation); Gemini CLI local/remote subagents.

Borrowed or planned: advisory path leases on the board (agent mail, Paperclip's execution lock); a machine-readable
reviewer verdict instead of parsing prose (ccswarm `SANGHA_DECISION`); stall detection per peer with one nudge then
`stalled` in `status.json` (Gas Town witness); destructive commands that refuse without a TTY and error texts that
never suggest `--force` (beads [#2363](https://github.com/gastownhall/beads/issues/2363): an agent followed the error's
advice and deleted 247 issues in 15 seconds); a single "needs you" view fed to the primary session by a hook.

Pain points we design against: orphans of every kind (tmux panes, worktrees, sessions killed by someone else's
cleanup); session ids lost on restart (agent-of-empires [#343](https://github.com/njbrake/agent-of-empires/issues/343));
quota burnt by fan-out (Conductor, Claude agent view) — the default is 3–5 peers per run, and heavy fan-out sits outside
Anthropic's "ordinary, individual usage" of a subscription.

## 3. Instruction and skill sync

| Project | What it is | Status 2026-10-24 |
|---|---|---|
| [rulesync](https://github.com/dyoshikawa/rulesync) | `.rulesync/` source → native rules/skills/MCP/hooks for 20+ tools; `generate --check` drift gate; plugin packaging | 1.6k★, very active, frequent majors |
| [ruler](https://github.com/intellectronica/ruler) | `.ruler/*.md` concatenated into each agent's file; `revert` with `.bak`; provenance comments | 3.0k★, "beta research preview" |
| [AGENTS.md](https://agents.md/) | the one instruction file Cursor, Copilot, Codex, Gemini and OpenCode all read; Claude Code needs `@AGENTS.md` in `CLAUDE.md` | 25k★ |
| [Agent Skills](https://agentskills.io/specification) | `SKILL.md` spec: `name` ≤64 chars matching the directory, `description` ≤1024, progressive disclosure | 26.5k★ |
| [Agent Plugins 1.0](https://agent-plugins.org/specification) | root `plugin.json` + `skills/` + `mcp.json`, read by Copilot CLI, Cursor and Codex; Claude Code has its own `.claude-plugin/` manifest and marketplace, which Copilot and Codex also read | announced 2026-08 |
| [mcpport](https://github.com/Abdalkaderdev/mcpport), [agentsync](https://github.com/x0c/agentsync), [sync-agents-settings](https://github.com/Leoyang183/sync-agents-settings) | MCP-config and settings sync across agents; dry-run by default, timestamped backups, temp+rename writes, the repo as its own plugin marketplace | small |

Where the agents actually look (from their own docs, same date): user-level skills converge on `~/.agents/skills/`
(Copilot, Cursor, Codex, Gemini, OpenCode; not Claude Code), and Cursor and OpenCode also read `~/.claude/skills/`
directly. Cursor documents no user-level rules file at all — which is why `ask-agent cursor` links the generated
`~/.cursor/rules/*.mdc` into `<repo>/.cursor/rules/` before every run (verified against the CLI 2026-09-12) instead of
trusting the home directory.

`bridge-sync` is already ahead on two points — it symlinks skills instead of copying them, and it strips Claude-only
sections before mirroring — and behind on the mechanics every sync tool in this group has: `--check`, dry-run for
config writes, backups, and a Copilot MCP target (`~/.copilot/mcp-config.json`). Distribution is the other gap: the
projects that grew in this space install in two or three commands (a plugin marketplace, `uv tool install`, a tap).

## Weaknesses we admit

- No interactive attach to a running peer: you can answer its card on the board, and `bridge-watch` shows the stream,
  but you cannot take over its terminal.
- No process sandbox: the rails are each CLI's own permission mechanism, with the gaps listed in
  [SECURITY.md](../SECURITY.md).
- No file-level lease between concurrent peers yet; no budget ceiling per run yet (both on the roadmap).
- The peer list follows vendor churn; an adapter breaks when a CLI renames a flag, and we find out from the run log.
