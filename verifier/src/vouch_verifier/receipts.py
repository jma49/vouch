"""Receipt log reading and signature verification.

Each line of a log is a DSSE envelope written by the Go proxy: its
payload is the canonical JSON body of one receipt, signed with Ed25519
over the exact payload bytes (signing.py). Verification therefore
checks bytes, not a re-serialization, and the body is parsed only after
its signature has been checked.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TextIO

from vouch_verifier.canonical import canonicalize, number_value, parse_preserving, serialize
from vouch_verifier.signing import (
    CHECKPOINT_PAYLOAD_TYPE,
    RECEIPT_PAYLOAD_TYPE,
    Keyring,
    decode,
    open_envelope,
)


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
    seq: int = 0  # position in the log (chain link, #54)
    prev_digest: str = ""  # digest of the previous entry's payload


class ReceiptError(ValueError):
    """A receipt failed structural, digest, signature, or chain checks."""


# The prev_digest of the first entry in a log.
GENESIS = "sha256:" + "0" * 64


@dataclass(frozen=True)
class LogAudit:
    """What reading a log established."""

    receipts: list[Receipt]
    checkpoints: int
    head: str  # digest of the last entry's payload, or GENESIS
    sealed: bool  # the last entry is a checkpoint


def _sha256_digest(canonical: str) -> str:
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# The keys the Go proxy writes (proxy/internal/receipt). A key that
# differs from one of these only in case is refused, as Go's reader
# refuses it: otherwise Go and this module could read different values
# out of one signed body (#98).
RECEIPT_KEYS = frozenset(
    {
        "receipt_id", "session_id", "turn_index", "tool_name", "args_canonical",
        "result_canonical", "result_digest", "payload_source", "response_canonical",
        "response_digest", "facts", "data_asof", "wall_time", "logical_time",
        "upstream_latency_ms", "seq", "prev_digest",
    }
)  # fmt: skip
CHECKPOINT_KEYS = frozenset({"seq", "prev_digest", "receipts", "session_id", "sealed_at"})
FACT_KEYS = frozenset(
    {"entity", "metric", "value", "unit", "as_of", "timeframe", "json_ptr", "tol_class"}
)
# What the writer omits when empty (Go's omitempty); every other key is
# required, in both verifiers and in docs/receipt-format.md (#124).
RECEIPT_OPTIONAL = frozenset({"data_asof"})
FACT_OPTIONAL = frozenset({"unit", "as_of", "timeframe"})


def _check_present(obj: dict[str, object], required: frozenset[str], where: str) -> None:
    missing = sorted(required - obj.keys())
    if missing:
        raise ReceiptError(f"{where}: missing required key {missing[0]!r}")


def _check_case(obj: dict[str, object], known: frozenset[str], where: str) -> None:
    folded = {k.lower(): k for k in known}
    for key in obj:
        if key not in known and key.lower() in folded:
            raise ReceiptError(
                f"{where}: key {key!r} differs from {folded[key.lower()]!r} only in case"
            )


def _check_fact_types(f: dict[str, object], lineno: int) -> None:
    """Fact fields of the wrong type are a malformed log, reported as
    such, not a TypeError deep in matching (#96)."""
    for key in ("entity", "metric", "json_ptr", "tol_class"):
        if not isinstance(f.get(key, ""), str):
            raise ReceiptError(f"line {lineno}: fact {key} is not a string")
    for key in ("unit", "as_of", "timeframe"):
        if f.get(key) is not None and not isinstance(f.get(key), str):
            raise ReceiptError(f"line {lineno}: fact {key} is not a string")


def _parse_receipt(line: str, lineno: int) -> Receipt:
    tree = parse_preserving(line)
    if not isinstance(tree, dict):
        raise ReceiptError(f"line {lineno}: receipt is not an object")
    _check_case(tree, RECEIPT_KEYS, f"line {lineno}")
    _check_present(tree, RECEIPT_KEYS - RECEIPT_OPTIONAL, f"line {lineno}")

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
            _check_case(f, FACT_KEYS, f"line {lineno}: fact")
            _check_present(f, FACT_KEYS - FACT_OPTIONAL, f"line {lineno}: fact")
            _check_fact_types(f, lineno)
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
        seq=integer("seq"),
        prev_digest=text("prev_digest"),
    )


def _numbered_lines(f: TextIO) -> Iterator[tuple[int, str]]:
    lineno = 0
    try:
        for lineno, line in enumerate(f, start=1):
            yield lineno, line
    except UnicodeDecodeError as e:
        raise ReceiptError(f"line {lineno + 1}: not valid UTF-8: {e.reason}") from e


def _link(tree: object, lineno: int) -> tuple[int, str]:
    if not isinstance(tree, dict):
        raise ReceiptError(f"line {lineno}: entry is not an object")
    seq, prev = tree.get("seq"), tree.get("prev_digest")
    try:
        seq_value = int(number_value(seq))
    except (ValueError, OverflowError) as e:
        raise ReceiptError(f"line {lineno}: seq: {e}") from e
    if not isinstance(prev, str):
        raise ReceiptError(f"line {lineno}: missing prev_digest")
    return seq_value, prev


def log_path(path: str | Path) -> Path:
    """A receipt log given as the file or as the directory the proxy
    writes it to (`vouch proxy --receipts <dir>`), so --receipts means
    the same thing in every command (#105)."""
    p = Path(path)
    return p / "receipts.jsonl" if p.is_dir() else p


def audit_log(
    path: str | Path,
    keys: Keyring | None = None,
    *,
    require_sealed: bool = False,
    expect_head: str | None = None,
) -> LogAudit:
    """Read a receipt log, enforcing every invariant the proxy promises.

    With keys, every entry must carry a valid signature from one of them,
    checked before its body is parsed; without keys, signatures are not
    checked and callers must say so to their users. Always checked:
    - the hash chain (#54): each entry's seq is its position and its
      prev_digest is the digest of the previous entry's payload, so
      deleting, reordering, or inserting entries is detected;
    - each checkpoint's receipt count;
    - result_digest and response_digest match what they cover;
    - receipt_id and (session_id, turn_index) are unique.
    Truncating the tail leaves a valid chain; require_sealed (the log
    ends in a checkpoint) and expect_head (a head digest kept outside
    the log) are how it is detected. Raises ReceiptError on any
    violation: a partially trusted log is not a thing.
    """
    receipts: list[Receipt] = []
    seen: dict[tuple[str, int], str] = {}
    ids: set[str] = set()
    head, seq, checkpoints, sealed = GENESIS, 0, 0, False
    # utf-8-sig: a byte-order mark from an editor is not a reason to reject
    # a log; a decoding error anywhere else is a ReceiptError (issue #16).
    with open(log_path(path), encoding="utf-8-sig") as f:
        for lineno, line in _numbered_lines(f):
            line = line.strip()
            if not line:
                continue
            try:
                # Duplicate keys are refused here as in Go (Canonicalize).
                parsed = parse_preserving(line)
                if not isinstance(parsed, dict):
                    raise ReceiptError(f"line {lineno}: not a DSSE envelope object")
                envelope: dict[str, Any] = parsed
                kind = envelope.get("payloadType")
                if kind not in (RECEIPT_PAYLOAD_TYPE, CHECKPOINT_PAYLOAD_TYPE):
                    raise ReceiptError(f"line {lineno}: unknown payload type {kind!r}")
                if keys is not None:
                    payload, keyid = open_envelope(envelope, kind, keys)
                else:
                    payload, keyid = decode(envelope, kind), None
                body = payload.decode("utf-8")
                # Exactly its canonical form, as in Go (#124): digests
                # inside are then over one byte string in every reader.
                if canonicalize(body) != body:
                    raise ReceiptError(
                        f"line {lineno}: payload is not in canonical form (docs/receipt-format.md)"
                    )
                tree = parse_preserving(body)
                entry_seq, entry_prev = _link(tree, lineno)
            except ReceiptError:
                raise
            except (ValueError, TypeError, AttributeError, OverflowError) as e:
                raise ReceiptError(f"line {lineno}: {e}") from e

            if entry_seq != seq:
                raise ReceiptError(
                    f"line {lineno}: chain broken: entry has seq {entry_seq}, want {seq} "
                    "(an entry was removed, inserted, or reordered)"
                )
            if entry_prev != head:
                raise ReceiptError(
                    f"line {lineno}: chain broken: prev_digest {entry_prev}, want {head}"
                )
            head = "sha256:" + hashlib.sha256(payload).hexdigest()
            seq += 1

            if kind == CHECKPOINT_PAYLOAD_TYPE:
                assert isinstance(tree, dict)
                _check_case(tree, CHECKPOINT_KEYS, f"line {lineno}")
                _check_present(tree, CHECKPOINT_KEYS, f"line {lineno}")
                try:
                    counted = int(number_value(tree.get("receipts")))
                except (ValueError, OverflowError) as e:
                    raise ReceiptError(f"line {lineno}: checkpoint receipts: {e}") from e
                if counted != len(receipts):
                    raise ReceiptError(
                        f"line {lineno}: checkpoint {entry_seq} counts {counted} receipts, "
                        f"the log has {len(receipts)}"
                    )
                checkpoints += 1
                sealed = True
                continue
            sealed = False

            try:
                r = replace(_parse_receipt(body, lineno), keyid=keyid)
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

    if require_sealed and not sealed:
        raise ReceiptError("log does not end in a checkpoint: it may have been cut short")
    if expect_head is not None and head != expect_head:
        raise ReceiptError(
            f"log head is {head}, expected {expect_head}: entries were removed or added"
        )
    return LogAudit(receipts=receipts, checkpoints=checkpoints, head=head, sealed=sealed)


def load_log(path: str | Path, keys: Keyring | None = None) -> list[Receipt]:
    """The receipts of a log, with every check of audit_log."""
    return audit_log(path, keys).receipts
