import pytest

from vouch_verifier import Tolerance, Verdict, compare

INDICATOR = Tolerance(rel=1e-6, display_rel=0.005)
PRICE = Tolerance(abs=0.01)
COUNT = Tolerance()


def test_exact_match_supported() -> None:
    assert compare(62.3, 62.3, INDICATOR) is Verdict.SUPPORTED


def test_display_rounding_supported() -> None:
    # 62.3 reported as "62" is legitimate display rounding.
    assert compare(62.0, 62.3, INDICATOR) is Verdict.SUPPORTED


def test_hallucination_contradicted() -> None:
    # 62.3 reported as "68" is the deadly class.
    assert compare(68.0, 62.3, INDICATOR) is Verdict.CONTRADICTED


def test_price_within_cent() -> None:
    assert compare(181.50, 181.505, PRICE) is Verdict.SUPPORTED


def test_price_off_by_dollar() -> None:
    assert compare(182.50, 181.50, PRICE) is Verdict.CONTRADICTED


def test_count_must_be_exact() -> None:
    assert compare(5, 5, COUNT) is Verdict.SUPPORTED
    assert compare(6, 5, COUNT) is Verdict.CONTRADICTED


@pytest.mark.parametrize("claimed,actual", [(0.0, 0.0), (-3.2, -3.2)])
def test_zero_and_negative(claimed: float, actual: float) -> None:
    assert compare(claimed, actual, INDICATOR) is Verdict.SUPPORTED


ROUNDING_PRICE = Tolerance(abs=0.01, display_round=True)


@pytest.mark.parametrize(
    ("claimed", "resolution", "verdict"),
    [
        (182.0, 1.0, Verdict.SUPPORTED),  # "182" asserts 181.5-182.5
        (181.0, 1.0, Verdict.CONTRADICTED),  # "181" asserts 180.5-181.5
        (181.5, 0.1, Verdict.SUPPORTED),
        (181.25, 0.01, Verdict.CONTRADICTED),  # digit swap keeps precision
        (1815.2, 0.1, Verdict.CONTRADICTED),  # magnitude shift
    ],
)
def test_display_round_uses_claim_precision(
    claimed: float, resolution: float, verdict: Verdict
) -> None:
    assert compare(claimed, 181.52, ROUNDING_PRICE, resolution) is verdict


def test_display_round_is_opt_in() -> None:
    # An unknown tolerance class falls back to Tolerance(); it must not
    # gain rounding slack from the claim's precision.
    assert compare(182.0, 181.52, Tolerance(), 1.0) is Verdict.CONTRADICTED
    assert compare(182.0, 181.52, PRICE, 1.0) is Verdict.CONTRADICTED


def test_exact_boundary_is_inclusive_despite_float_error() -> None:
    # 181.53 - 181.52 is 0.010000000000019 in binary floating point.
    assert compare(181.53, 181.52, PRICE) is Verdict.SUPPORTED
    assert compare(181.54, 181.52, PRICE) is Verdict.CONTRADICTED


def test_policy_is_printable() -> None:
    assert ROUNDING_PRICE.describe() == "abs=0.01 rel=0.0 display_rel=0.0 display_round=true"
    assert ROUNDING_PRICE.as_dict()["display_round"] is True


def test_float_epsilon_cannot_hide_a_displayed_unit() -> None:
    # Found by Hypothesis: a size-relative epsilon (1e-9 * 50,000 = 5e-5)
    # swallowed a one-unit error in a four-decimal claim.
    rounding = Tolerance(display_round=True)
    assert compare(50000.0001, 50000.0, rounding, 1e-4) is Verdict.CONTRADICTED
