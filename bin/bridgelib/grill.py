"""Grill-plan policy for bridge jobs — whether to run grilling rounds (skill `grilling` / `grill-me`) before peers spend.

Kept small on purpose: playbook frontmatter sets a default; bridge-plan recommends do/who/when; the session card
records the ruling; `status: TODO` blocks `bridge-plan agree` the same way other open fields do.
Interactive grilling is always human↔main-agent (grill-me is user-invoked); a panel leader may only *recommend*
an after-findings grill in the report — it never runs the interview itself.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .core import STATE


# who: main-agent = this session runs `grilling` with the user
#       panel-leader = leader peer only recommends questions in the report (no interactive run)
#       user = user invokes /grill-me themselves; main waits
#       none = no grill
# when: before-agree | before-run | after-findings | never
# do:   yes | no | optional  (optional = leader/main may offer; never blocks agree)
# status: TODO (blocks agree) | pending (printed advice, does not block) | done | skipped


def recommend(book: dict, gaps: list[str], request: str, level: str) -> dict:
    """Return a grill-plan dict for the session card / bridge-plan printout."""
    policy = (book.get("grill") or "recommend").strip().lower()
    form = (book.get("form") or "direct").strip().lower()
    kind = book.get("name", "")

    if level == "routine" or policy == "skip" or form == "direct":
        return _plan("no", "none", "never", 0, "skipped",
                     f"playbook grill={policy or 'skip'} / level={level} / form={form} — no grill")

    ambiguous = bool(re.search(
        r"\b(option|which|should|brainstorm|ออกแบบ|ทางเลือก|ควรเป็น|ยังไม่ชัวร์|unsure|WIP|PROPOSED)\b",
        request or "", re.I))
    many_gaps = len(gaps) >= 2
    negotiate = level == "negotiate"
    gate_named = bool(re.search(r"--files|--verify|touch only|files the fix may touch", request or "", re.I))

    # Pipeline first: --files/--verify are the contract. Do not force grill just because
    # missing_inputs() wants globs the CLI will carry later (that branch used to dead-code the skip).
    if form == "pipeline" and policy != "require":
        if ambiguous and not gate_named:
            return _plan(
                "yes", "main-agent", "before-agree", 1, "TODO",
                "implement scope still fuzzy (no files/verify named in the request) — one grill round before the card")
        return _plan(
            "no", "none", "never", 0, "skipped",
            "implement pipeline: files+verify are the contract; override Grill plan on the card if scope is still fuzzy")

    # fix-flow: grilling belongs after the panel, on the decision table — not before analyze.
    if form == "fix-flow" and policy != "require":
        return _plan(
            "optional", "main-agent", "after-findings", 2, "skipped",
            "analyze-decide-fix: optional grill on the Decisions table after the panel, before `bridge-fix decide`")

    if policy == "require" or (policy == "recommend" and negotiate and (many_gaps or ambiguous)):
        return _plan(
            "yes", "main-agent", "before-agree", 2, "TODO",
            "sharpen goal/options/invariants with grilling before peers run "
            f"(gaps={len(gaps)}, ambiguous={ambiguous}, policy={policy})")

    # confirm: advise only — do not inject TODO (that would break the one-word confirm gate)
    if policy == "recommend" and level == "confirm" and many_gaps:
        return _plan(
            "yes", "main-agent", "before-run", 1, "pending",
            f"confirm card but {len(gaps)} inputs still missing — grill before the run; status is pending (does not block agree)")

    if policy == "recommend" and kind in ("design-decision", "analyze-decide-fix"):
        return _plan(
            "optional", "panel-leader", "after-findings", 1, "skipped",
            "inputs look complete; panel leader may recommend an after-findings grill in the decision table")

    return _plan("no", "none", "never", 0, "skipped",
                 "default: no grill — override on the session card if you want one")


def _plan(do: str, who: str, when: str, rounds: int, status: str, reason: str) -> dict:
    return {
        "do": do,
        "who": who,
        "when": when,
        "max_rounds": rounds,
        "status": status,
        "skill": "grilling",
        "reason": reason,
    }


_INTERVIEWERS = ("main-agent", "user")


def card_section(g: dict) -> str:
    """Markdown section embedded in the session agreement. Avoid the substring TODO except as status value."""
    section = f"""## Grill plan
(skill `grilling` via `/grill-me` — rounds of frontier questions with recommended answers; not a peer panel)
- do: {g['do']}
- who: {g['who']}   # main-agent | panel-leader | user | none
- when: {g['when']}   # before-agree | before-run | after-findings | never
- max_rounds: {g['max_rounds']}
- status: {g['status']}   # open marker blocks agree; else pending | done | skipped
- skill: {g['skill']}
- reason: {g['reason']}
"""
    if g.get("do") == "yes" and g.get("who") in _INTERVIEWERS:
        section += """
## Grill log
(log EVERY question asked as `- Q [decision|fact|analysis]: …` (untagged = decision), each answered below by one of
`  A (user <session id>): "<the user's words, verbatim>"` · `  A (source <abs path>): "<verbatim quote>"` ·
`  A (peer <name> <conv id>): <gist>` · `  A (main): …` (never counts). decision ⇐ user or source;
fact/analysis ⇐ source or peer — look facts up yourself or ask a peer, put only decisions to the user.
done/skipped is refused until every question has an answer the gate can verify on disk.)
"""
    return section


def print_line(g: dict) -> str:
    return (f"grill      {g['do'].upper()} — who={g['who']} when={g['when']} "
            f"rounds≤{g['max_rounds']} status={g['status']}\n"
            f"           {g['reason']}")


def parse_card(text: str) -> dict | None:
    """Read a Grill plan section back from an agreement card (for drift-free reprint)."""
    m = re.search(r"(?ms)^## Grill plan\n(.*?)(?=^## |\Z)", text)
    if not m:
        return None
    body = m.group(1)
    out: dict = {}
    for key in ("do", "who", "when", "max_rounds", "status", "skill", "reason"):
        km = re.search(rf"^- {key}:\s*(.+)$", body, re.M)
        if not km:
            continue
        val = km.group(1).split("  #")[0].strip()
        if key == "max_rounds":
            try:
                val = int(val)
            except ValueError:
                pass
        out[key] = val
    return out if out.get("do") else None

def strip_grill_section(text: str) -> str:
    """Agreement body without the Grill plan section (so its status: TODO is not a field TODO)."""
    return re.sub(r"(?ms)^## Grill plan\n.*?(?=^## |\Z)", "", text)


def open_field_todos(text: str) -> list[str]:
    """Tool-minted TODO markers outside the Grill plan section.

    Matches bridge-plan negotiate fields (`TODO: …` or a bare `TODO` line), `…: TODO`,
    `**TODO**`, or a list item that is only TODO — not free-text "TODO" mid-sentence
    (e.g. "remove TODO comments").
    """
    out: list[str] = []
    for ln in strip_grill_section(text).splitlines():
        if re.search(r"^\s*TODO(?::|\s*$)|:\s*TODO\b|\*\*TODO\*\*|^\s*-\s*TODO\b", ln):
            out.append(ln.strip())
    return out


PROJECTS = Path.home() / ".claude" / "projects"   # Claude Code transcripts: what the user really typed
CONV = STATE / "conv"                             # one dir per bridged peer conversation
_ACCEPTS = {"decision": ("user", "source"), "fact": ("source", "peer"), "analysis": ("source", "peer")}
_MIN_QUOTE = {"user": 2, "source": 12}


def _norm(s: str) -> str:
    return " ".join(s.split())


def _quote(rest: str) -> str:
    m = re.search(r'"([^"]+)"|“([^”]+)”', rest)
    return _norm(m.group(1) or m.group(2)) if m else ""


def _user_texts(session: str) -> list[str]:
    """What the human wrote in one top-level transcript: typed prompts, mid-turn messages, AskUserQuestion answers.
    Compact summaries, meta records and other tool results are written by the agent side and are skipped."""
    out: list[str] = []
    for f in PROJECTS.glob(f"*/{session}*.jsonl"):
        for ln in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if r.get("type") == "queue-operation" and r.get("operation") == "enqueue":
                out.append(str(r.get("content") or ""))
            if r.get("type") != "user" or r.get("isMeta") or r.get("isCompactSummary"):
                continue
            tur = r.get("toolUseResult")
            if isinstance(tur, dict) and "answers" in tur:
                out += [str(v) for v in (tur.get("answers") or {}).values()]
                out += [str(a.get("notes", "")) for a in (tur.get("annotations") or {}).values() if isinstance(a, dict)]
                continue
            if tur is not None:
                continue
            c = (r.get("message") or {}).get("content")
            out += [c] if isinstance(c, str) else [b.get("text", "") for b in c or [] if b.get("type") == "text"]
    return [_norm(t) for t in out]


def _verified(kind: str, who: str, ref: str, rest: str, seen: dict) -> bool:
    if who not in _ACCEPTS[kind]:
        return False
    if who == "peer":
        conv = ref.split()[-1] if ref.split() else ""
        return bool(re.fullmatch(r"\w[\w.-]*", conv)) and (CONV / conv).is_dir()
    quote = _quote(rest)
    if len(quote) < _MIN_QUOTE[who] or not ref:
        return False
    if who == "user":
        if len(ref) < 8 or not re.fullmatch(r"[0-9a-f-]+", ref):
            return False
        if ref not in seen:
            seen[ref] = _user_texts(ref)
        return any(quote in t for t in seen[ref])
    path = Path(ref).expanduser()
    return path.is_absolute() and path.is_file() and quote in _norm(path.read_text(errors="replace"))


def log_problems(text: str) -> list[str]:
    """Questions under `## Grill log` with no answer the gate can verify on disk (empty list = every one has)."""
    m = re.search(r"(?ms)^## Grill log\n(.*?)(?=^## |\Z)", text)
    items: list[list] = []   # [kind, question, [(who, ref, rest)]]
    for ln in (m.group(1) if m else "").splitlines():
        q = re.match(r"^- Q(?: \[(decision|fact|analysis)\])?:\s*(.+)$", ln)
        a = re.match(r"^\s+A \((user|source|peer|main)\b\s*([^)]*)\):?\s*(.*)$", ln)
        if q:
            items.append([q.group(1) or "decision", q.group(2).strip(), []])
        elif a and items:
            items[-1][2].append((a.group(1), a.group(2).strip(), a.group(3)))
    if not items:
        return ["no question logged under ## Grill log"]
    seen: dict = {}
    return [f"[{kind}] {question[:60]!r} — needs a verified {' or '.join(_ACCEPTS[kind])} answer"
            for kind, question, answers in items
            if not any(_verified(kind, who, ref, rest, seen) for who, ref, rest in answers)]


def unevidenced_close(g: dict, text: str) -> str | None:
    """do=yes grill closed (done/skipped) by an interviewer while a logged question lacks a verifiable answer —
    the main agent answering its own frontier from session context is not a grill."""
    if g.get("do") != "yes" or g.get("who") not in _INTERVIEWERS or g.get("status") not in ("done", "skipped"):
        return None
    problems = log_problems(text)
    if not problems:
        return None
    more = f" (+{len(problems) - 3} more)" if len(problems) > 3 else ""
    return (f"Grill plan status={g.get('status')} but {len(problems)} grill question(s) are unanswered on disk: "
            + "; ".join(problems[:3]) + more)


def grill_blocks_agree(text: str) -> str | None:
    """If Grill plan is open (TODO) or closed without a logged answer before-agree/before-run, return a reason."""
    g = parse_card(text)
    if not g:
        return None
    if g.get("when") not in ("before-agree", "before-run"):
        return None
    if g.get("status") == "TODO":
        return f"Grill plan status=TODO (do={g.get('do')} when={g.get('when')} who={g.get('who')})"
    return unevidenced_close(g, text)


def gate_before_run(text: str) -> tuple[str | None, str | None]:
    """(hard_error, warning) checked by bridge-panel / bridge-work before peers spend.

    do=yes + when in before-agree|before-run + status=TODO → refuse.
    do=yes + status=pending → warn (confirm depth: agree already passed).
    do=optional + status=TODO → warn only.
    do=yes + who=main-agent|user + done/skipped with no user/peer answer in ## Grill log → refuse.
    after-findings / never / evidenced done / skipped → no signal.
    """
    g = parse_card(text)
    if not g:
        return None, None
    do, when, status = g.get("do"), g.get("when"), g.get("status")
    if when not in ("before-agree", "before-run"):
        return None, None
    if status in ("done", "skipped"):
        return unevidenced_close(g, text), None
    who = g.get("who", "?")
    if do == "yes" and status == "TODO":
        return (f"Grill plan still open (do=yes when={when} status=TODO who={who}). "
                f"Run grilling and set status to done before this run."), None
    if do == "yes" and status == "pending":
        return None, (f"Grill plan pending (when={when}, who={who}) — grill before peers spend, "
                      f"or set status to done/skipped")
    if do == "optional" and status == "TODO":
        return None, (f"optional Grill plan still TODO (when={when}, who={who}) — grill or set status skipped")
    return None, None
