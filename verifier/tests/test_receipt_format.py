"""docs/receipt-format.md against this verifier (#124): the spec's field
tables match what the reader requires, and what the spec says a
verifier must refuse, this one refuses. Go has the same tests
(proxy/internal/receipt/format_test.go, store/tamper_test.go)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from envelopes import GOLDEN_KEYS, bodies, golden_lines, sign_line, signed_chain, write_log

from vouch_verifier.receipts import (
    CHECKPOINT_KEYS,
    FACT_KEYS,
    FACT_OPTIONAL,
    GENESIS,
    RECEIPT_KEYS,
    RECEIPT_OPTIONAL,
    ReceiptError,
    load_log,
)
from vouch_verifier.signing import CHECKPOINT_PAYLOAD_TYPE, RECEIPT_PAYLOAD_TYPE

SPEC = (Path(__file__).parents[2] / "docs" / "receipt-format.md").read_text(encoding="utf-8")
ROW = re.compile(r"^\| `([a-z_]+)` \| [^|]+ \| (yes|no) \|", re.MULTILINE)


def _table(heading: str) -> tuple[frozenset[str], frozenset[str]]:
    section = SPEC.split(f"\n{heading}\n", 1)[1].split("\n#", 1)[0]
    rows = ROW.findall(section)
    return (
        frozenset(k for k, req in rows if req == "yes"),
        frozenset(k for k, req in rows if req == "no"),
    )


@pytest.mark.parametrize(
    ("heading", "keys", "optional"),
    [
        ("### Receipt body", RECEIPT_KEYS, RECEIPT_OPTIONAL),
        ("### Fact", FACT_KEYS, FACT_OPTIONAL),
        ("### Checkpoint body", CHECKPOINT_KEYS, frozenset()),
    ],
)
def test_spec_tables_match_the_reader(
    heading: str, keys: frozenset[str], optional: frozenset[str]
) -> None:
    assert _table(heading) == (keys - optional, optional)


def test_spec_names_the_current_types() -> None:
    for literal in (RECEIPT_PAYLOAD_TYPE, CHECKPOINT_PAYLOAD_TYPE, GENESIS):
        assert literal in SPEC


def _entries() -> list[tuple[str, dict[str, Any]]]:
    return [(kind, json.loads(body)) for kind, body in bodies(golden_lines())]


def _without(
    entries: list[tuple[str, dict[str, Any]]], index: int, key: str, fact: bool = False
) -> None:
    body = entries[index][1]
    if fact:
        del body["facts"][0][key]
    else:
        del body[key]


@pytest.mark.parametrize(
    ("index", "key", "fact", "message"),
    [
        (0, "payload_source", False, "missing required key 'payload_source'"),
        (0, "args_canonical", False, "missing required key 'args_canonical'"),
        (0, "facts", False, "missing required key 'facts'"),
        (0, "json_ptr", True, "fact: missing required key 'json_ptr'"),
        (3, "sealed_at", False, "missing required key 'sealed_at'"),
    ],
)
def test_a_signed_body_missing_a_required_key_is_refused(
    tmp_path: Path, index: int, key: str, fact: bool, message: str
) -> None:
    entries = _entries()
    _without(entries, index, key, fact)
    log = write_log(tmp_path / "r.jsonl", signed_chain(entries))
    with pytest.raises(ReceiptError, match=message):
        load_log(log, GOLDEN_KEYS)


def test_a_signed_body_missing_both_response_fields_is_refused(tmp_path: Path) -> None:
    # Python used to accept this and Go to reject it (#124).
    entries = _entries()
    del entries[0][1]["response_canonical"], entries[0][1]["response_digest"]
    log = write_log(tmp_path / "r.jsonl", signed_chain(entries))
    with pytest.raises(ReceiptError, match="missing required key 'response_canonical'"):
        load_log(log, GOLDEN_KEYS)


def test_an_optional_key_may_be_absent(tmp_path: Path) -> None:
    entries = _entries()
    del entries[0][1]["data_asof"]
    for fact in entries[0][1]["facts"]:
        fact.pop("unit", None)
    log = write_log(tmp_path / "r.jsonl", signed_chain(entries))
    assert len(load_log(log, GOLDEN_KEYS)) == 3


def test_a_signed_body_not_in_canonical_form_is_refused(tmp_path: Path) -> None:
    # Whitespace inside result_canonical: Python used to re-serialize and
    # accept, Go digested the raw bytes and rejected (#124).
    kind, body = bodies(golden_lines()[:1])[0]
    start = body.index('"result_canonical":{') + len('"result_canonical":{')
    colon = body.index(":", start)
    spaced = body[: colon + 1] + " " + body[colon + 1 :]
    log = write_log(tmp_path / "r.jsonl", [sign_line(kind, spaced.encode("utf-8"))])
    with pytest.raises(ReceiptError, match="not in canonical form"):
        load_log(log, GOLDEN_KEYS)
