"""Malformed input fails cleanly: ReceiptError from the loader, exit 2
from the CLI (issue #16). Exit 1 is reserved for failing claims."""

from __future__ import annotations

from pathlib import Path

import pytest
from envelopes import GOLDEN, GOLDEN_KEYS, ROOT, edit_body, edit_payload, golden_lines

from vouch_verifier.cli import main
from vouch_verifier.receipts import ReceiptError, load_log

GOLDEN_PUB = str(ROOT / "testdata" / "keys" / "golden.pub.pem")
OTHER_PUB = str(ROOT / "testdata" / "keys" / "eval.pub.pem")


def _first_line_with(**changes: object) -> str:
    return edit_body(golden_lines()[0], lambda body: body.update(changes))


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (GOLDEN.read_text(encoding="utf-8") + "not json\n", "line 4"),
        (_first_line_with(facts=["not an object"]) + "\n", "fact is not an object"),
        (
            edit_payload(
                golden_lines()[0], lambda b: b.replace('"turn_index":0', '"turn_index":1e400')
            )
            + "\n",
            "turn_index",
        ),
        (
            GOLDEN.read_text(encoding="utf-8").splitlines()[0]
            + "\n"
            + _first_line_with(turn_index=99)
            + "\n",
            "duplicate receipt_id",
        ),
    ],
)
def test_malformed_logs_raise_receipt_errors(tmp_path: Path, content: str, message: str) -> None:
    log = tmp_path / "receipts.jsonl"
    log.write_text(content, encoding="utf-8")
    with pytest.raises(ReceiptError, match=message):
        load_log(log)


def test_invalid_utf8_is_a_receipt_error(tmp_path: Path) -> None:
    log = tmp_path / "receipts.jsonl"
    log.write_bytes(GOLDEN.read_bytes() + b"\xff\xfe\n")
    with pytest.raises(ReceiptError, match="not valid UTF-8"):
        load_log(log)


def test_a_byte_order_mark_is_tolerated(tmp_path: Path) -> None:
    log = tmp_path / "receipts.jsonl"
    log.write_bytes(b"\xef\xbb\xbf" + GOLDEN.read_bytes())
    assert len(load_log(log, GOLDEN_KEYS)) == 3


@pytest.fixture
def answer(tmp_path: Path) -> Path:
    path = tmp_path / "answer.txt"
    path.write_text("NVDA closed at 181.52.\n", encoding="utf-8")
    return path


def test_cli_exit_codes(
    answer: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("VOUCH_PUBLIC_KEY", GOLDEN_PUB)
    assert main(["--answer", str(answer), "--receipts", str(GOLDEN)]) == 0
    wrong = tmp_path / "wrong.txt"
    wrong.write_text("NVDA closed at 190.\n", encoding="utf-8")
    assert main(["--answer", str(wrong), "--receipts", str(GOLDEN)]) == 1

    assert main(["--answer", str(tmp_path / "missing.txt"), "--receipts", str(GOLDEN)]) == 2
    monkeypatch.setenv("VOUCH_PUBLIC_KEY", OTHER_PUB)
    assert main(["--answer", str(answer), "--receipts", str(GOLDEN)]) == 2
    err = capsys.readouterr().err
    assert "no valid signature" in err and "Traceback" not in err
