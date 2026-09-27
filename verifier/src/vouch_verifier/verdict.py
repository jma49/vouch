"""Verdict taxonomy and tolerance policy (docs/design.md section 6).

Six verdicts, not two: fabrication (UNSUPPORTED) and contradiction
(CONTRADICTED) are different failures with different fixes. Rounding is
not hallucination: the two-level tolerance (exact + display) separates
"62.3 reported as 62" (legitimate) from "62.3 reported as 68" (deadly).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class Verdict(StrEnum):
    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    UNSUPPORTED = "UNSUPPORTED"
    STALE = "STALE"
    DERIVED = "DERIVED"
    UNVERIFIABLE = "UNVERIFIABLE"


# Verdicts that mean the answer misreports its tools. STALE belongs here:
# the value was true once, but not for the time the claim is about.
FAILURES = frozenset({Verdict.CONTRADICTED, Verdict.UNSUPPORTED, Verdict.STALE})


@dataclass(frozen=True)
class Tolerance:
    """Tolerance policy for one class of fact (price, indicator, ...).

    abs: absolute tolerance (same unit as the value)
    rel: relative tolerance
    display_rel: additional relative slack for display-layer rounding
    display_round: allow half a unit of the claim's last displayed
        digit, so "182" is consistent with 181.52 and "181" is not.
        Off by default: an unknown class must not gain slack.
    """

    abs: float = 0.0
    rel: float = 0.0
    display_rel: float = 0.0
    display_round: bool = False

    def allowed(self, actual: float, resolution: float = 0.0) -> float:
        """Largest |claimed - actual| this policy accepts."""
        slack = max(self.abs, (self.rel + self.display_rel) * abs(actual))
        if self.display_round:
            slack = max(slack, resolution / 2)
        return slack

    def describe(self) -> str:
        return (
            f"abs={self.abs} rel={self.rel} display_rel={self.display_rel} "
            f"display_round={str(self.display_round).lower()}"
        )

    def as_dict(self) -> dict[str, float | bool]:
        return {
            "abs": self.abs,
            "rel": self.rel,
            "display_rel": self.display_rel,
            "display_round": self.display_round,
        }


# Absorbs binary floating-point error at exact tolerance boundaries
# (181.53 vs 181.52 under abs=0.01). Measured in units in the last
# place of the operands, not relative to their size: a relative epsilon
# of 1e-9 at 50,000 is 5e-5, wide enough to hide a whole-unit error in
# a four-decimal claim (found by tests/test_properties.py).
_EPSILON_ULPS = 8


def compare(claimed: float, actual: float, tol: Tolerance, resolution: float = 0.0) -> Verdict:
    """Compare a claimed value against a receipted fact value.

    `resolution` is the unit of the claim's last displayed digit (0.1
    for "62.3"); it only matters under a display_round policy.
    Returns SUPPORTED within tolerance, CONTRADICTED otherwise. Which
    fact to compare against, and the remaining verdicts, are the
    matcher's job.
    """
    bound = tol.allowed(actual, resolution) + _EPSILON_ULPS * math.ulp(
        max(abs(claimed), abs(actual), 1.0)
    )
    if abs(claimed - actual) <= bound:
        return Verdict.SUPPORTED
    return Verdict.CONTRADICTED
