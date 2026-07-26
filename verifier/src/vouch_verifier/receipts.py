"""Receipt log reading and signature verification.

The Go proxy signs HMAC-SHA256 over canonical(receipt minus sig), and
appends canonical(signed receipt) to the JSONL log. Because canonical
form is key-sorted and number literals survive the round trip, dropping
the top-level "sig" key from a stored line and re-serializing yields
exactly the bytes the HMAC covers — no struct-order coupling with Go.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from pathlib import Path

from vouch_verifier.canonical import number_value, parse_preserving, serialize


@dataclass(frozen=True)
class Fact:
    """A verifiable atom extracted from a tool result (design section 3.2)."""

    entity: str
    metric: str
    value: float
    unit: str | None = None
    as_of: str | None = None
    timeframe: str | None = None
    json_ptr: str = ""
    tol_class: str = ""


@dataclass(frozen=True)
class Receipt:
    """One signed tool-call receipt (design section 3.1)."""

    receipt_id: str
    session_id: str
    turn_index: int
    tool_name: str
    args_canonical: str
    result_canonical: str
    result_digest: str
    facts: tuple[Fact, ...]
    data_asof: str | None
    wall_time: str
    logical_time: int
    upstream_latency_ms: int
    sig: str
    raw: object = field(repr=False, compare=False, default=None)


class ReceiptError(ValueError):
    """A receipt failed structural, digest, or signature checks."""


def _sha256_digest(canonical: str) -> str:
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_receipt(line: str, lineno: int) -> Receipt:
    tree = parse_preserving(line)
    if not isinstance(tree, dict):
        raise ReceiptError(f"line {lineno}: receipt is not an object")

    def text(key: str, required: bool = True) -> str | None:
        v = tree.get(key)
        if v is None:
            if required:
                raise ReceiptError(f"line {lineno}: missing {key}")
            return None
        if not isinstance(v, str):
            raise ReceiptError(f"line {lineno}: {key} is not a string")
        return v

    def integer(key: str) -> int:
        try:
            return int(number_value(tree.get(key)))
        except ValueError as e:
            raise ReceiptError(f"line {lineno}: {key}: {e}") from e

    facts = []
    raw_facts = tree.get("facts")
    if raw_facts is not None:
        if not isinstance(raw_facts, list):
            raise ReceiptError(f"line {lineno}: facts is not an array")
        for f in raw_facts:
            facts.append(
                Fact(
                    entity=f.get("entity", ""),
                    metric=f.get("metric", ""),
                    value=number_value(f.get("value")),
                    unit=f.get("unit"),
                    as_of=f.get("as_of"),
                    timeframe=f.get("timeframe"),
                    json_ptr=f.get("json_ptr", ""),
                    tol_class=f.get("tol_class", ""),
                )
            )

    return Receipt(
        receipt_id=text("receipt_id"),
        session_id=text("session_id"),
        turn_index=integer("turn_index"),
        tool_name=text("tool_name"),
        args_canonical=serialize(tree.get("args_canonical")),
        result_canonical=serialize(tree.get("result_canonical")),
        result_digest=text("result_digest"),
        facts=tuple(facts),
        data_asof=text("data_asof", required=False),
        wall_time=text("wall_time"),
        logical_time=integer("logical_time"),
        upstream_latency_ms=integer("upstream_latency_ms"),
        sig=text("sig"),
        raw=tree,
    )


def verify_receipt(r: Receipt, key: bytes) -> bool:
    """Recompute the HMAC over canonical(receipt minus sig)."""
    if not isinstance(r.raw, dict) or not r.sig.startswith("hmac-sha256:"):
        return False
    unsigned = {k: v for k, v in r.raw.items() if k != "sig"}
    payload = serialize(unsigned).encode("utf-8")
    mac = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest("hmac-sha256:" + mac, r.sig)


def load_log(path: str | Path, key: bytes | None = None) -> list[Receipt]:
    """Read a receipt log, enforcing the invariants the proxy promises.

    Always checked: result_digest matches result_canonical, and
    (session_id, turn_index) is unique. When key is given, every
    signature is verified too. Raises ReceiptError on any violation —
    a partially trusted log is not a thing.
    """
    receipts: list[Receipt] = []
    seen: dict[tuple[str, int], str] = {}
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            r = _parse_receipt(line, lineno)
            if _sha256_digest(r.result_canonical) != r.result_digest:
                raise ReceiptError(
                    f"line {lineno}: result_digest does not match result_canonical"
                )
            dup = seen.get((r.session_id, r.turn_index))
            if dup is not None:
                raise ReceiptError(
                    f"line {lineno}: duplicate (session_id={r.session_id}, "
                    f"turn_index={r.turn_index}), first seen as receipt {dup}"
                )
            seen[(r.session_id, r.turn_index)] = r.receipt_id
            if key is not None and not verify_receipt(r, key):
                raise ReceiptError(f"line {lineno}: signature verification failed")
            receipts.append(r)
    return receipts
