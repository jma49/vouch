"""Canonical JSON serialization, matching the Go proxy byte-for-byte.

Seed contract (see docs/design.md section 7):
  - object keys sorted lexicographically, recursively
  - compact output, no insignificant whitespace
  - UTF-8 passthrough (ensure_ascii=False)
  - number literals preserved as they appeared in the source document

Implementation mirrors the Go side: parse with number-literal
preservation, then walk the tree with an explicit recursive writer.
(A json.JSONEncoder subclass is not used deliberately: CPython's
C-accelerated encoder bypasses __repr__ overrides on float subclasses,
which silently reformats numbers.) Full RFC 8785 number normalization
is a tracked follow-up; cross-language behavior is pinned by
testdata/canonical_vectors.json.
"""

from __future__ import annotations

import json


class _NumberLiteral:
    """Wraps a JSON number, preserving its exact source literal."""

    __slots__ = ("literal",)

    def __init__(self, literal: str) -> None:
        self.literal = literal


def canonicalize(raw: str | bytes) -> str:
    """Parse raw JSON and re-serialize it in canonical form.

    Raises ValueError on invalid JSON or trailing data.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        value = json.loads(raw, parse_float=_NumberLiteral, parse_int=_NumberLiteral)
    except json.JSONDecodeError as e:
        raise ValueError(f"canonicalize: parse: {e}") from e
    parts: list[str] = []
    _write(parts, value)
    return "".join(parts)


def _write(parts: list[str], value: object) -> None:
    if value is None:
        parts.append("null")
    elif value is True:
        parts.append("true")
    elif value is False:
        parts.append("false")
    elif isinstance(value, _NumberLiteral):
        parts.append(value.literal)
    elif isinstance(value, str):
        parts.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, list):
        parts.append("[")
        for i, elem in enumerate(value):
            if i:
                parts.append(",")
            _write(parts, elem)
        parts.append("]")
    elif isinstance(value, dict):
        parts.append("{")
        for i, key in enumerate(sorted(value)):
            if i:
                parts.append(",")
            parts.append(json.dumps(key, ensure_ascii=False))
            parts.append(":")
            _write(parts, value[key])
        parts.append("}")
    else:  # pragma: no cover - unreachable with the parse hooks above
        raise ValueError(f"canonicalize: unsupported type {type(value).__name__}")
