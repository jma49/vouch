"""Numeric span tokenization: what counts as a candidate claim."""

import pytest

from vouch_verifier.tokens import tokenize


def texts(s: str) -> list[str]:
    return [t.text for t in tokenize(s)]


@pytest.mark.parametrize(
    "s",
    [
        "2026-07-24",
        "2026-07-24T20:00:00Z",
        "7/24/2026",
        "7/24",
        "July 24, 2026",
        "Jul 24th",
        "24 July 2026",
        "July 2026",
        "4:00 pm",
        "16:00",
        "4pm",
        "Q3 2026",
        "FY2026",
        "since 2024",
        "the 3rd day",
        "50-day",
        "52-week",
        "5 sessions",
        "1d",
        "4h",
        "RSI(14)",
    ],
)
def test_structure_is_not_a_claim(s: str) -> None:
    assert texts(s) == []


def test_thousands_groups_do_not_swallow_trailing_comma() -> None:
    assert texts("RSI is 62, volume 52,410,000, and 1,234.5.") == ["62", "52,410,000", "1,234.5"]
    assert tokenize("52,410,000")[0].value == 52_410_000


def test_sign_and_percent() -> None:
    (tok,) = tokenize("change is -1.35%")
    assert (tok.text, tok.value, tok.unit) == ("-1.35%", -1.35, "pct")
    (tok,) = tokenize("up +2 %")
    assert (tok.text, tok.value, tok.unit) == ("+2 %", 2.0, "pct")


def test_multiplier_and_ranges_are_not_points() -> None:
    assert [(t.text, t.kind) for t in tokenize("volume was 3x average")] == [("3x", "multiple")]
    for s in ("RSI in the 60-65 range", "RSI between 60 and 65"):
        assert [(t.text, t.kind) for t in tokenize(s)] == [("60", "range"), ("65", "range")]


def test_move_endpoints_stay_points() -> None:
    # "from X to Y" describes a move in market prose, not a range.
    assert [t.kind for t in tokenize("RSI moved from 55.1 to 62.3")] == ["point", "point"]


def test_unrecognized_years_stay_candidates() -> None:
    # Masking is conservative: a bare four-digit number is left for
    # resolution to judge, since "closed at 2026" can be a price.
    assert texts("closed at 2026") == ["2026"]


def test_exclude_spans() -> None:
    s = "RSI 62.3, MACD 0.42"
    assert texts(s) == ["62.3", "0.42"]
    assert [t.text for t in tokenize(s, exclude=[(10, len(s))])] == ["62.3"]


def test_spans_index_the_source() -> None:
    s = "On July 24, NVDA closed at 181.52, up 1.9% on 3x volume."
    for t in tokenize(s):
        assert s[t.start : t.end] == t.text


@pytest.mark.parametrize(
    ("s", "text", "value", "resolution"),
    [
        ("52.4 million shares", "52.4 million", 52_400_000, 100_000),
        ("52.41M", "52.41M", 52_410_000, 10_000),
        ("$1.2B", "1.2B", 1_200_000_000, 100_000_000),
        ("2bn", "2bn", 2_000_000_000, 1_000_000_000),
        ("3.4K", "3.4K", 3_400, 100),
        ("187,340 thousand", "187,340 thousand", 187_340_000, 1_000),
        ("$15m raise", "15m", 15_000_000, 1_000_000),
    ],
)
def test_magnitudes_scale_value_and_resolution(
    s: str, text: str, value: float, resolution: float
) -> None:
    (tok,) = tokenize(s)
    assert tok.text == text
    assert tok.value == pytest.approx(value)
    assert tok.resolution == pytest.approx(resolution)


def test_magnitude_needs_a_word_boundary() -> None:
    # "5 more" is not five million; "4 bars" is a period, not billions.
    assert [t.value for t in tokenize("5 more catalysts")] == [5]
    assert texts("over 4 bars") == []
