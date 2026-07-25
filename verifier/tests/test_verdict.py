import pytest

from vouch_verifier import Tolerance, Verdict, compare

INDICATOR = Tolerance(rel=1e-6, display_rel=0.005)
PRICE = Tolerance(abs=0.01)
COUNT = Tolerance()


def test_exact_match_supported():
    assert compare(62.3, 62.3, INDICATOR) is Verdict.SUPPORTED


def test_display_rounding_supported():
    # 62.3 reported as "62" is legitimate display rounding.
    assert compare(62.0, 62.3, INDICATOR) is Verdict.SUPPORTED


def test_hallucination_contradicted():
    # 62.3 reported as "68" is the deadly class.
    assert compare(68.0, 62.3, INDICATOR) is Verdict.CONTRADICTED


def test_price_within_cent():
    assert compare(181.50, 181.505, PRICE) is Verdict.SUPPORTED


def test_price_off_by_dollar():
    assert compare(182.50, 181.50, PRICE) is Verdict.CONTRADICTED


def test_count_must_be_exact():
    assert compare(5, 5, COUNT) is Verdict.SUPPORTED
    assert compare(6, 5, COUNT) is Verdict.CONTRADICTED


@pytest.mark.parametrize("claimed,actual", [(0.0, 0.0), (-3.2, -3.2)])
def test_zero_and_negative(claimed, actual):
    assert compare(claimed, actual, INDICATOR) is Verdict.SUPPORTED
