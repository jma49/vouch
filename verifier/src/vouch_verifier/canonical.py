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
import re


class _NumberLiteral:
    """Wraps a JSON number, preserving its exact source literal."""

    __slots__ = ("literal",)

    def __init__(self, literal: str) -> None:
        self.literal = literal


def parse_preserving(raw: str | bytes) -> object:
    """Parse JSON with number literals preserved (as _NumberLiteral).

    The parsed tree round-trips through serialize() byte-for-byte, which
    is what receipt signature re-verification depends on.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        return json.loads(raw, parse_float=_NumberLiteral, parse_int=_NumberLiteral)
    except json.JSONDecodeError as e:
        raise ValueError(f"canonicalize: parse: {e}") from e


def serialize(value: object) -> str:
    """Serialize a parse_preserving() tree in canonical form."""
    parts: list[str] = []
    _write(parts, value)
    return "".join(parts)


def number_value(value: object) -> float:
    """Return the numeric value of a preserved number literal."""
    if isinstance(value, _NumberLiteral):
        return float(value.literal)
    raise ValueError(f"not a number literal: {value!r}")


def canonicalize(raw: str | bytes) -> str:
    """Parse raw JSON and re-serialize it in canonical form.

    Raises ValueError on invalid JSON or trailing data.
    """
    return serialize(parse_preserving(raw))


# Where Go's encoding/json (which writes the receipts) and Python's
# json.dumps differ on strings, follow Go byte for byte (issue #9):
# - U+2028 and U+2029 are escaped by Go even with HTML escaping off;
# - a lone surrogate is replaced by U+FFFD in Go, while Python keeps it
#   and later fails to encode it as UTF-8.
# Pinned by testdata/canonical_vectors.json on both sides.
_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def _string(s: str) -> str:
    out = json.dumps(_SURROGATE_RE.sub("\ufffd", s), ensure_ascii=False)
    return out.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


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
        parts.append(_string(value))
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
            parts.append(_string(key))
            parts.append(":")
            _write(parts, value[key])
        parts.append("}")
    else:  # pragma: no cover - unreachable with the parse hooks above
        raise ValueError(f"canonicalize: unsupported type {type(value).__name__}")
