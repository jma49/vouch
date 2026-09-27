"""Tamper suite, Python half (#55), run against the Go-written golden log.

Every way of altering a receipt log must be detected by the verifier or
be the one case, tail truncation, that only sealing or an externally
kept head digest can reveal (docs/threat-model.md). The Go half is
proxy/internal/store/tamper_test.go; the cases correspond one to one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from envelopes import (
    GOLDEN,
    GOLDEN_KEYS,
    OTHER_KEYS,
    bodies,
    edit_payload,
    golden_lines,
    signed_chain,
    write_log,
)

from vouch_verifier.cli import main
from vouch_verifier.receipts import ReceiptError, audit_log

R0, R1, R2, CP = golden_lines()


def _swap_signature(target: str, donor: str) -> str:
    envelope = json.loads(target)
    envelope["signatures"] = json.loads(donor)["signatures"]
    return json.dumps(envelope)


def _resigned_with_bad_link() -> list[str]:
    # A trusted key holder re-signs receipt 1 claiming seq 7.
    entries = [(kind, json.loads(body)) for kind, body in bodies([R0, R1])]
    return signed_chain(entries, overrides={1: {"seq": 7}})


CASES = [
    pytest.param([R0, R2, CP], GOLDEN_KEYS, "chain broken", id="delete-middle"),
    pytest.param([R1, R0, R2, CP], GOLDEN_KEYS, "chain broken", id="reorder"),
    pytest.param([R0, R1, R1, R2, CP], GOLDEN_KEYS, "chain broken", id="duplicate-line"),
    pytest.param([R1, R2, CP], GOLDEN_KEYS, "chain broken", id="delete-first"),
    pytest.param(
        [R0, edit_payload(R1, lambda b: b.replace("172.04", "172.40")), R2, CP],
        GOLDEN_KEYS,
        "line 2: no valid signature",
        id="edit-fact",
    ),
    pytest.param([R0, R1, R2, CP], OTHER_KEYS, "no valid signature", id="unknown-key"),
    pytest.param(
        [R0, R1, R2, _swap_signature(CP, R0)], GOLDEN_KEYS, "no valid signature", id="swap-sig"
    ),
    pytest.param(
        [R0, R1, R2, edit_payload(CP, lambda b: b.replace('"receipts":3', '"receipts":2'))],
        None,
        "checkpoint 3 counts 2 receipts",
        id="miscount-checkpoint",
    ),
]


@pytest.mark.parametrize(("lines", "keys", "message"), CASES)
def test_tampering_is_detected(
    tmp_path: Path, lines: list[str], keys: object, message: str
) -> None:
    log = write_log(tmp_path / "receipts.jsonl", lines)
    with pytest.raises(ReceiptError, match=message):
        audit_log(log, keys)  # type: ignore[arg-type]


def test_a_key_holder_cannot_rewrite_links(tmp_path: Path) -> None:
    log = write_log(tmp_path / "receipts.jsonl", _resigned_with_bad_link())
    with pytest.raises(ReceiptError, match="chain broken: entry has seq 7"):
        audit_log(log, GOLDEN_KEYS)


def test_golden_log_is_sealed_with_the_head_go_reports() -> None:
    audit = audit_log(GOLDEN, GOLDEN_KEYS, require_sealed=True)
    assert (len(audit.receipts), audit.checkpoints, audit.sealed) == (3, 1, True)
    # The same head `vouch receipts verify` prints for this file.
    assert audit.head == "sha256:79c6dbce9bc1d8b10c51b8789cccf5a678c0cbdaa3c59fe02229fa8a9d375e22"


def test_tail_truncation_needs_sealing_or_an_external_head(tmp_path: Path) -> None:
    head = audit_log(GOLDEN, GOLDEN_KEYS).head
    cut = write_log(tmp_path / "receipts.jsonl", [R0, R1])
    # A prefix of a valid chain is a valid chain: this passes on its own.
    audit = audit_log(cut, GOLDEN_KEYS)
    assert not audit.sealed and audit.head != head
    with pytest.raises(ReceiptError, match="does not end in a checkpoint"):
        audit_log(cut, GOLDEN_KEYS, require_sealed=True)
    with pytest.raises(ReceiptError, match="expected " + head):
        audit_log(cut, GOLDEN_KEYS, expect_head=head)


def test_cli_sealing_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOUCH_PUBLIC_KEY", str(GOLDEN.parent / "keys" / "golden.pub.pem"))
    answer = tmp_path / "answer.txt"
    answer.write_text("NVDA closed at 181.52.\n", encoding="utf-8")
    head = audit_log(GOLDEN, GOLDEN_KEYS).head
    args = ["--answer", str(answer), "--receipts", str(GOLDEN)]
    assert main([*args, "--require-sealed", "--expect-head", head]) == 0
    cut = write_log(tmp_path / "receipts.jsonl", [R0, R1])
    assert main(["--answer", str(answer), "--receipts", str(cut), "--require-sealed"]) == 2
