"""Matching and verdict assignment against the Go golden receipt log."""

from pathlib import Path

import pytest

from vouch_verifier.claims import extract_claims
from vouch_verifier.matcher import DEFAULT_TOLERANCES, load_tolerances, match_claims
from vouch_verifier.receipts import load_log
from vouch_verifier.report import build_report, to_json, to_markdown
from vouch_verifier.verdict import Verdict

GOLDEN = Path(__file__).parent.parent.parent / "testdata" / "receipts_golden.jsonl"
KEY = b"vouch-golden-key"
ENTITIES = {"NVDA", "AMD"}


@pytest.fixture(scope="module")
def receipts():
    return load_log(GOLDEN, key=KEY)


def run(answer, receipts):
    extraction = extract_claims(answer, ENTITIES)
    return extraction, match_claims(extraction, receipts)


def test_supported_exact_and_display_rounding(receipts):
    _, matched = run("NVDA closed at 181.52 with RSI at 62.", receipts)
    assert [mc.verdict for mc in matched] == [Verdict.SUPPORTED, Verdict.SUPPORTED]
    # "62" is display rounding of 62.3, allowed by display_rel.
    rsi = matched[1]
    assert rsi.fact is not None and rsi.fact.value == 62.3


def test_contradicted_the_deadly_class(receipts):
    _, matched = run("NVDA RSI(14) is at 68 right now.", receipts)
    assert len(matched) == 1
    assert matched[0].verdict is Verdict.CONTRADICTED
    assert matched[0].fact.value == 62.3
    assert "62.3" in matched[0].note


def test_unsupported_fabricated_from_memory(receipts):
    _, matched = run("TSLA closed at 244.10.", receipts)
    # TSLA is not a known entity -> unresolved -> UNVERIFIABLE; use a
    # known entity with an unreceipted metric for UNSUPPORTED:
    _, matched = run("NVDA volume was 99,000,000 shares.", receipts)
    assert len(matched) == 1
    assert matched[0].verdict is Verdict.UNSUPPORTED


def test_cited_claim_supported(receipts):
    _, matched = run("NVDA RSI(14) is 62.3 [[r:golden-0#/rsi_14]].", receipts)
    assert len(matched) == 1
    mc = matched[0]
    assert mc.verdict is Verdict.SUPPORTED
    assert mc.claim.tier == 1
    assert mc.receipt_id == "golden-0"


def test_cited_claim_contradicted(receipts):
    _, matched = run("NVDA RSI(14) is 68 [[r:golden-0#/rsi_14]].", receipts)
    assert matched[0].verdict is Verdict.CONTRADICTED


def test_fabricated_citation(receipts):
    _, matched = run("NVDA RSI(14) is 62.3 [[r:deadbeef#/rsi_14]].", receipts)
    assert matched[0].verdict is Verdict.UNSUPPORTED
    assert "does not exist" in matched[0].note


def test_citation_to_missing_fact(receipts):
    _, matched = run("NVDA RSI(14) is 62.3 [[r:golden-0#/nonexistent]].", receipts)
    assert matched[0].verdict is Verdict.UNSUPPORTED
    assert "no fact at" in matched[0].note


def test_sign_flip_caught(receipts):
    # Receipted change_pct is -1.35; claiming it as a gain contradicts.
    _, matched = run("AMD rose 1.35% on the day.", receipts)
    assert len(matched) == 1
    assert matched[0].verdict is Verdict.CONTRADICTED


def test_direction_word_supports_negative_fact(receipts):
    _, matched = run("AMD fell 1.35% on the day.", receipts)
    assert matched[0].verdict is Verdict.SUPPORTED


def test_unverifiable_counted_not_dropped(receipts):
    extraction, matched = run("The magic number is 42.", receipts)
    assert len(matched) == 1
    assert matched[0].verdict is Verdict.UNVERIFIABLE
    report = build_report(extraction, matched, DEFAULT_TOLERANCES)
    assert report.coverage == 0.0


def test_report_rendering(receipts):
    extraction, matched = run(
        "NVDA RSI(14) is 62.3 [[r:golden-0#/rsi_14]]. NVDA closed at 181.52. Answer is 42.",
        receipts,
    )
    report = build_report(extraction, matched, DEFAULT_TOLERANCES)
    md = to_markdown(report)
    assert "SUPPORTED: 2" in md
    assert "UNVERIFIABLE: 1" in md
    assert "Tier 1 (cited) share: 33%" in md
    assert "display_rel=0.005" in md
    js = to_json(report)
    assert '"coverage"' in js and '"tolerance_policy"' in js


def test_load_tolerances_matches_defaults(tmp_path):
    repo_policy = Path(__file__).parent.parent.parent / "tolerance.yaml"
    assert load_tolerances(repo_policy) == DEFAULT_TOLERANCES

    bad = tmp_path / "bad.yaml"
    bad.write_text("price: { abs: 0.01, typo: 1 }\n")
    with pytest.raises(ValueError, match="unknown keys"):
        load_tolerances(bad)
