"""DERIVED: claims recomputed from a receipted series (design 6.2, #90)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch_verifier.claims import extract_claims
from vouch_verifier.lookahead import parse_moment
from vouch_verifier.matcher import MatchedClaim, match_claims
from vouch_verifier.receipts import Fact, Receipt
from vouch_verifier.verdict import Verdict
from vouch_verifier.vocabulary import load_vocabulary

CLOSES = {
    "2026-07-20": 170.00,
    "2026-07-21": 172.50,
    "2026-07-22": 168.40,
    "2026-07-23": 176.10,
    "2026-07-24": 181.52,
}


def _bars(closes: dict[str, float], rid: str = "ohlcv") -> Receipt:
    facts = tuple(
        Fact("NVDA", "close_price", v, "USD", f"{day}T20:00:00Z", None, f"/bars/{i}/close", "price")
        for i, (day, v) in enumerate(closes.items())
    )
    return Receipt(
        receipt_id=rid, session_id=rid, turn_index=0, tool_name="get_ohlcv",
        args_canonical="{}", result_canonical="{}", result_digest="", facts=facts,
        data_asof=None, wall_time="2026-07-24T21:00:00Z", logical_time=0,
        upstream_latency_ms=0,
    )  # fmt: skip


def judge(answer: str, receipts: list[Receipt] | None = None, **kw: object) -> MatchedClaim:
    extraction = extract_claims(answer, {"NVDA"})
    [mc] = match_claims(extraction, receipts or [_bars(CLOSES)], **kw)  # type: ignore[arg-type]
    return mc


def pct(a: str, b: str) -> float:
    return (CLOSES[b] / CLOSES[a] - 1) * 100


SINCE_20 = pct("2026-07-20", "2026-07-24")  # 6.78
LAST_3 = pct("2026-07-21", "2026-07-24")  # 5.23
DROP_22 = -pct("2026-07-21", "2026-07-22")  # 2.38
FROM_22 = pct("2026-07-22", "2026-07-24")  # 7.79


@pytest.mark.parametrize(
    ("answer", "verdict"),
    [
        (f"NVDA is up {SINCE_20:.1f}% since July 20.", Verdict.DERIVED),
        (f"NVDA is up {SINCE_20 + 2:.1f}% since July 20.", Verdict.CONTRADICTED),
        (f"NVDA rose {LAST_3:.2f}% over the past 3 sessions.", Verdict.DERIVED),
        (f"NVDA gained {LAST_3:.1f}% over the last 3 trading days.", Verdict.DERIVED),
        (f"NVDA fell {DROP_22:.2f}% from July 21 to July 22.", Verdict.DERIVED),
        (f"NVDA closed up {FROM_22:.1f}% from its July 22 close.", Verdict.DERIVED),
        ("NVDA hit a 3-day high of 181.52.", Verdict.DERIVED),
        ("NVDA hit a 3-day low of 168.40.", Verdict.DERIVED),
        ("NVDA's highest close over the past 5 sessions was 181.52.", Verdict.DERIVED),
        ("NVDA's lowest close over the past 2 sessions was 168.40.", Verdict.CONTRADICTED),
        ("NVDA is up 6.8% since July 1.", Verdict.UNSUPPORTED),
        ("NVDA hit a 10-day high of 181.52.", Verdict.UNSUPPORTED),
        ("NVDA rose 3.1% over the past 3 days.", Verdict.UNVERIFIABLE),
    ],
)  # fmt: skip
def test_derived_claims(answer: str, verdict: Verdict) -> None:
    mc = judge(answer)
    assert mc.verdict is verdict, mc.note


def test_the_note_shows_the_recomputation() -> None:
    mc = judge("NVDA is up 9% since July 20.")
    assert mc.verdict is Verdict.CONTRADICTED
    assert "recomputed NVDA close_price change since --07-20 = 6.776" in mc.note
    assert "2026-07-20 (170) to 2026-07-24 (181.52)" in mc.note


def test_point_claims_are_unchanged() -> None:
    assert judge("NVDA closed at 181.52.").verdict is Verdict.SUPPORTED
    assert judge("NVDA closed at 176.10 on July 23.").verdict is Verdict.SUPPORTED


def test_disagreeing_receipts_are_not_resolved_by_choice() -> None:
    other = _bars({"2026-07-20": 171.00}, rid="quote")
    mc = judge("NVDA is up 6.8% since July 20.", [_bars(CLOSES), other])
    assert mc.verdict is Verdict.UNSUPPORTED and "disagree" in mc.note


def test_as_of_limits_the_series() -> None:
    # At the July 22 close, "3-day high" means July 20 to July 22.
    mc = judge("NVDA hit a 3-day high of 172.50.", as_of=parse_moment("2026-07-22"))
    assert mc.verdict is Verdict.DERIVED, mc.note


def test_a_domain_without_a_series_derives_nothing() -> None:
    pack = Path(__file__).resolve().parents[2] / "examples" / "analytics" / "vocabulary.yaml"
    vocab = load_vocabulary(pack)
    claims = extract_claims("EMEA revenue rose 4% since July 1.", {"EMEA"}, vocab)
    assert all(c.derivation is None for c in (*claims.claims, *claims.unresolved))


def test_the_start_date_is_never_the_end_date() -> None:
    # #127: the previous phrase's "on July 20" is the base, not the end.
    since_20_to_23 = pct("2026-07-20", "2026-07-23")
    answer = (
        "As of the July 23 close, NVDA closed at 176.10, up from 170.00 on July 20, "
        f"a {since_20_to_23:.2f}% gain since July 20."
    )
    matched = match_claims(extract_claims(answer, {"NVDA"}), [_bars(CLOSES)])
    assert [m.verdict for m in matched] == [Verdict.SUPPORTED, Verdict.SUPPORTED, Verdict.DERIVED]
    assert "to --07-23" in matched[2].note
    # With no other date, the window ends on the latest receipted day.
    answer = f"Up from 170.00 on July 20, NVDA is up {SINCE_20:.1f}% since July 20."
    *_, mc = match_claims(extract_claims(answer, {"NVDA"}), [_bars(CLOSES)])
    assert mc.verdict is Verdict.DERIVED and "to " not in mc.note.split("=")[0], mc.note
