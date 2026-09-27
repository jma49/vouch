"""Matching and verdict assignment against the Go golden receipt log."""

from pathlib import Path

import pytest
from envelopes import GOLDEN_KEYS

from vouch_verifier.claims import Extraction, extract_claims
from vouch_verifier.matcher import DEFAULT_TOLERANCES, MatchedClaim, load_tolerances, match_claims
from vouch_verifier.receipts import Fact, Receipt, load_log
from vouch_verifier.report import build_report, to_json, to_markdown
from vouch_verifier.verdict import Verdict

GOLDEN = Path(__file__).parent.parent.parent / "testdata" / "receipts_golden.jsonl"
ENTITIES = {"NVDA", "AMD"}


@pytest.fixture(scope="module")
def receipts() -> list[Receipt]:
    return load_log(GOLDEN, GOLDEN_KEYS)


def run(answer: str, receipts: list[Receipt]) -> tuple[Extraction, list[MatchedClaim]]:
    extraction = extract_claims(answer, ENTITIES)
    return extraction, match_claims(extraction, receipts)


def test_supported_exact_and_display_rounding(receipts: list[Receipt]) -> None:
    _, matched = run("NVDA closed at 181.52 with RSI at 62.", receipts)
    assert [mc.verdict for mc in matched] == [Verdict.SUPPORTED, Verdict.SUPPORTED]
    # "62" is display rounding of 62.3, allowed by display_rel.
    rsi = matched[1]
    assert rsi.fact is not None and rsi.fact.value == 62.3


def test_contradicted_the_deadly_class(receipts: list[Receipt]) -> None:
    _, matched = run("NVDA RSI(14) is at 68 right now.", receipts)
    assert len(matched) == 1
    assert matched[0].verdict is Verdict.CONTRADICTED
    assert matched[0].fact is not None
    assert matched[0].fact.value == 62.3
    assert "62.3" in matched[0].note


def test_unsupported_fabricated_from_memory(receipts: list[Receipt]) -> None:
    _, matched = run("TSLA closed at 244.10.", receipts)
    # TSLA is not a known entity -> unresolved -> UNVERIFIABLE; use a
    # known entity with an unreceipted metric for UNSUPPORTED:
    _, matched = run("NVDA volume was 99,000,000 shares.", receipts)
    assert len(matched) == 1
    assert matched[0].verdict is Verdict.UNSUPPORTED


def test_cited_claim_supported(receipts: list[Receipt]) -> None:
    _, matched = run("NVDA RSI(14) is 62.3 [[r:golden-0#/rsi_14]].", receipts)
    assert len(matched) == 1
    mc = matched[0]
    assert mc.verdict is Verdict.SUPPORTED
    assert mc.claim.tier == 1
    assert mc.receipt_id == "golden-0"


def test_cited_claim_contradicted(receipts: list[Receipt]) -> None:
    _, matched = run("NVDA RSI(14) is 68 [[r:golden-0#/rsi_14]].", receipts)
    assert matched[0].verdict is Verdict.CONTRADICTED


def test_fabricated_citation(receipts: list[Receipt]) -> None:
    _, matched = run("NVDA RSI(14) is 62.3 [[r:deadbeef#/rsi_14]].", receipts)
    assert matched[0].verdict is Verdict.UNSUPPORTED
    assert "does not exist" in matched[0].note


def test_citation_to_missing_fact(receipts: list[Receipt]) -> None:
    _, matched = run("NVDA RSI(14) is 62.3 [[r:golden-0#/nonexistent]].", receipts)
    assert matched[0].verdict is Verdict.UNSUPPORTED
    assert "no fact at" in matched[0].note


def test_sign_flip_caught(receipts: list[Receipt]) -> None:
    # Receipted change_pct is -1.35; claiming it as a gain contradicts.
    _, matched = run("AMD rose 1.35% on the day.", receipts)
    assert len(matched) == 1
    assert matched[0].verdict is Verdict.CONTRADICTED


def test_direction_word_supports_negative_fact(receipts: list[Receipt]) -> None:
    _, matched = run("AMD fell 1.35% on the day.", receipts)
    assert matched[0].verdict is Verdict.SUPPORTED


def test_unverifiable_counted_not_dropped(receipts: list[Receipt]) -> None:
    _, matched = run("The magic number is 42.", receipts)
    assert len(matched) == 1
    assert matched[0].verdict is Verdict.UNVERIFIABLE
    report = build_report(matched, DEFAULT_TOLERANCES)
    assert report.coverage == 0.0


def test_report_rendering(receipts: list[Receipt]) -> None:
    _, matched = run(
        "NVDA RSI(14) is 62.3 [[r:golden-0#/rsi_14]]. NVDA closed at 181.52. Answer is 42.",
        receipts,
    )
    report = build_report(matched, DEFAULT_TOLERANCES)
    md = to_markdown(report)
    assert "SUPPORTED: 2" in md
    assert "UNVERIFIABLE: 1" in md
    assert "Tier 1 (cited) share: 33%" in md
    assert "display_rel=0.005" in md
    js = to_json(report)
    assert '"coverage"' in js and '"tolerance_policy"' in js


def test_load_tolerances_matches_defaults(tmp_path: Path) -> None:
    repo_policy = Path(__file__).parent.parent.parent / "tolerance.yaml"
    assert load_tolerances(repo_policy) == DEFAULT_TOLERANCES

    bad = tmp_path / "bad.yaml"
    bad.write_text("price: { abs: 0.01, typo: 1 }\n")
    with pytest.raises(ValueError, match="unknown keys"):
        load_tolerances(bad)


def test_load_tolerances_rejects_non_bool_display_round(tmp_path: Path) -> None:
    # A quoted "yes" must not silently enable rounding slack.
    bad = tmp_path / "bad.yaml"
    bad.write_text('price: { abs: 0.01, display_round: "yes" }\n')
    with pytest.raises(ValueError, match="display_round"):
        load_tolerances(bad)


def _receipt(receipt_id: str, facts: tuple[Fact, ...], data_asof: str | None = None) -> Receipt:
    return Receipt(
        receipt_id=receipt_id,
        session_id=receipt_id,  # one turn per session keeps (session, turn) unique
        turn_index=0,
        tool_name="t",
        args_canonical="{}",
        result_canonical="{}",
        result_digest="",
        facts=facts,
        data_asof=data_asof,
        wall_time="2026-07-24T21:00:00Z",
        logical_time=0,
        upstream_latency_ms=0,
    )


def _close(value: float, as_of: str | None) -> Fact:
    return Fact("NVDA", "close_price", value, "USD", as_of, "1d", "/close", "price")


def test_undated_fact_takes_its_receipts_date() -> None:
    # Issue #14: an undated fact used to fall outside every window.
    receipts = [
        _receipt("undated", (_close(181.52, None),)),
        _receipt("older", (_close(100.0, "2026-07-20T20:00:00Z"),)),
    ]
    _, matched = run("NVDA closed at 181.52.", receipts)
    assert matched[0].verdict is Verdict.SUPPORTED
    _, matched = run("On July 24, NVDA closed at 181.52.", receipts)
    assert matched[0].verdict is Verdict.SUPPORTED
    _, matched = run("NVDA closed at 100.", receipts)
    assert matched[0].verdict is Verdict.STALE


def test_undated_fact_prefers_the_receipts_data_asof() -> None:
    receipts = [_receipt("r", (_close(181.52, None),), data_asof="2026-07-23T20:00:00Z")]
    _, matched = run("On July 23, NVDA closed at 181.52.", receipts)
    assert matched[0].verdict is Verdict.SUPPORTED


def test_exact_receipt_id_beats_a_longer_id_with_the_same_prefix() -> None:
    # Issue #17: "golden-1" is also a prefix of "golden-10".
    fact = Fact("NVDA", "rsi_14", 62.3, None, None, "1d", "/rsi_14", "indicator")
    receipts = [_receipt("golden-1", (fact,)), _receipt("golden-10", (fact,))]
    _, matched = run("NVDA RSI is 62.3 [[r:golden-1#/rsi_14]].", receipts)
    assert (matched[0].verdict, matched[0].receipt_id) == (Verdict.SUPPORTED, "golden-1")
    longer = [_receipt("golden-100", (fact,)), _receipt("golden-101", (fact,))]
    _, matched = run("NVDA RSI is 62.3 [[r:golden-10#/rsi_14]].", longer)
    assert "ambiguous" in matched[0].note
    # #95: a prefix under 8 characters cites nothing.
    _, matched = run("NVDA RSI is 62.3 [[r:golden#/rsi_14]].", receipts)
    assert (matched[0].verdict, matched[0].note) == (
        Verdict.UNSUPPORTED,
        "cited receipt 'golden' does not exist",
    )


def test_markdown_report_escapes_cells(receipts: list[Receipt]) -> None:
    # Issue #18: a json pointer with "|" and markup reached the note cell raw.
    _, matched = run("NVDA RSI is 62.3 [[r:golden-0#/x|y|<b>z</b>]].", receipts)
    md = to_markdown(build_report(matched, DEFAULT_TOLERANCES))
    row = next(line for line in md.splitlines() if "62.3" in line)
    assert row.count(" | ") == 6  # seven cells, as in the header
    assert "<b>" not in row and "&lt;b&gt;" in row and "\\|" in row
