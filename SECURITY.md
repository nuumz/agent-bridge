# Security and data handling

## What leaves the machine

agent-bridge runs the coding-agent CLIs you already have (`claude`, `cursor-agent`, `copilot`, `codex`). Every bridged call sends
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
| Read-only by default | each CLI's own permission / deny mechanism (`--permission-mode`, deny lists); Codex: `--sandbox read-only` (no file writes, no network) | Cursor honours only its global deny list, which the bridge holds temporarily; an unrestricted shell can still write through an interpreter. The Codex sandbox is the CLI's own (Seatbelt / Landlock) and has not been exercised through the bridge yet |
| No `git commit` / `push` / `tag` | Claude and Copilot: their deny lists. Cursor: a write run holds `Shell(git commit)`/`push`/`tag` in the global deny for its duration. Codex: no deny list — its `workspace-write` sandbox keeps `<root>/.git` read-only and has no network, so a commit or push cannot land | the Cursor hold is the global `~/.cursor/cli-config.json`, so it is only as reliable as the restore on exit; `bridge-work` reports the branch against its base SHA, so a stray commit is visible. A `-m write` Claude run may `git add`. For Codex the rail is the sandbox's `.git` protection alone, plus the prompt |
| Your uncommitted work survives a write run in your checkout | `git restore` / `checkout` / `switch` / `stash` / `reset` / `clean` (and the `git -C` / `-c` / `--git-dir` / `--work-tree` option forms) denied on every peer outside a bridge-owned worktree (`-w`, branch `bridge/<name>`) | denies match a command prefix only: a pre-approved runner (`make`, `npm run`, `npx`, `pytest` on Claude) or an unrestricted shell (Cursor) can still reach the verbs through a script it writes; the peer can overwrite a file it is allowed to edit; the Cursor hold is machine-global for the run. **Codex has no deny flag at all**: these verbs are prompt-only there (they write to the working tree, which `workspace-write` allows), so prefer `-w`-style isolation (`git worktree add` + `-d`) for a Codex write run in a dirty checkout |
| One hop | `AGENT_BRIDGE_HOP` in the peer's environment; every command that spawns a peer refuses under it | a peer could `unset` it in a shell it is allowed to run |
| Isolated writes | `bridge-work` / `bridge-fix` write in a git worktree on branch `bridge/<name>` | the worktree shares the repository's hooks and untracked ignores |

Treat a peer's output as untrusted input: verify `file:line` claims before acting, and never paste a peer's answer into
a shell.

## Network surface (A2A) — off by default

Nothing listens unless you run `bridge-a2a serve`. When you do:

| | |
|---|---|
| Who can connect | holders of a bearer token you issued (`bridge-a2a client add <name>`; tokens in `~/.agent-bridge/state/a2a/clients.json`, mode 0600). The agent card is the only unauthenticated resource. The server binds loopback unless `--bind` says otherwise, and refuses a non-loopback bind with no client token |
| What a client can do | post requests on its own topic `a2a:<name>`, addressed to the families you listed with `--peers`; read and cancel those requests. It cannot read any other topic, choose a directory (yours, `-d`), run anything (a request waits for your `bridge-board dispatch`), or ask for `write` unless you started the server with `--allow-write` |
| What leaves the machine | to a client: the reply text of its own requests. To a remote agent you call (`ask-agent a2a`, or a turn `to=a2a` that `dispatch` runs): the prompt — for a dispatched turn the request body alone, no topic, identity, requester or other board messages — a mode and a shape; never a credential, a path grant (`--add-dir` is not forwarded) or a command; the token travels in `AGENT_BRIDGE_A2A_TOKEN`, never on a command line, and does not appear in `--dry-run` or run logs |
| Transport | plain HTTP unless `--cert`/`--key`; across machines use an SSH tunnel or put TLS in front. Request bodies are capped at 1 MiB and need a plain-decimal `Content-Length` (no chunked transfer), a JSON-RPC batch at 32 requests; a connection silent for 30 s is closed, and every error response (401, 411, 413, a parse error) closes its connection so an unread body is never parsed as the next request |
| Known gaps | no rate limit beyond the board's cap of five open turns per client (its own topic); a client's turns are user-origin for `dispatch` (you issued its token), so `--include-peer-tasks` does not gate them; the remote agent's rails are its own — the bridge cannot deny a command on the other side |

## Reporting a vulnerability

Open a private security advisory on GitHub (Security → Report a vulnerability) or, if that is unavailable, an issue
titled `security:` with no exploit details; the maintainer will move it to a private channel. Please include the bridge
commit, the peer CLI versions from `ask-agent --list`, and the `[bridge]` line of an affected run.
