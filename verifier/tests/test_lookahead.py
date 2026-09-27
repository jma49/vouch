"""Look-ahead detection for backtests (design section 8.4, #84)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from envelopes import GOLDEN

from vouch_verifier.claims import extract_claims
from vouch_verifier.cli import main
from vouch_verifier.lookahead import find_lookahead, parse_moment
from vouch_verifier.matcher import match_claims
from vouch_verifier.receipts import Fact, Receipt
from vouch_verifier.verdict import Verdict


def _receipt(rid: str, value: float, as_of: str) -> Receipt:
    return Receipt(
        receipt_id=rid,
        session_id=rid,
        turn_index=0,
        tool_name="get_quote",
        args_canonical="{}",
        result_canonical="{}",
        result_digest="",
        facts=(Fact("NVDA", "close_price", value, "USD", as_of, "1d", "/close", "price"),),
        data_asof=as_of,
        wall_time="2026-07-24T21:00:00Z",
        logical_time=0,
        upstream_latency_ms=0,
    )


# A backtest set at the close of July 20: July 20 is known, July 24 is not.
RECEIPTS = [
    _receipt("past", 170.0, "2026-07-20T20:00:00Z"),
    _receipt("future", 181.52, "2026-07-24T20:00:00Z"),
]
AS_OF = parse_moment("2026-07-20T21:00:00Z")


def judge(answer: str) -> tuple[Verdict, str]:
    [mc] = match_claims(extract_claims(answer, {"NVDA"}), RECEIPTS, as_of=AS_OF)
    return mc.verdict, mc.note


def test_parse_moment_reads_a_bare_date_as_the_end_of_that_day() -> None:
    assert parse_moment("2026-07-20") > parse_moment("2026-07-20T23:59:00Z")
    assert parse_moment("2026-07-20") < parse_moment("2026-07-21T00:00:00Z")
    assert parse_moment("2026-07-20T15:00:00") == datetime(2026, 7, 20, 15, tzinfo=UTC)
    assert parse_moment("2026-07-20T17:00:00+02:00") == datetime(2026, 7, 20, 15, tzinfo=UTC)
    with pytest.raises(ValueError, match="ISO 8601"):
        parse_moment("July 20")


def test_find_lookahead_names_receipts_with_later_data() -> None:
    [la] = find_lookahead(RECEIPTS, AS_OF)
    assert (la.receipt_id, la.latest) == ("future", "2026-07-24T20:00:00Z")
    # Same-day daily data under an intraday as-of is flagged: the close
    # is not known until the day ends.
    daily = [_receipt("daily", 1.0, "2026-07-20")]
    assert find_lookahead(daily, parse_moment("2026-07-20T15:00:00Z"))
    assert not find_lookahead(daily, parse_moment("2026-07-20"))


def test_undated_claim_is_judged_against_the_latest_available_data() -> None:
    assert judge("NVDA closed at 170.")[0] is Verdict.SUPPORTED
    verdict, note = judge("NVDA closed at 181.52.")
    assert verdict is Verdict.STALE and note.startswith("look-ahead")


def test_claim_dated_in_the_future_is_look_ahead() -> None:
    verdict, note = judge("On July 24, 2026, NVDA closed at 181.52.")
    assert verdict is Verdict.STALE and "look-ahead" in note


def test_cited_future_data_is_look_ahead() -> None:
    verdict, note = judge("NVDA closed at 181.52 [[r:future#/close]].")
    assert verdict is Verdict.STALE and "look-ahead" in note


def test_without_as_of_nothing_changes() -> None:
    [mc] = match_claims(extract_claims("NVDA closed at 181.52.", {"NVDA"}), RECEIPTS)
    assert mc.verdict is Verdict.SUPPORTED


def test_cli_fails_a_backtest_that_saw_the_future(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The golden log's data is from the July 24, 2026 close.
    answer = tmp_path / "a.txt"
    answer.write_text("NVDA traded higher today.\n")  # no claims at all
    args = ["--answer", str(answer), "--receipts", str(GOLDEN), "--format", "json"]
    assert main(args) == 0
    capsys.readouterr()
    assert main([*args, "--as-of", "2026-07-20"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["lookahead"]["as_of"] == "2026-07-20"
    assert {la["receipt_id"] for la in report["lookahead"]["receipts"]} >= {"golden-0"}
    assert main([*args, "--as-of", "2026-07-24"]) == 0
    assert main([*args, "--as-of", "yesterday"]) == 2
