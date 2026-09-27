"""Regressions from the 2026-09-27 audit: claims the verifier passed or
failed wrongly (#94 DERIVED, #95 resolution). Each case is the audit's
reproduction."""

from __future__ import annotations

import pytest
from test_derived import CLOSES, _bars

from vouch_verifier.claims import extract_claims
from vouch_verifier.matcher import MatchedClaim, match_claims
from vouch_verifier.receipts import Fact, Receipt
from vouch_verifier.verdict import Verdict


def _receipt(rid: str, facts: tuple[Fact, ...]) -> Receipt:
    return Receipt(
        receipt_id=rid, session_id=rid, turn_index=0, tool_name="t", args_canonical="{}",
        result_canonical="{}", result_digest="", facts=facts, data_asof=None,
        wall_time="2026-07-24T21:00:00Z", logical_time=0, upstream_latency_ms=0,
    )  # fmt: skip


def judge(answer: str, receipts: list[Receipt]) -> list[MatchedClaim]:
    return match_claims(extract_claims(answer, {"NVDA", "AMD"}), receipts)


BARS = [_bars(CLOSES)]


@pytest.mark.parametrize(
    ("answer", "verdict"),
    [
        # #94.1: a change in another metric is not the close's.
        ("NVDA's RSI rose 6.8% since July 20.", Verdict.UNVERIFIABLE),
        ("NVDA volume is up 6.8% since July 20.", Verdict.UNVERIFIABLE),
        ("NVDA's RSI hit a 3-day high of 181.52.", Verdict.UNVERIFIABLE),
        ("NVDA volume hit a 5-day high of 181.52.", Verdict.UNVERIFIABLE),
        # #94.4: a stated date ends the window.
        ("On July 23, NVDA hit a 3-day high of 176.10.", Verdict.DERIVED),
        ("NVDA hit a 3-day high of 176.10 on July 23.", Verdict.DERIVED),
        (f"On July 23, NVDA was up {(176.10 / 172.50 - 1) * 100:.2f}% over the past 2 sessions.",
         Verdict.DERIVED),
    ],
)  # fmt: skip
def test_derived_claims_after_the_audit(answer: str, verdict: Verdict) -> None:
    [mc] = judge(answer, BARS)
    assert mc.verdict is verdict, mc.note


def test_cues_in_another_phrase_do_not_derive() -> None:
    # #94.3: the "since" belongs to another phrase; 3.08% is a day change.
    [mc] = judge("NVDA rose 3.08% on the day, its biggest gain since July 20.", BARS)
    assert mc.claim.derivation is None
    matched = judge(
        "NVDA traded near its high over the past 5 sessions and closed at 181.52.", BARS
    )
    assert [m.claim.derivation for m in matched] == [None]


def test_a_gapped_series_is_not_sessions() -> None:
    # #94.2: three receipted days weeks apart are not "the past 2 sessions".
    gapped = [
        _bars({"2026-07-01": 150.0}, rid="july-1"),
        _bars({"2026-07-23": 176.10, "2026-07-24": 181.52}, rid="late"),
    ]
    [mc] = judge("NVDA rose 21.0% over the past 2 sessions.", gapped)
    assert mc.verdict is Verdict.UNSUPPORTED, mc.note
    [mc] = judge("NVDA's lowest close over the past 3 sessions was 150.", gapped)
    assert mc.verdict is Verdict.UNSUPPORTED, mc.note


RSI = Fact("NVDA", "rsi_14", 62.3, None, "2026-07-24", "1d", "/rsi_14", "indicator")


@pytest.mark.parametrize(
    ("answer", "verdict"),
    [
        ("NVDA's RSI is 62.3 [[r:aaaa1111#/rsi_14]].", Verdict.SUPPORTED),
        ("AMD's RSI is 62.3 [[r:aaaa1111#/rsi_14]].", Verdict.UNSUPPORTED),
        ("NVDA's RSI on July 1 was 62.3 [[r:aaaa1111#/rsi_14]].", Verdict.UNSUPPORTED),
        ("NVDA's RSI is 62.3 [[r:a#/rsi_14]].", Verdict.UNSUPPORTED),
    ],
)
def test_citations_answer_to_the_prose(answer: str, verdict: Verdict) -> None:
    # #95.1
    [mc] = judge(answer, [_receipt("aaaa1111", (RSI,))])
    assert mc.verdict is verdict, mc.note


def test_disagreeing_timeframes_are_ambiguous() -> None:
    # #95.2: daily 62.3 and hourly 48.0 on one day; "RSI is 48" names none.
    hourly = Fact("NVDA", "rsi_14", 48.0, None, "2026-07-24", "1h", "/rsi_14", "indicator")
    receipts = [_receipt("daily", (RSI,)), _receipt("hourly", (hourly,))]
    [mc] = judge("NVDA's RSI is 48.", receipts)
    assert mc.verdict is Verdict.UNVERIFIABLE, mc.note
    [mc] = judge("NVDA's hourly RSI is 48.", receipts)
    assert mc.verdict is Verdict.SUPPORTED, mc.note


def test_a_pronoun_keeps_its_referent() -> None:
    # #95.3
    close = Fact("NVDA", "close_price", 181.52, "USD", "2026-07-24", None, "/c", "price")
    [mc] = judge(
        "AMD has been weak. It closed at 181.52 while NVDA rallied.", [_receipt("r", (close,))]
    )
    assert mc.claim.entity == "AMD" and mc.verdict is Verdict.UNSUPPORTED


@pytest.mark.parametrize(
    "answer",
    [
        "NVDA closed at 181.52, up from 176.10 on July 23.",
        "NVDA was down 5% since July 20 but closed at 181.52.",
    ],
)
def test_a_date_in_another_phrase_does_not_date_the_claim(answer: str) -> None:
    # #95.4
    matched = judge(answer, BARS)
    close = next(m for m in matched if m.claim.text == "181.52")
    assert close.claim.as_of is None and close.verdict is Verdict.SUPPORTED, close.note
