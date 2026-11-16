"""Model-free checks on what a peer returned: do its `file:line` citations resolve, and does an answer have the shape
the request asked for. Both feed the ledger (`bridge-plan learn --team`) and gate a reply on the board."""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from pathlib import Path

from . import core

# a citation is `path.ext:line` where ext is something a repo holds source or text in — `api.example.com:443`,
# `v1.2:3` and `1.5:1` are not citations
SOURCE_EXT = ("py|js|mjs|cjs|ts|tsx|jsx|java|kt|kts|groovy|go|rs|rb|php|c|h|cc|cpp|hpp|cs|swift|m|mm|scala|clj|ex|exs|"
              "erl|hs|lua|pl|pm|r|jl|dart|sql|sh|zsh|bash|fish|ps1|md|mdc|mdx|rst|txt|yaml|yml|json|jsonl|toml|xml|html|"
              "htm|css|scss|less|vue|svelte|tf|hcl|gradle|properties|cfg|ini|conf|env|csv|proto|graphql|gql|dockerfile|"
              "makefile|cmake|lock")
# an absolute `/…/x.py:3` or `~/…` keeps its root: stripped to `Users/…` it resolved nowhere (2026-11-16: 120 of the
# misses in a month of ledger rows were files that existed)
CITE_RE = re.compile(rf"`?((?:~?/)?(?:[\w.-]+/)*[\w.-]+\.(?:{SOURCE_EXT})):(\d+)(?:-(\d+))?`?", re.I)
# `~` only as `~/`: `~10/20/30 ms` or `~build/x` is no one's home directory, and expanduser() raises on it
NAMED_PATH_RE = re.compile(r"(?:^|[\s`'\"(=<\[])((?:~(?=/)|/)[\w.@+-]*(?:/[\w.@+-]+)+)")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "target", "build", "dist", ".idea", ".gradle",
             ".mypy_cache", ".pytest_cache", ".tox"}
LOW_EVIDENCE_MIN = 2        # citations a role must have made before its evidence rate is judged
LOW_EVIDENCE_RATE = 0.5     # below this share of resolving citations the role is "low-evidence"
# Bumped with any change here that can move a citation's status. A ledger row records the revision that scored it and
# the scorecard counts only rows of the current one — an older resolver's misses were not the peer's (rev 2, 2026-11-16:
# absolute and ~/ paths, nested checkouts only after the workspace, --add-dir and problem-named roots, and no file read
# outside the roots the run was granted).
RESOLVER_REV = 2


def find_named(root: Path, name: str, limit: int = 2) -> list[Path]:
    """Files called `name` under root, skipping vendored and generated trees, stopping at `limit` hits. A nested
    checkout (a directory with its own `.git`, such as a bridge worktree under `.claude/worktrees/`) holds copies of
    the tree's files, so its hits count only when the tree itself has none — otherwise every file a bridge-work run
    touched reads as AMBIGUOUS in the repo it came from."""
    hits: list[Path] = []
    nested_hits: list[Path] = []
    nested: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        inside = any(dirpath == n or dirpath.startswith(n + os.sep) for n in nested)
        if not inside and Path(dirpath) != Path(root) and (".git" in dirnames or ".git" in filenames):
            nested.append(dirpath)
            inside = True
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if name in filenames:
            (nested_hits if inside else hits).append(Path(dirpath) / name)
            if len(hits) >= limit:
                break
    return hits or nested_hits[:limit]


def named_roots(text: str, add_dirs: Iterable[str] = (), cwd: str = ".") -> list[Path]:
    """Where a short citation may point outside the workspace: the `--add-dir` roots, each existing file the brief
    names by absolute path, and the roots `core.cited_roots` grants an ask or plan run for the paths the brief names —
    a git checkout strictly under HOME, one bridge `state/<kind>/<name>` folder, never the bridge checkout or
    `state/runs` (a peer cites `TaskClient.java:16` for a repo the brief named). A write run, whose grant stays
    explicit, is checked against them too, in its ledger counts and its `[bridge] citations` line. Every root is
    resolved: one place, one root."""
    add_dirs = list(add_dirs)
    out: dict[Path, None] = {}
    for d in add_dirs:
        out[(Path(cwd) / Path(d).expanduser()).resolve()] = None
    for m in NAMED_PATH_RE.finditer(text or ""):
        try:
            p = Path(m.group(1)).expanduser().resolve()
            if p.is_file():
                out[p] = None
        except (OSError, RuntimeError):         # an unreadable path, a symlink loop: no root
            continue
    try:
        out.update(dict.fromkeys(Path(r) for r in core.cited_roots(text or "", cwd, add_dirs)))
    except (OSError, RuntimeError):             # before 3.14 exists() raises on EACCES: no checkout, none walked
        pass
    return list(out)


def _shown(path: Path, root: Path) -> str:
    """Workspace files relative to it (so `/abs/app/x.py:4` and `app/x.py:4` are one row), others by their real path."""
    real, base = path.resolve(), root.resolve()
    return str(real.relative_to(base)) if real.is_relative_to(base) else str(real)


OUTSIDE = "OUTSIDE THE ROOTS (not read; --add-dir grants it)"


def _inside(path: Path, roots: Sequence[Path], as_written: bool = True) -> bool:
    """Whether `path` lies in one of the roots (for a file root: is that file) as resolved, so /tmp and /private/tmp
    are one place, or as written: a symlink a root holds belongs to it. A path that climbs counts only as resolved —
    the OS takes `..` after a link, so `linked/../x` lands beside the link's target, not in the root that holds the
    link."""
    written, real = Path(os.path.abspath(path)), path.resolve()
    return any(real.is_relative_to(r.resolve()) or as_written and written.is_relative_to(os.path.abspath(r))
               for r in roots)


def resolve_cited(root: Path, rel: str, extra: Sequence[Path] = ()) -> tuple[Path | None, str]:
    """A role may cite `cutoff.py:10` for `app/cutoff.py`, or a file in a checkout the brief named: resolve by path,
    then by basename — the workspace first, then the `extra` roots — before calling a citation broken. Nothing outside
    those roots is read, since a resolved line goes into the other roles' prompts (the panel's citation table): a file
    that exists elsewhere is reported unread (its full path names it, so no same-named file stands in for it), and an
    absolute path that does not exist is read as a workspace path, as a container's `/app/x.py` is. A `..` path is
    read where it lands, links followed; only one that names no file is looked up by basename. A root at or above HOME
    is never walked: a miss there walks everything."""
    bases, climbs = (root, *extra), ".." in Path(rel).parts
    if rel.startswith(("/", "~/")):
        absolute = Path(rel).expanduser()
        if absolute.is_file():
            if _inside(absolute, bases, as_written=not climbs):
                return absolute, _shown(absolute, root)
            return None, OUTSIDE
        rel = rel.lstrip("~/")
    outside = False
    for base in bases:
        if base.is_dir() and (base / rel).is_file():
            if not climbs or _inside(base / rel, bases, as_written=False):
                return base / rel, _shown(base / rel, root)
            outside = True              # it names a file, just not one a root holds: no same-named file stands in
    if outside:
        return None, OUTSIDE
    name, home = Path(rel).name, core.HOME.resolve()
    for group in ((root,), tuple(extra)):
        hits: list[Path] = []
        for b in group:
            if b.is_file():
                hits += [b] if b.name == name else []
            elif b.is_dir() and not home.is_relative_to(b.resolve()):
                hits += find_named(b, name)
        hits = list(dict.fromkeys(hits))
        if len(hits) == 1:
            return hits[0], _shown(hits[0], root)
        if len(hits) > 1:
            return None, f"AMBIGUOUS (several files named {name})"
    return None, "NO SUCH FILE"


def verify_citations(workspace: str, texts: dict[str, str], extra: Sequence[Path] = ()) -> list[dict]:
    """Check every `path:line` a role wrote against the workspace — the shared, model-free fact base. `extra` is
    `named_roots(brief, add_dirs)`: the other places the brief pointed the peer at."""
    root = Path(workspace)
    out: dict[tuple[str, int], dict] = {}
    seen: dict[str, tuple[Path | None, str]] = {}
    for role, text in texts.items():
        for m in CITE_RE.finditer(text or ""):
            rel, start = re.sub(r"^(\./)+", "", m.group(1)), int(m.group(2))   # not lstrip: `.github/…` keeps its dot
            if rel not in seen:
                try:
                    seen[rel] = resolve_cited(root, rel, extra)
                except (OSError, RuntimeError):     # before 3.14 is_file() raises on EACCES/EPERM; a symlink loop
                    seen[rel] = None, "UNREADABLE"
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
