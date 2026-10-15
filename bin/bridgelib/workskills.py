"""Work-skill packs for bridge-work — policy addenda selected by task/files/verify (Strategy registry, not a plugin SPI).

Skills live in ~/.agent-bridge/work-skills/*.md (frontmatter + ### implementer / ### reviewer body sections).
Same-named files in work-skills.local/ override (machine-local, not for remote).
`classify` scores trigger regexes like playbooks; the pipeline injects the winning pack into write/review prompts.
"""
from __future__ import annotations

import re
from pathlib import Path

from .core import ROOT

SKILLS = ROOT / "work-skills"
LOCAL_SKILLS = ROOT / "work-skills.local"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    meta: dict = {}
    body = text
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    if not m:
        return meta, body
    body = m.group(2).strip()
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] == '"':
            v = v[1:-1]
        meta[k.strip()] = v
    return meta, body


def _section(body: str, name: str) -> str:
    """Body text under `### <name>` until the next `###` or EOF."""
    m = re.search(rf"(?ims)^###\s+{re.escape(name)}\s*\n(.*?)(?=^###\s|\Z)", body)
    return m.group(1).strip() if m else ""


def _load_dir(folder: Path, out: dict[str, dict]) -> None:
    if not folder.is_dir():
        return
    for f in sorted(folder.glob("*.md")):
        meta, body = _parse_frontmatter(f.read_text())
        meta.setdefault("name", f.stem)
        meta["path"] = str(f)
        meta["implementer"] = _section(body, "implementer")
        meta["reviewer"] = _section(body, "reviewer")
        meta["body"] = body
        out[meta["name"]] = meta


def load_skills() -> dict[str, dict]:
    out: dict[str, dict] = {}
    _load_dir(SKILLS, out)
    _load_dir(LOCAL_SKILLS, out)
    return out


def classify(text: str, skills: dict[str, dict] | None = None) -> list[tuple[str, int]]:
    """Skills whose trigger hits `text`, best first (hit count, then earliest match)."""
    skills = skills if skills is not None else load_skills()
    scored: list[tuple[str, int, int]] = []
    for name, s in skills.items():
        pat = s.get("triggers")
        if not pat:
            continue
        try:
            hits = list(re.finditer(pat, text, re.I))
        except re.error:
            hits = []
        if hits:
            scored.append((name, len(hits), hits[0].start()))
    return [(n, c) for n, c, _ in sorted(scored, key=lambda x: (-x[1], x[2]))]


def pick(task: str, files: list[str] | None = None, verify: str = "",
         forced: str | None = None) -> dict | None:
    """Resolve one skill for a work run. `forced`: skill name, or 'none'/'off' to disable.

    Scoring: trigger hits on task+files+verify, plus a ×3 bonus for `paths:` matches on the file list
    (path evidence beats a coincidental word like Mapper inside UserMapperTest).
    """
    skills = load_skills()
    if forced:
        key = forced.strip().lower()
        if key in ("none", "off", "-"):
            return None
        if key not in skills:
            raise KeyError(f"unknown work-skill {forced!r}; see `bridge-work skills`")
        return skills[key]
    files = files or []
    blob = "\n".join([task or "", ",".join(files), verify or ""])
    file_blob = "\n".join(files)
    scored: list[tuple[str, int, int]] = []
    for name, s in skills.items():
        score = 0
        earliest = 10**9
        pat = s.get("triggers")
        if pat:
            try:
                hits = list(re.finditer(pat, blob, re.I))
            except re.error:
                hits = []
            score += len(hits)
            if hits:
                earliest = min(earliest, hits[0].start())
        path_pat = s.get("paths")
        if path_pat and file_blob:
            try:
                ph = list(re.finditer(path_pat, file_blob, re.I))
            except re.error:
                ph = []
            score += len(ph) * 3
            if ph:
                earliest = min(earliest, ph[0].start())
        if score:
            scored.append((name, score, earliest))
    if not scored:
        return None
    scored.sort(key=lambda x: (-x[1], x[2]))
    return skills[scored[0][0]]


def prompt_addenda(skill: dict | None) -> tuple[str, str]:
    """(implementer_block, reviewer_block) ready to append to prompts; empty strings if no skill."""
    if not skill:
        return "", ""
    name = skill.get("name", "?")
    impl = skill.get("implementer") or ""
    rev = skill.get("reviewer") or ""
    hint = skill.get("verify_hint") or ""
    impl_block = ""
    if impl or hint:
        impl_block = f"\n\n## Work skill `{name}` (follow in addition to the agreement)\n"
        if hint:
            impl_block += f"Preferred verify shape (human may have passed a different `--verify`): `{hint}`\n"
        if impl:
            impl_block += impl + "\n"
    rev_block = ""
    if rev:
        rev_block = f"\n\n## Work skill `{name}` — review focus\n{rev}\n"
    return impl_block, rev_block
