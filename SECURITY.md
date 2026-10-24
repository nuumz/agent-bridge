# Security and data handling

## What leaves the machine

agent-bridge runs the coding-agent CLIs you already have (`claude`, `cursor-agent`, `copilot`). Every bridged call sends
the prompt, the preamble the bridge adds, and **every file the peer decides to read** to that peer's vendor, under your
own login and plan. The bridge adds no telemetry, no network calls of its own, and no API keys.

Decide per vendor which repositories may be shared. `bridge-panel`, `bridge-work` and `bridge-fix` take `-p` as the
explicit list of peers allowed to see the repository; `ask-agent` sends to exactly the peer you name. In read-only runs
the bridge also grants a peer the git repository of any absolute path under `$HOME` that your prompt cites
(printed as `[bridge] add-dir (cited in prompt)`), so cite only what the peer may read.

## What stays local

Everything under `~/.agent-bridge/state/` — every prompt, transcript, run log, board message, ledger row — is written
to your disk only and is git-ignored. Delete the directory to forget it; `bridge-panel gc` and the board gc prune runs
older than 14 days.

## Guard rails and their limits

| Rail | Enforced by | Known gap |
|---|---|---|
| Read-only by default | each CLI's own permission / deny mechanism (`--permission-mode`, deny lists) | Cursor honours only its global deny list, which the bridge holds temporarily; an unrestricted shell can still write through an interpreter |
| No `git commit` / `push` / `tag` | Claude and Copilot: their deny lists. Cursor: no deny list applies to a write run — the prompt forbids it | a Cursor `-m write` run is not prevented from committing; `bridge-work` reports the branch against its base SHA, so a stray commit is visible, not blocked. A `-m write` Claude run may `git add` |
| One hop | `AGENT_BRIDGE_HOP` in the peer's environment; every command that spawns a peer refuses under it | a peer could `unset` it in a shell it is allowed to run |
| Isolated writes | `bridge-work` / `bridge-fix` write in a git worktree on branch `bridge/<name>` | the worktree shares the repository's hooks and untracked ignores |

Treat a peer's output as untrusted input: verify `file:line` claims before acting, and never paste a peer's answer into
a shell.

## Reporting a vulnerability

Open a private security advisory on GitHub (Security → Report a vulnerability) or, if that is unavailable, an issue
titled `security:` with no exploit details; the maintainer will move it to a private channel. Please include the bridge
commit, the peer CLI versions from `ask-agent --list`, and the `[bridge]` line of an affected run.
