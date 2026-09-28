"""examples/backtest, end to end: the committed log shows look-ahead
under --as-of, and re-recording it gives the same verdicts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import needs_proxy
from keys import EVAL_KEYS, EVAL_PUB

from vouch_verifier.cli import main as verify
from vouch_verifier.receipts import audit_log

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "backtest"


def _verify(
    receipts: Path, capsys: pytest.CaptureFixture[str], *extra: str
) -> tuple[int, dict[str, Any]]:
    code = verify(["--answer", str(EXAMPLE / "answer.txt"), "--receipts", str(receipts),
                   "--public-key", str(EVAL_PUB), "--format", "json", *extra])  # fmt: skip
    return code, json.loads(capsys.readouterr().out)


def _check(receipts: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, report = _verify(receipts, capsys)
    assert code == 0
    assert [v["verdict"] for v in report["verdicts"]] == [
        "SUPPORTED",
        "SUPPORTED",
        "DERIVED",
        "SUPPORTED",
    ]

    # Deciding at the July 22 close: the quote is from July 24.
    code, report = _verify(receipts, capsys, "--as-of", "2026-07-22")
    assert code == 1
    assert [v["verdict"] for v in report["verdicts"]] == [
        "SUPPORTED",
        "SUPPORTED",
        "DERIVED",
        "STALE",
    ]
    assert "look-ahead" in report["verdicts"][3]["note"]
    assert {r["tool"] for r in report["lookahead"]["receipts"]} == {"get_ohlcv", "get_quote"}

    code, report = _verify(receipts, capsys, "--as-of", "2026-07-24")
    assert code == 0 and report["lookahead"]["receipts"] == []


def test_the_committed_example(capsys: pytest.CaptureFixture[str]) -> None:
    log = EXAMPLE / "receipts" / "receipts.jsonl"
    head = (EXAMPLE / "receipts" / "HEAD").read_text().strip()
    audit_log(log, EVAL_KEYS, require_sealed=True, expect_head=head)
    _check(log, capsys)


@needs_proxy
def test_recording_it_again_gives_the_same_verdicts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = importlib.util.spec_from_file_location("backtest_record", EXAMPLE / "record.py")
    assert spec is not None and spec.loader is not None
    record = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(record)
    head = record.record(tmp_path)
    assert head.startswith("sha256:")
    _check(tmp_path, capsys)
