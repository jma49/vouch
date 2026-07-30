"""Repeated-run eval: metrics, variance machinery, and the N>=2 rule."""

from pathlib import Path

import pytest

from vouch_verifier.receipts import load_log

from vouch_harness.cli import main
from vouch_harness.eval import run_eval, summarize
from vouch_harness.report import to_json, to_markdown

import random

GOLDEN = Path(__file__).parent.parent.parent / "testdata" / "receipts_golden.jsonl"
KEY = b"vouch-golden-key"


@pytest.fixture(scope="module")
def receipts():
    return load_log(GOLDEN, key=KEY)


@pytest.fixture(scope="module")
def result(receipts):
    return run_eval(receipts, n=3, seed=0)


def test_single_run_refused(receipts):
    with pytest.raises(ValueError, match="single-run"):
        run_eval(receipts, n=1)


def test_detectable_mutations_have_full_recall(result):
    for mutation in ("digit_swap", "magnitude_shift", "sign_flip", "fabricated_citation"):
        series = result.recall_series(mutation)
        assert series and all(v == 1.0 for v in series), (mutation, series)


def test_known_misses_have_zero_recall(result):
    # Reported, not hidden: the MVP verifier cannot catch these yet.
    for mutation in ("false_absence",):
        series = result.recall_series(mutation)
        assert series and all(v == 0.0 for v in series), (mutation, series)


def test_no_false_positives_on_clean_answers(result):
    assert all(r.false_positive_rate == 0.0 for r in result.runs)


def test_deterministic_pipeline_is_stable(result):
    assert result.stability == 1.0


def test_summarize_stats():
    s = summarize([0.8, 1.0, 0.9], random.Random(0))
    assert s.lo == 0.8 and s.hi == 1.0
    assert abs(s.mean - 0.9) < 1e-9
    assert s.ci_lo <= s.mean <= s.ci_hi


def test_reports_render(result):
    md = to_markdown(result)
    assert "Per-mutation recall" in md
    assert "digit_swap" in md and "false_absence" in md
    assert "does not report single-run scores" in md
    js = to_json(result)
    assert '"per_mutation_recall"' in js and '"stability"' in js


def test_cli_refuses_single_run(capsys):
    rc = main(["--receipts", str(GOLDEN), "--n", "1"])
    assert rc == 2
    assert "single-run" in capsys.readouterr().err


def test_cli_end_to_end(capsys, monkeypatch):
    monkeypatch.setenv("VOUCH_HMAC_KEY", KEY.decode())
    rc = main(["--receipts", str(GOLDEN), "--n", "2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "vouch eval report" in out
    assert "Runs: 2" in out
