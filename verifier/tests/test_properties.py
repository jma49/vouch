"""Property-based tests (Hypothesis) for tokenization and comparison.

The corpus pins sentences someone thought of; these pin invariants over
inputs nobody wrote down: spans always index the source, formatted
numbers parse back to their value, and display rounding is never
reported as a contradiction.
"""

from __future__ import annotations

import math
from itertools import pairwise

from hypothesis import given, settings
from hypothesis import strategies as st

from vouch_verifier.claims import extract_claims
from vouch_verifier.tokens import tokenize
from vouch_verifier.verdict import Tolerance, Verdict, compare

ROUNDING = Tolerance(display_round=True)

# Text drawn from the characters the tokenizer actually reacts to, plus
# arbitrary unicode, so generated inputs hit the interesting paths.
_prose = st.lists(
    st.one_of(
        st.sampled_from(list("0123456789.,-+%$/: ()xkmMB;")),
        st.sampled_from(["July ", "Q3 ", " million", "50-day ", "NVDA ", " RSI ", " closed at "]),
        st.characters(),
    ),
    max_size=40,
).map("".join)


@settings(max_examples=500)
@given(_prose)
def test_spans_always_index_the_source(text: str) -> None:
    tokens = tokenize(text)
    for t in tokens:
        assert text[t.start : t.end] == t.text
        assert math.isfinite(t.value)
        assert t.resolution > 0
    starts = [t.start for t in tokens]
    assert starts == sorted(starts)
    for a, b in pairwise(tokens):
        assert a.end <= b.start, "tokens overlap"


@settings(max_examples=300)
@given(_prose)
def test_extraction_never_raises_and_stays_in_bounds(text: str) -> None:
    ex = extract_claims(text, known_entities={"NVDA", "AMD"})
    for c in (*ex.claims, *ex.unresolved):
        assert 0 <= c.span[0] < c.span[1] <= len(text)
        assert text[c.span[0] : c.span[1]] == c.text


@given(
    whole=st.integers(min_value=0, max_value=10**12),
    decimals=st.integers(min_value=0, max_value=4),
    frac=st.integers(min_value=0, max_value=9999),
    thousands=st.booleans(),
    negative=st.booleans(),
)
def test_formatted_numbers_parse_back(
    whole: int, decimals: int, frac: int, thousands: bool, negative: bool
) -> None:
    digits = f"{whole:,}" if thousands else str(whole)
    literal = digits + (f".{frac % 10**decimals:0{decimals}d}" if decimals else "")
    sign = "-" if negative else ""
    text = f"NVDA RSI is {sign}{literal} today"
    (tok,) = tokenize(text)
    expected = float(literal.replace(",", "")) * (-1 if negative else 1)
    assert tok.value == expected
    assert tok.text == sign + literal
    assert tok.resolution == 10.0**-decimals


@given(
    value=st.floats(min_value=0.001, max_value=1e6, allow_nan=False),
    scale=st.sampled_from([("k", 1e3), (" million", 1e6), ("B", 1e9)]),
)
def test_magnitudes_scale_value(value: float, scale: tuple[str, float]) -> None:
    literal = f"{value:.2f}"
    (tok,) = tokenize(f"volume was {literal}{scale[0]} shares")
    assert math.isclose(tok.value, float(literal) * scale[1], rel_tol=1e-12)
    assert math.isclose(tok.resolution, 0.01 * scale[1], rel_tol=1e-12)


@given(
    actual=st.floats(min_value=-1e7, max_value=1e7, allow_nan=False),
    decimals=st.integers(min_value=0, max_value=4),
)
def test_rounding_is_never_a_contradiction(actual: float, decimals: int) -> None:
    # Whatever the precision an agent chooses to display, the rounded
    # value is consistent with the receipted one.
    shown = round(actual, decimals)
    assert compare(shown, actual, ROUNDING, 10.0**-decimals) is Verdict.SUPPORTED


@given(
    actual=st.floats(min_value=-1e7, max_value=1e7, allow_nan=False),
    decimals=st.integers(min_value=0, max_value=4),
    units_off=st.integers(min_value=1, max_value=1000),
)
def test_an_error_of_a_whole_displayed_unit_is_contradicted(
    actual: float, decimals: int, units_off: int
) -> None:
    # Moving the shown value by at least one unit of its own last digit
    # leaves the rounding interval, so it cannot hide behind precision.
    resolution = 10.0**-decimals
    shown = round(actual, decimals) + units_off * resolution
    assert compare(shown, actual, ROUNDING, resolution) is Verdict.CONTRADICTED
