"""The message envelope of the bridge protocol (docs/PROTOCOL.md) — one JSON object per board message — and a
standard-library validator for the subset of JSON Schema the envelope schema uses.

The board (boardlib) stays the source of truth for what a message *is*; this module is the one place that turns a
board row into the envelope other agents, scripts and future transports read (`bridge-board read --json`), and that
checks an envelope against `docs/protocol/envelope.schema.json` plus the invariants the schema cannot express
(a request carries `shape:` and `to`, a `blocked` carries `missing:`, only the user posts `decision`, the derived
`headers` / `citations` fields match the body). `tests/test_protocol.py` holds the spec to this module and this
module to the board.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import evidence
from .core import ROOT

VERSION = 1
SCHEMA_PATH = ROOT / "docs" / "protocol" / "envelope.schema.json"
VECTORS_PATH = ROOT / "docs" / "protocol" / "vectors.json"
# the fields of one envelope, in the order they are written (a reader must accept any order)
FIELDS = ("v", "topic", "seq", "author", "kind", "to", "reply_to", "created_at", "body", "headers", "citations")
NO_VALUE = ("", "-", "none", "n/a", "na", "?")     # = boardlib.NO_VALUE; a `missing:` with one of these is no reason


def load_schema(path: Path = SCHEMA_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def header(body: str) -> dict[str, str]:
    """`key: value` lines at the top of a body, up to the first blank or non-header line — the same reading as
    boardlib.header (kept here too so a transport without the board can parse a body the same way)."""
    out: dict[str, str] = {}
    for ln in body.splitlines():
        key, sep, value = ln.partition(":")
        if not sep or not key.strip() or " " in key.strip():
            break
        out[key.strip().lower()] = value.strip()
    return out


def citations(body: str) -> list[dict[str, Any]]:
    """Every `path.ext:line[-end]` the body cites, in order, each once — what the file:line rule is checked on."""
    out: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for m in evidence.CITE_RE.finditer(body or ""):
        rec: dict[str, Any] = {"path": m.group(1).lstrip("./"), "line": int(m.group(2))}
        if m.group(3):
            rec["end_line"] = int(m.group(3))
        key = (rec["path"], rec["line"], rec.get("end_line"))
        if key not in seen:
            seen.add(key)
            out.append(rec)
    return out


def envelope(topic: str, row: dict) -> dict:
    """A board row (`seq, author, kind, body, reply_to, created_at, to` — what boardlib.read returns) as an envelope."""
    body = row["body"]
    return {"v": VERSION, "topic": topic, "seq": row["seq"], "author": row["author"], "kind": row["kind"],
            "to": row.get("to"), "reply_to": row.get("reply_to"), "created_at": row.get("created_at"), "body": body,
            "headers": header(body), "citations": citations(body)}


# ----------------------------------------------------------------------------- validator
# The subset of JSON Schema 2020-12 that envelope.schema.json uses: $ref into $defs, type, const, enum, required,
# properties, additionalProperties (bool or schema), pattern, minimum, minLength, items, anyOf, not. Keeping to the
# subset is part of the contract — a keyword this validator does not know is a test failure, not a silent pass.
KNOWN = {"$schema", "$id", "$defs", "$ref", "title", "description", "type", "const", "enum", "required", "properties",
         "additionalProperties", "pattern", "minimum", "minLength", "items", "anyOf", "not", "examples"}


def _is_type(value: Any, t: str) -> bool:
    if t == "null":
        return value is None
    if t == "boolean":
        return isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "string":
        return isinstance(value, str)
    if t == "array":
        return isinstance(value, list)
    if t == "object":
        return isinstance(value, dict)
    raise ValueError(f"unknown type {t!r} in schema")


def validate(value: Any, schema: dict | bool, root: dict | None = None, path: str = "$") -> list[str]:
    """Every way `value` fails `schema`, as `path: reason` lines; empty = valid."""
    if schema is True:
        return []
    if schema is False:
        return [f"{path}: not allowed"]
    root = root if root is not None else schema
    unknown = set(schema) - KNOWN
    if unknown:
        raise ValueError(f"schema keyword(s) outside the supported subset at {path}: {sorted(unknown)}")
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            raise ValueError(f"only local $ref is supported (got {ref!r})")
        target: Any = root
        for part in ref[2:].split("/"):
            target = target[part]
        return validate(value, target, root, path)
    errors: list[str] = []
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(value, t) for t in types):
            return [f"{path}: expected {' | '.join(types)}, got {type(value).__name__}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must be {json.dumps(schema['const'])}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")
    if "not" in schema and not validate(value, schema["not"], root, path):
        errors.append(f"{path}: matches the forbidden form")
    if "anyOf" in schema:
        branches = [validate(value, s, root, path) for s in schema["anyOf"]]
        if all(branches):
            errors.append(f"{path}: matches none of the allowed forms (" + "; ".join(b[0] for b in branches) + ")")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: {value!r} does not match /{schema['pattern']}/")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and "minimum" in schema \
            and value < schema["minimum"]:
        errors.append(f"{path}: below minimum {schema['minimum']}")
    if isinstance(value, dict):
        for key in schema.get("required", ()):
            if key not in value:
                errors.append(f"{path}: missing required field {key!r}")
        props = schema.get("properties", {})
        for key, val in value.items():
            if key in props:
                errors.extend(validate(val, props[key], root, f"{path}.{key}"))
            elif "additionalProperties" in schema:
                extra = schema["additionalProperties"]
                if extra is False:
                    errors.append(f"{path}: unknown field {key!r}")
                elif extra is not True:
                    errors.extend(validate(val, extra, root, f"{path}.{key}"))
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors.extend(validate(item, schema["items"], root, f"{path}[{i}]"))
    return errors


def check(env: dict, schema: dict | None = None) -> list[str]:
    """Schema validation plus the invariants of docs/PROTOCOL.md that a schema cannot say. Empty = conformant.
    `headers` and `citations` may be left out by a writer; when present they must be what the body yields, and the
    header lines of the body are held to the schema's `headers` subschema either way."""
    schema = schema or load_schema()
    errors = validate(env, schema)
    if errors:
        return errors
    body, kind, head = env["body"], env["kind"], header(env["body"])
    errors.extend(validate(head, schema["properties"]["headers"], schema, "$.body(header lines)"))
    if "headers" in env and env["headers"] != head:
        errors.append("$.headers: does not equal the header lines parsed from body")
    if "citations" in env and env["citations"] != citations(body):
        errors.append("$.citations: does not equal the file:line citations found in body")
    if kind == "request":
        if env.get("to") is None:
            errors.append("$.to: a request needs an owner (an identity, @role or *)")
        if not head.get("shape"):
            errors.append("$.body: a request needs a `shape: <what the answer must look like>` header line")
    if kind == "blocked" and head.get("missing", "").strip().lower() in NO_VALUE:
        errors.append("$.body: a blocked needs a `missing: <what you lack>` header line")
    if kind == "decision" and not env["author"].startswith("user:"):
        errors.append("$.author: only user:* posts a decision")
    if kind == "task" and env["author"] != "board":
        errors.append("$.author: a task row is the board's own log line (author `board`)")
    if head.get("card") and (kind != "request" or not str(env.get("to") or "").startswith("user:")):
        errors.append("$.body: a card is a request addressed to user:*")
    if head.get("card") and head.get("shape") != head.get("card"):
        errors.append("$.body: a card's `shape:` equals its `card:` (decision or fact)")
    return errors
