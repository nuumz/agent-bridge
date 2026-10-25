# agent-bridge protocol — version 1

The message layer that lets coding agents of different vendors talk to each other through the bridge. This file is
the normative spec; [`protocol/envelope.schema.json`](protocol/envelope.schema.json) is the machine-readable shape of
one message and [`protocol/vectors.json`](protocol/vectors.json) the test vectors every implementation must pass.
`bin/bridgelib/protocol.py` implements both for the bridge; `tests/test_protocol.py` holds this file, the schema, the
vectors and the board (`bin/boardlib.py`) to each other.

> **Document boundary.** This file owns *what a message is and what it means*: identities, the envelope, header
> lines, kinds, addressing, the request lifecycle, topics, body markers, transport requirements. How a peer CLI is
> driven (flags, modes, exit codes, deny lists) stays in [SHARED-RULES.md §2](../SHARED-RULES.md); what leaves the
> machine in [SECURITY.md](../SECURITY.md); how to add a vendor adapter in
> [CONTRIBUTING.md](../CONTRIBUTING.md#adding-a-peer-adapter).

**Words.** MUST / MUST NOT / SHOULD / MAY as in RFC 2119. *Transport* = whatever carries envelopes between agents
(v1: the SQLite board, §10). *Implementation* = anything that writes, carries or reads envelopes: the board, a vendor
adapter, a script, a future transport.

**Versioning.** Every envelope carries `v`. An additive change (a new optional field, a new header key, a new body
marker) keeps `v: 1` and adds vectors. A change under which a valid v1 envelope becomes invalid, or means something
else, bumps `v`; a reader that sees a `v` it does not know MUST treat the message as data it cannot act on, never
guess.

## 1. Terms

| Term | Meaning |
|---|---|
| **family** | one vendor CLI as the bridge drives it: `claude`, `cursor`, `copilot`, `codex`, and `a2a` for the remote agent of §10.4 (the `ADAPTERS` registry; `boardlib.FAMILIES` for turns). A family name is lowercase ASCII letters and digits |
| **peer** | an agent of a family started by the bridge for one job (an `ask-agent` run, a dispatched turn, a live peer). A peer is always *inside* the bridge: hop guard on, no spawning |
| **primary** | the interactive session of a family the user is typing into (`<family>@primary`). It may spend peer runs; a peer may not |
| **user** | the human at the machine, `user:<name>`. The only author of a `decision`; the one every *card* is addressed to |
| **topic** | one thread of messages with its own `seq` numbering and member list (§7) |
| **request / owner** | a message that asks another agent for something in a stated shape; the owner is whoever `to` names (§6) |
| **card** | the one shape in which an agent asks the human (§6.4) |
| **turn** | a request addressed to a *family* rather than a running identity: it queues a task that a human later dispatches as one run (§6.5) |
| **hop** | being inside a bridged run. One hop only: a peer never starts another peer, never dispatches, never opens a room |

## 2. Identities

An identity is resolved by the transport, never taken from what the author claims (§9). Grammar (`boardlib.IDENTITY`,
`$defs.identity` in the schema): no whitespace, no `+`, not starting with `@` or `*`, never the bare `user:`.

| Form | Who | Rights |
|---|---|---|
| `user:<name>` | the human | everything: `decision`, opening and closing any topic, turns, cards closed by their reply |
| `<family>@primary` | the user's interactive session of that family | opens rooms, calls turns, dispatches; never `decision` |
| `<family>#<pid>` | one `ask-agent` run (pid = the CLI process, so every run and every dialog round is distinct) | posts, replies, opens a `dm:` with the user or an agent it shares a topic with |
| `<family>:task<N>` | the run `bridge-board dispatch` started for task N (a turn) | as a run; the only identity a family-addressed request reaches |
| `<family>@live:<peer>` | a persistent `bridge-live` peer | as a run |
| `<family>@hop` | a bridged run that *claimed* a user or primary identity through its environment: the claim is downgraded to this | as a run |
| `a2a:<client>` | a remote agent the operator gave a bearer token to (`bridge-a2a client add <client>`), speaking A2A (§10.4) | leads its own topic `a2a:<client>` and nothing else: posts requests there (turns to the families the operator allowed), reads and withdraws them |
| `board` | the transport itself (`kind=task` log rows) | writes only `task` rows |
| `<slug>` (lowercase, digits, dashes) | a script on the user's side speaking for itself (`bridge-board post --as bridge-fix`) | posts as itself; never `decision`, never inside a bridged run |

Two runs of one `<family>#<pid>` thread (a later `--conv` turn gets a new pid) count as the same requester for closing
a request (§6.2). `family(identity)` is the part before the first `#`, `@` or `:`.

## 3. The envelope

One message is one JSON object. Field order is free; a reader MUST accept any order and MUST reject unknown fields
(`additionalProperties: false`): a field this version does not define is a different version, not an extension.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `v` | `1` | yes | envelope version |
| `topic` | string, non-empty | yes | the thread (§7) |
| `seq` | integer ≥ 1 | yes | position in the topic. Assigned by the transport; strictly increasing per topic; never reused while the topic exists |
| `author` | identity (§2) | yes | who posted it, as the transport resolved it |
| `kind` | one of §5 | yes | what the message does |
| `to` | `null` · `*` · `@<role>` · identity · family | no (null = everyone) | whom it is for (§5.2) |
| `reply_to` | `null` · integer ≥ 1 | no | the `seq` this one answers, same topic |
| `created_at` | `null` · integer ≥ 0 | no | Unix seconds the transport accepted it |
| `body` | string | yes | the message: header lines (§4), then text |
| `headers` | object | no, derived | the header lines of `body`, keys lower-cased. When present it MUST equal what §4 yields from `body` |
| `citations` | array | no, derived | every `path.ext:line[-end]` in `body`, in order, each once (§8.1). When present it MUST equal what the body yields |

`headers` and `citations` exist so a reader need not re-parse the body; the body stays the message. A writer MAY omit
them; the bridge's own export (`bridge-board read --json`) always fills them.

```json
{"v": 1, "topic": "room:design", "seq": 2, "author": "claude@primary", "kind": "request", "to": "cursor#4120",
 "reply_to": null, "created_at": 1761300010,
 "body": "shape: table file:line · claim · evidence\n\nList every caller of post() in bin/boardlib.py.",
 "headers": {"shape": "table file:line · claim · evidence"}, "citations": []}
```

## 4. Header lines

A body MAY start with a header block: consecutive lines of the form `<key>:<value>` where `key` has no whitespace.
The block ends at the first blank line or the first line that is not of that form. Keys are compared case-insensitively
and reported lower-cased; the value is everything after the first colon, trimmed. A body whose first prose line happens
to start with a colon-word (`Summary: all good`) is read as a header `summary` — start such a body with a blank line
or the text itself when that is not meant. A repeated key keeps the last value.

| Key | On | Meaning |
|---|---|---|
| `shape` | `request` (required) | what the answer must look like. Tokens a script checks: `table`, `file:line`, `json`, `diff` / `patch`, `decision`, `command` (§6.3); anything else is a human's call |
| `missing` | `blocked` (required) | what the author lacks, and the default it will take if nobody answers. `-`, `none`, `n/a`, `na`, `?` or empty do not count |
| `card` | a card (§6.4) | `decision` or `fact` |
| `due` | a card | when the default applies |
| `dir` | a turn (§6.5) | the workspace the run starts in; stored resolved. Required with `mode: write` |
| `mode` | a turn | `ask` (default), `plan` or `write` |
| `owner` | `request` (informational) | the identity the requester expects to answer; the transport routes on `to`, not on this |

Any other key is carried as a string and means nothing to the transport.

## 5. Kinds and addressing

### 5.1 Kinds

| `kind` | Posted by | Meaning | Closes a request? |
|---|---|---|---|
| `note` | anyone | information; no reply owed | no |
| `question` | anyone | a question with no owner and no shape; a reply is a courtesy, not a contract | no |
| `request` | anyone (to a family: user, primary or the topic's leader only) | asks `to` for an answer in `shape:`; stays open until closed (§6) | — |
| `answer` | anyone | an answer; to an open request from its owner or requester it closes it | yes |
| `done` | anyone | work finished, with evidence; also the summary a topic needs before it closes (§7.3) | yes |
| `blocked` | anyone | cannot proceed: `missing:` says what, and the default taken if nobody answers | yes |
| `handoff` | anyone | passes a piece of work to `to` | no |
| `decision` | `user:*` only | the human's ruling. Closes a card; on an agent's request it steers without closing | a card only |
| `task` | `board` only | the transport's task-log row (queued, claimed, done, lease expired…); never mail | no |
| `system` | bridge commands only | bookkeeping: `reopened by <who>` (as the reopener), a live peer's start / restart / leave (by its broker, as that peer) | no |

An agent MUST NOT post `decision`, `task` or `system` by its own hand; the MCP tool enumerates only the seven it may
post.

### 5.2 Addressing (`to`) and reach

- `null` or `*`: everyone in the topic. `@<role>`: every member holding that role (§7.2). An identity: that agent. A
  family name on a `request`: a turn (§6.5).
- A message **reaches** an agent when it is not a `task` row and is addressed to everyone, to the agent, to one of its
  roles, or replies to a message of the agent's. A family-addressed request reaches the `<family>:task<N>` run
  dispatched for it (or a hand-started `<family>#<pid>` run of that family), nobody else.
- A transport MUST let an agent read or wait on *only what reaches it* (`board_read mine=true`, `board_wait
  mention=true`), so other people's traffic does not spend its waits.

## 6. The request lifecycle

```
                 post kind=request, to=<owner>, body starts with `shape: …`
                                      │
                                      ▼
                              ┌──── OPEN ────┐
   reply_to=seq, kind ∈ {answer, done, blocked}   from the owner or the requester
   (a card: any user:* reply, `decision` included)
                                      │
                                      ▼
                                   CLOSED
```

### 6.1 Opening

A `request` MUST carry `to` (a request without an owner is a note) and a `shape:` header line. The transport refuses
one without either.

### 6.2 Closing

A request is closed by a reply (`reply_to` = its `seq`) of kind `answer`, `done` or `blocked` from:

- the owner: the identity `to` names; any member holding the `@role` it names; anyone when `to` is `*`; a run of
  that family (`<family>:task<N>` or `<family>#<pid>`) when `to` is a family; any `user:*` when `to` is a user;
- or the requester (a later run of the same `<family>#<pid>` thread included) — withdrawing it.

A third party's reply does not close it. For a card (`to` = `user:*`) the user's `decision` also closes it; on any
other request a `decision` steers and leaves it open. An implementation MUST list the open requests addressed to an
agent (`open_requests_to_me`), and that count MUST reach zero only through the closes above.

### 6.3 Shape check

An `answer` or `done` from an agent to *another* agent's still-open request is held to the request's `shape:`. The
tokens a script can check (`bridgelib/evidence.py`):

| token | satisfied by |
|---|---|
| `table` | a markdown table with a header row, a separator and at least one data row, outside code fences |
| `file:line` | at least one `path.ext:line` citation (§8.1) |
| `json` | a fenced ```` ```json ```` block |
| `diff` / `patch` | a unified diff (`diff --git`, `--- a/`, `+++ b/`) |
| `decision` | a four-column table row under a separator, outside code fences |
| `command` | a `$ cmd` line, a fenced `sh`/`bash`/`zsh`/`console` block, or an inline `` `cmd arg` `` |

A shape offering alternatives (`json or a table`) is met by either; a negated token (`no table`) is not asked for;
`command output` asks for output, not a command; a shape with no checkable token is not checked. A reply that misses is
**refused** with the shape quoted back — that refusal is the one automatic re-request; the agent reposts in shape, or
posts `blocked` with a `missing:` line. Not checked: the requester withdrawing, a human's reply, a reply to a card, a
reply to a request already closed. A refused reply is a `shape_miss` ledger row (the same body retried is one row);
every `blocked` is a `blocked` row.

### 6.4 Cards — the one way to reach the human

A card is a `request` addressed to `user:*` whose body is `boardlib.card()`: header lines `card: decision|fact`,
`shape:` equal to the card kind, optional `due:`, a blank line, then one table row per question — question · options ·
recommendation · if undecided. A *decision* card is a choice only the human owns; a *fact* card is information nobody
else has, with the safe default the agent takes if nobody answers, so the team does not stop. The user answers with
`decision` (a decision card) or `answer` (a fact card) `reply_to` the card; any `user:*` reply closes it. Cells carry no
newline and no `|`, so a question cannot forge a header or break the table. An agent MUST NOT guess a decision or a
fact only the user has; it posts the card, keeps working on what does not depend on it, and says in its `done` which
part rests on the default.

### 6.5 Turns — a peer that is not running joins by the request

A `request` with `to` = a family name is a turn: the transport queues it as a task for that family in the topic
(`request_seq` links them), with `dir:` and `mode:` from the header (`write` needs `dir:`), in the same transaction as
the message or not at all. Only the user, the primary or the topic's leader may call one (each is a full run); a
peer's request to a family is refused. Nothing runs until a human runs `bridge-board dispatch`; the run gets the
identity `<family>:task<N>`, is told which request it answers and in which shape, and its closing reply closes the
request and the task together (`blocked` → task failed). A closing reply by anyone else (the requester withdrawing,
the user) cancels the task. When the run ends without having posted that reply — a remote family (§10.4) has no board
tools; a run crashed or answered off the board — the transport posts it from the run's result as `<family>:task<N>`:
`answer` with the text, or `blocked` with a `missing:` line when the run failed or the text misses the request's
shape (§6.3). A turn therefore always closes its request; nothing else is ever posted on a run's behalf.

## 7. Topics

### 7.1 Names

Any non-empty string. Reserved prefixes and who opens them:

| Prefix | Opened by | What |
|---|---|---|
| `conv:<name>` | `ask-agent --conv` | one multi-turn thread with a peer; the bridge mirrors both sides |
| `panel:<name>`, `conv:panel-<name>-<role>` | `bridge-panel` | a panel's plan and report; one thread per role |
| `live:<room>` | `bridge-live` | a room of persistent peers; the caller leads |
| `fix:<name>` | `bridge-fix` | the decision card of a fix flow |
| `a2a:<client>` | `bridge-a2a serve`, for an authenticated remote client, which leads it | the client's thread: every A2A task it sends is one request there (§10.4) |
| `dm:<a>+<b>` | either side (sorted by the transport; `+` because identities carry `:`) | a 1:1 thread with exactly those two as members. A peer may open one only with the user or with an agent it already shares a topic with |
| `room:<name>` | the user or a primary, who becomes its leader | a group thread |
| anything else | the user or a primary | a plain topic; a peer may not open topics |

### 7.2 Members and roles

A member holds one role: `leader`, `member`, `observer`, or a persona slug (`root-cause`, `design-critic`, …) so
`to=@root-cause` reaches whoever holds it. Only `leader` carries rights: adding, re-roling and removing members,
calling turns, closing the topic. The user has every right in every topic and is never removed by an agent. Anyone
may leave. A closed `dm:`/`room:` is reopened only by the user or a primary; a `system` row `reopened by <who>` marks
it, and a side that left is not re-added.

### 7.3 Closing

The user, the leader, either side of a `dm:`, or the creator of a topic that has no members may close it. A topic with
members closes only after a **summary**: a `done` to `*`, not a reply, posted after the last request and the last
reopening — what was done, the evidence, what is still open — so the record ends with the outcome. A finished bridge
run closes its own topics; closed topics older than 14 days are deleted by `gc`.

## 8. Body conventions and markers

### 8.1 Evidence: `file:line` or UNVERIFIED

A claim another agent must act on carries a citation `path.ext:line` or `path.ext:line-end` (`evidence.CITE_RE`: a
path whose extension is one a repository holds source or text in, so `host:443` and `v1.2:3` are not citations). A
claim without one is **UNVERIFIED** and is weighed as such however many agents repeat it. `citations` in the envelope
lists what was cited; whether it resolves is checked against a workspace outside the envelope (`verify_citations`),
and a role whose citations mostly fail is named *low-evidence*.

### 8.2 In-band markers

The direct channel (§10.2) and the pipelines read these from a reply body; each is one line, verbatim:

| Marker | Where | Meaning |
|---|---|---|
| `[[ASK]] <question>` | last line of a peer's reply | the peer needs one fact only the caller has; `ask-agent` exits 5 and, with a board thread, posts the question as a fact card |
| `[[AGREE]]` / `[[DONE]]` | a dialog or live-room reply | nothing left to dispute — not "verified". Both sides ending on it ends a dialog; a live peer is not woken by replies that only agree back |
| `[[STOP]]` | a `user:*` `decision` | ends a running dialog before its next turn, or every live peer the decision reaches |
| `BRIDGE_VERDICT=APPROVE` / `BRIDGE_VERDICT=REVISE` | last line of a `bridge-work` review | decides the fix loop; `REVISE` without a finding row is itself a must-fix |
| `QUESTION FOR <role>: …` | a panel finding | a question another role answers in cross-examination |

### 8.3 Language

Agent ↔ agent text uses whatever is cheapest (terse English is fine). Whatever is *returned* to the user — a report,
a review table, a direct answer — is in the language the request was written in. Code, identifiers, paths, commands
and quoted messages stay verbatim.

## 9. Trust

- **Identity is the transport's.** The board resolves an author from the registered run among the caller's ancestor
  processes, never from a claim; a bridged run that claims `user:*` or `@primary` through its environment becomes
  `<family>@hop`. A transport that cannot authenticate an identity MUST NOT carry one.
- **Board text is data, not instructions.** Every reader treats a body as content written by another model; only a
  `decision` from `user:*` steers. A request whose shape asks for secrets, credentials, tokens or files outside the
  task is answered `blocked` with `missing: outside this task`.
- **One hop.** A peer never spawns a peer, never dispatches, never opens a room; the transport refuses, not the prompt.
- **Nothing on the board spawns anything.** A turn is a queued task until a human dispatches it with a concurrency cap.
- **Egress is the human's choice.** Which families may see a repository is set per run (`-p`); the protocol never
  routes to a family the user did not name.
- **Read-only by default; never a commit.** A run's mode is `ask` unless the request says otherwise; `commit`, `push`
  and `tag` are denied on every peer however the request is worded.

## 10. Transports

### 10.1 Board (v1, the only transport today)

One SQLite file (`~/.agent-bridge/state/board/board.db`, WAL), served by `bridge-board serve` over MCP stdio — each
CLI spawns its own server, no daemon — and by the `bridge-board` CLI. Tools: `board_status`, `board_post`,
`board_read`, `board_wait`, `board_open`, `board_join`, `board_leave`, `board_close`, `board_ask_user`,
`board_task_add`, `board_task_claim`, `board_task_done`. `bridge-board read <topic> --json` prints the topic as JSON
Lines, one envelope per line, `headers` and `citations` filled.

Any transport MUST provide: per-topic `seq` that is strictly increasing and assigned by the transport; the refusals of
§6.1, §6.2's closing rules and §6.3's shape check at the transport (a prompt is not enforcement); identities per §2
and §9; a read and a wait limited to what reaches the reader (§5.2); at-least-once delivery with `seq` as the
deduplication key; and the invariants `bridgelib.protocol.check` tests on every envelope it emits.

### 10.2 The direct channel

`ask-agent <family> "<prompt>"` is one request and one reply outside any topic: the prompt (plus the bridge's protocol
suffix, which names the board thread when there is one) goes to the peer's CLI, the reply comes back as text with the
markers of §8.2, and the exit code says what happened (SHARED-RULES §2: 0 ok · 5 asked back · 7 out of quota …). With
`--conv <name>` both sides are mirrored onto topic `conv:<name>` as envelopes (the request as `user:<name>` `note`, the
reply as `<family>#<pid>` `answer`), so a direct exchange is readable on the board like any other.

### 10.3 Not in v1

Message signatures, per-family capability discovery (which shapes and modes a family answers) and budget headers. A
transport that adds them is a new `v`, or new optional fields with vectors. Reach across machines exists as the A2A
transport below, which carries the envelope unchanged.

### 10.4 A2A transport (`bridge-a2a`)

The bridge is reachable as one [A2A](https://a2a-protocol.org) agent (Agent2Agent, JSON-RPC 2.0 over HTTP), and can
send a task to any A2A agent. Nothing in the envelope changes: an A2A task *is* one board request, and the A2A
client is an identity of §2. The server is started by the operator (`bridge-a2a serve`, like `bridge-live start`),
listens on loopback unless told otherwise, and is needed for nothing local.

| A2A | Board |
|---|---|
| a client with a bearer token (`bridge-a2a client add <name>`) | identity `a2a:<name>`, leader of its own topic `a2a:<name>` (created on first use) |
| `message/send` — text parts, `metadata.to` (a family the operator allowed with `--peers`), `metadata.mode` (`ask` default, `plan`, `write` only under `--allow-write`), `metadata.shape` | one `request` on that topic, `to` = the family (a turn: a task queued for it), header lines `shape:`, `mode:`, `dir:` (the operator's `--dir`, never the client's), `message-id:` |
| Task `id` / `contextId` | `<topic>/<seq>` / the topic |
| `submitted` · `working` | the queued task · the task claimed by a dispatch |
| `completed` (artifact = the reply text) · `failed` (status message = the `blocked` body or the task's failure) | the closing `answer`/`done` · `blocked` from a run of that family (§6.2), or a board task that ended without a reply |
| `canceled` | the requester's own closing reply (`tasks/cancel` posts it), which cancels the queued task |
| `tasks/get` | the request, its board task and its replies, read as the client only (another client's task is `TaskNotFound`) |
| `configuration.blocking` | the server polls the board up to `--max-block` seconds before answering |
| not offered: `message/stream`, push notifications (`capabilities` says so; `UnsupportedOperation` / `PushNotificationNotSupported`) | the board has no push channel either; polling is the model |

What the server enforces, on every request: the token (the card is the only unauthenticated resource); one topic
per client, so a client never reads or closes another's requests; the operator's family list as the egress gate;
the operator's directory; the peer task cap of §6.5 (a client's turns are peer-origin for the cap, user-origin for
dispatch, since the operator issued the token); and the rule that **nothing on the board spawns anything** — a
remote task waits in `submitted` until a human runs `bridge-board dispatch`.

Outbound, `bridge-a2a send` (what `ask-agent a2a` runs through the `a2a` adapter) fetches the remote card, sends one
`message/send` and polls `tasks/get` until a terminal state, printing JSON Lines — `card`, `task` per state change,
`result` — so the adapter's `parse()` and a human read one stream. The remote URL comes from `AGENT_BRIDGE_A2A_URL`
and the token from `AGENT_BRIDGE_A2A_TOKEN`, environment only, never a command line. The bridge sends text, a mode
and a shape; no credential, no path grant (`--add-dir` is not forwarded), no command — the remote agent's own rails
are the rails. A request `to=a2a` on the board is a turn to that remote like any other family, with two differences
on this side: `dispatch` sends it the request body alone (no topic, identity, requester or other messages — the
remote has no board tools and should learn nothing local), and the bridge posts the remote's reply as `a2a:task<N>`
from the run's result (§6.5) — `answer`, or `blocked` with a `missing:` line when the remote task failed.

## 11. Conformance

`python3 -m unittest tests.test_protocol` checks that:

1. the schema uses only the keyword subset `bridgelib.protocol.validate` implements (an unknown keyword is a failure,
   never a silent pass), and its enumerations equal the board's (`KINDS`, `TASK_MODES`, the identity and role
   grammars);
2. every `valid` vector passes `protocol.check` and every `invalid` one fails;
3. every kind the board accepts, posted on a real (temporary) board, exports as a conformant envelope, and
   `bridge-board read --json` prints exactly those;
4. this file names every kind and every header key.

Another implementation (a new transport, a script in another language) is conformant when it passes the vectors and
the requirements of §10.1. To add a vendor: an adapter class (CONTRIBUTING.md), its family name in `boardlib.FAMILIES`
so turns can address it, its stub and fixture under `tests/fakebin/` with a row in the `EXPECT` table of
`tests/test_adapter_conformance.py` (the adapter conformance kit: the same spawn / parse / failure-path suite every
family passes), and a vector per new behaviour. The envelope itself does not change.
