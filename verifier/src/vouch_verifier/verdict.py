"""Verdict taxonomy and tolerance policy (docs/design.md section 6).

Six verdicts, not two: fabrication (UNSUPPORTED) and contradiction
(CONTRADICTED) are different failures with different fixes. Rounding is
not hallucination: the two-level tolerance (exact + display) separates
"62.3 reported as 62" (legitimate) from "62.3 reported as 68" (deadly).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Verdict(str, Enum):
    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    UNSUPPORTED = "UNSUPPORTED"
    STALE = "STALE"
    DERIVED = "DERIVED"
    UNVERIFIABLE = "UNVERIFIABLE"


@dataclass(frozen=True)
class Tolerance:
    """Tolerance policy for one class of fact (price, indicator, ...).

    abs: absolute tolerance (same unit as the value)
    rel: relative tolerance
    display_rel: additional relative slack for display-layer rounding
    """

    abs: float = 0.0
    rel: float = 0.0
    display_rel: float = 0.0

    def _within(self, claimed: float, actual: float, extra_rel: float) -> bool:
        diff = abs(claimed - actual)
        allowed = max(self.abs, (self.rel + extra_rel) * abs(actual))
        return diff <= allowed


def compare(claimed: float, actual: float, tol: Tolerance) -> Verdict:
    """Compare a claimed value against a receipted fact value.

    Returns SUPPORTED when within (exact + display) tolerance,
    CONTRADICTED otherwise. Matching (which fact to compare against)
    and the remaining verdicts are handled by the matcher, not here.
    """
    if tol._within(claimed, actual, tol.display_rel):
        return Verdict.SUPPORTED
    return Verdict.CONTRADICTED
