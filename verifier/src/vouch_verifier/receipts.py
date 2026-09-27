"""Receipt log reading and signature verification.

Each line of a log is a DSSE envelope written by the Go proxy: its
payload is the canonical JSON body of one receipt, signed with Ed25519
over the exact payload bytes (signing.py). Verification therefore
checks bytes, not a re-serialization, and the body is parsed only after
its signature has been checked.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TextIO

from vouch_verifier.canonical import number_value, parse_preserving, serialize
from vouch_verifier.signing import RECEIPT_PAYLOAD_TYPE, Keyring, decode, open_envelope


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
    raw: object = field(repr=False, compare=False, default=None)
    # The whole tools/call result the agent received (#20). Absent in
    # logs written before the proxy recorded it; the signature, when
    # checked, covers it either way.
    payload_source: str | None = None
    response_canonical: str | None = None
    response_digest: str | None = None
    keyid: str | None = None  # the trusted key that signed it, when verified


class ReceiptError(ValueError):
    """A receipt failed structural, digest, or signature checks."""


def _sha256_digest(canonical: str) -> str:
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_receipt(line: str, lineno: int) -> Receipt:
    tree = parse_preserving(line)
    if not isinstance(tree, dict):
        raise ReceiptError(f"line {lineno}: receipt is not an object")

    def optional_text(key: str) -> str | None:
        v = tree.get(key)
        if v is not None and not isinstance(v, str):
            raise ReceiptError(f"line {lineno}: {key} is not a string")
        return v

    def text(key: str) -> str:
        v = optional_text(key)
        if v is None:
            raise ReceiptError(f"line {lineno}: missing {key}")
        return v

    def integer(key: str) -> int:
        try:
            return int(number_value(tree.get(key)))
        except (ValueError, OverflowError) as e:
            raise ReceiptError(f"line {lineno}: {key}: {e}") from e

    facts = []
    raw_facts = tree.get("facts")
    if raw_facts is not None:
        if not isinstance(raw_facts, list):
            raise ReceiptError(f"line {lineno}: facts is not an array")
        for f in raw_facts:
            if not isinstance(f, dict):
                raise ReceiptError(f"line {lineno}: fact is not an object: {f!r:.60}")
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
        payload_source=optional_text("payload_source"),
        response_canonical=(
            serialize(tree["response_canonical"]) if "response_canonical" in tree else None
        ),
        response_digest=optional_text("response_digest"),
        result_digest=text("result_digest"),
        facts=tuple(facts),
        data_asof=optional_text("data_asof"),
        wall_time=text("wall_time"),
        logical_time=integer("logical_time"),
        upstream_latency_ms=integer("upstream_latency_ms"),
        raw=tree,
    )


def _numbered_lines(f: TextIO) -> Iterator[tuple[int, str]]:
    lineno = 0
    try:
        for lineno, line in enumerate(f, start=1):
            yield lineno, line
    except UnicodeDecodeError as e:
        raise ReceiptError(f"line {lineno + 1}: not valid UTF-8: {e.reason}") from e


def load_log(path: str | Path, keys: Keyring | None = None) -> list[Receipt]:
    """Read a receipt log, enforcing the invariants the proxy promises.

    With keys, every line must carry a valid signature from one of them,
    checked before the body is parsed. Without keys, only structure is
    checked; callers must say so to their users. Always checked:
    result_digest and response_digest match what they cover, and
    receipt_id and (session_id, turn_index) are unique. Raises
    ReceiptError on any violation: a partially trusted log is not a
    thing.
    """
    receipts: list[Receipt] = []
    seen: dict[tuple[str, int], str] = {}
    ids: set[str] = set()
    # utf-8-sig: a byte-order mark from an editor is not a reason to reject
    # a log; a decoding error anywhere else is a ReceiptError (issue #16).
    with open(path, encoding="utf-8-sig") as f:
        for lineno, line in _numbered_lines(f):
            line = line.strip()
            if not line:
                continue
            try:
                envelope = json.loads(line)
                if keys is not None:
                    payload, keyid = open_envelope(envelope, RECEIPT_PAYLOAD_TYPE, keys)
                else:
                    payload, keyid = decode(envelope, RECEIPT_PAYLOAD_TYPE), None
                r = replace(_parse_receipt(payload.decode("utf-8"), lineno), keyid=keyid)
            except ReceiptError:
                raise
            except (ValueError, TypeError, AttributeError, OverflowError) as e:
                raise ReceiptError(f"line {lineno}: {e}") from e
            if r.receipt_id in ids:
                raise ReceiptError(f"line {lineno}: duplicate receipt_id {r.receipt_id}")
            ids.add(r.receipt_id)
            if _sha256_digest(r.result_canonical) != r.result_digest:
                raise ReceiptError(f"line {lineno}: result_digest does not match result_canonical")
            if (r.response_digest is None) != (r.response_canonical is None) or (
                r.response_canonical is not None
                and _sha256_digest(r.response_canonical) != r.response_digest
            ):
                raise ReceiptError(
                    f"line {lineno}: response_digest does not match response_canonical"
                )
            dup = seen.get((r.session_id, r.turn_index))
            if dup is not None:
                raise ReceiptError(
                    f"line {lineno}: duplicate (session_id={r.session_id}, "
                    f"turn_index={r.turn_index}), first seen as receipt {dup}"
                )
            seen[(r.session_id, r.turn_index)] = r.receipt_id
            receipts.append(r)
    return receipts
