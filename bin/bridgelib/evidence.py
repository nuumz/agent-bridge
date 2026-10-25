"""Model-free checks on what a peer returned: do its `file:line` citations resolve, and does an answer have the shape
the request asked for. Both feed the ledger (`bridge-plan learn --team`) and gate a reply on the board."""
from __future__ import annotations

import os
import re
from pathlib import Path

# a citation is `path.ext:line` where ext is something a repo holds source or text in — `api.example.com:443`,
# `v1.2:3` and `1.5:1` are not citations
SOURCE_EXT = ("py|js|mjs|cjs|ts|tsx|jsx|java|kt|kts|groovy|go|rs|rb|php|c|h|cc|cpp|hpp|cs|swift|m|mm|scala|clj|ex|exs|"
              "erl|hs|lua|pl|pm|r|jl|dart|sql|sh|zsh|bash|fish|ps1|md|mdc|mdx|rst|txt|yaml|yml|json|jsonl|toml|xml|html|"
              "htm|css|scss|less|vue|svelte|tf|hcl|gradle|properties|cfg|ini|conf|env|csv|proto|graphql|gql|dockerfile|"
              "makefile|cmake|lock")
CITE_RE = re.compile(rf"`?((?:[\w.-]+/)*[\w.-]+\.(?:{SOURCE_EXT})):(\d+)(?:-(\d+))?`?", re.I)
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "target", "build", "dist", ".idea", ".gradle",
             ".mypy_cache", ".pytest_cache", ".tox"}
LOW_EVIDENCE_MIN = 2        # citations a role must have made before its evidence rate is judged
LOW_EVIDENCE_RATE = 0.5     # below this share of resolving citations the role is "low-evidence"


def find_named(root: Path, name: str, limit: int = 2) -> list[Path]:
    """Files called `name` under root, skipping vendored and generated trees, stopping at `limit` hits."""
    hits: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if name in filenames:
            hits.append(Path(dirpath) / name)
            if len(hits) >= limit:
                break
    return hits


def resolve_cited(root: Path, rel: str) -> tuple[Path | None, str]:
    """A role may cite `cutoff.py:10` for `app/cutoff.py`; resolve by basename before calling a citation broken."""
    direct = root / rel
    if direct.is_file():
        return direct, rel
    hits = find_named(root, Path(rel).name)
    if len(hits) == 1:
        return hits[0], str(hits[0].relative_to(root))
    if len(hits) > 1:
        return None, f"AMBIGUOUS (several files named {Path(rel).name})"
    return None, "NO SUCH FILE"


def verify_citations(workspace: str, texts: dict[str, str]) -> list[dict]:
    """Check every `path:line` a role wrote against the workspace — the shared, model-free fact base."""
    root = Path(workspace)
    out: dict[tuple[str, int], dict] = {}
    seen: dict[str, tuple[Path | None, str]] = {}
    for role, text in texts.items():
        for m in CITE_RE.finditer(text or ""):
            rel, start = re.sub(r"^(\./)+", "", m.group(1)), int(m.group(2))   # not lstrip: `.github/…` keeps its dot
            if rel not in seen:
                seen[rel] = resolve_cited(root, rel)
            f, resolved = seen[rel]
            # key on the RESOLVED path: `cutoff.py:4` and `app/cutoff.py:4` are one line, so the shared record
            # must show one row (two rows read as two pieces of evidence for the same claim)
            key = (resolved if f else rel, start)
            rec = out.setdefault(key, {"cite": f"{resolved if f else rel}:{start}", "roles": [], "as_written": set(),
                                       "status": "", "text": ""})
            rec["as_written"].add(rel)
            if role not in rec["roles"]:
                rec["roles"].append(role)
            if rec["status"]:
                continue
            if f is None:
                rec["status"] = resolved
                continue
            try:
                lines = f.read_text(errors="replace").splitlines()
            except OSError:
                rec["status"] = "UNREADABLE"
                continue
            if not 1 <= start <= len(lines):
                rec["status"] = f"OUT OF RANGE (file has {len(lines)} lines)"
                continue
            rec["text"] = lines[start - 1].strip()[:160]
            rec["status"] = "ok" if rec["text"] else "BLANK LINE (wrong anchor?)"
    rows = []
    for rec in out.values():
        short = sorted(w for w in rec.pop("as_written") if not rec["cite"].startswith(w))
        if short:
            rec["cite"] += f"  (written as {', '.join(short)})"
        rows.append(rec)
    return sorted(rows, key=lambda r: (r["status"] == "ok", r["cite"]))


def citation_table(rows: list[dict]) -> str:
    if not rows:
        return ""
    bad = [r for r in rows if r["status"] != "ok"]
    head = ("## Verified citations — checked against the files, not by a model\n\n"
            f"{len(rows)} distinct citations, {len(bad)} that do not resolve.\n\n"
            "| citation | cited by | line on disk / problem |\n|---|---|---|\n")
    body = "".join(f"| `{r['cite']}` | {', '.join(r['roles'])} | "
                   f"{'`' + r['text'] + '`' if r['status'] == 'ok' else '**' + r['status'] + '**'} |\n" for r in rows)
    return head + body


def cite_score(rows: list[dict], role: str) -> tuple[int, int]:
    """(resolving, total) citations `role` made, in its finding and its cross-examination — the role's evidence rate."""
    names = {role, f"{role} (cross)"}
    mine = [r for r in rows if names & set(r["roles"])]
    return sum(1 for r in mine if r["status"] == "ok"), len(mine)


def low_evidence(rows: list[dict], roles: list[str]) -> dict[str, tuple[int, int]]:
    """Roles whose citations mostly did not resolve: at least LOW_EVIDENCE_MIN made, under LOW_EVIDENCE_RATE ok."""
    out = {}
    for role in roles:
        ok, total = cite_score(rows, role)
        if total >= LOW_EVIDENCE_MIN and ok / total < LOW_EVIDENCE_RATE:
            out[role] = (ok, total)
    return out


# the tokens of a `shape:` line a script can check; anything else is a human's call (shape_met returns None).
# table/decision are read with fenced code stripped (a table quoted inside ``` is not the answer) and need a data row
# under the separator (the requested header echoed back with nothing beneath is not one either).
_ROW = r"^[ \t]*\|[^\n]*\|[ \t]*$"
_SEP = r"^[ \t]*\|[ \t:|-]+\|[ \t]*$"
SHAPES: dict[str, re.Pattern] = {
    "table": re.compile(rf"{_ROW}\n{_SEP}\n^[ \t]*\|[^\n]*[A-Za-z0-9][^\n]*\|[ \t]*$", re.M),
    "file:line": CITE_RE,
    "json": re.compile(r"```json\b", re.I),
    "diff": re.compile(r"^(diff --git|--- a/|\+\+\+ b/)", re.M),
    "patch": re.compile(r"^(diff --git|--- a/|\+\+\+ b/)", re.M),
    "decision": re.compile(rf"{_SEP}\n^[ \t]*\|(?:[^|\n]*[A-Za-z0-9][^|\n]*\|){{4}}[ \t]*$", re.M),
    "command": re.compile(r"((^|\n)[ \t]*\$ \S|```(sh|bash|zsh|console)\b|`[a-z][\w.-]*(?: [^`\n]+)+`)", re.I),
}
UNFENCED = ("table", "decision")
NEGATED = re.compile(r"\b(?:no|not|without|never|instead of|rather than)\s+(?:an?\s+|the\s+)?(?P<tok>[\w:-]+)", re.I)
SUPPRESS = {"command": re.compile(r"\boutput\b", re.I)}    # "command output" asks for the output, not a command


def shape_tokens(shape: str) -> list[list[str]]:
    """The checkable tokens of a shape line, as alternatives (split on `or`), each a list of tokens that must all
    hold; negated tokens ("no table") and suppressed ones ("command output") are dropped."""
    negated = {m.group("tok").lower().rstrip("s") for m in NEGATED.finditer(shape)}
    groups = []
    for alt in re.split(r"\bor\b|/", shape, flags=re.I):
        toks = [k for k in SHAPES if re.search(rf"(?<![\w-]){re.escape(k)}s?(?![\w-])", alt, re.I)
                and k.rstrip("s") not in negated and not (k in SUPPRESS and SUPPRESS[k].search(shape))]
        if toks:
            groups.append(toks)
    return groups


def shape_met(shape: str | None, text: str) -> bool | None:
    """Does `text` carry the checkable tokens of `shape` (one alternative when the shape offers several)? None when
    the shape names nothing a script can check."""
    if not shape:
        return None
    groups = shape_tokens(shape)
    if not groups:
        return None
    text = text or ""
    unfenced = re.sub(r"```.*?```", "", text, flags=re.S)
    return any(all(SHAPES[k].search(unfenced if k in UNFENCED else text) for k in g) for g in groups)
