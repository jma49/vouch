"""Claim -> Fact matching and verdict assignment (design sections 3.3, 6).

MVP scope is the three-verdict line: SUPPORTED / CONTRADICTED /
UNSUPPORTED, plus UNVERIFIABLE for numeric spans extraction could not
resolve. STALE and DERIVED are explicitly later (design section 11).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from vouch_verifier.claims import Claim, Extraction
from vouch_verifier.index import build_index, facts_for
from vouch_verifier.receipts import Fact, Receipt
from vouch_verifier.verdict import Tolerance, Verdict, compare

DEFAULT_TOLERANCES: dict[str, Tolerance] = {
    "price": Tolerance(abs=0.01),
    "indicator": Tolerance(rel=1.0e-6, display_rel=0.005),
    "percentage": Tolerance(abs=0.05),
    "count": Tolerance(abs=0),
}


def load_tolerances(path: str | Path) -> dict[str, Tolerance]:
    """Load a tolerance policy file (design section 6.3)."""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    out: dict[str, Tolerance] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            raise ValueError(f"tolerance {name}: expected a mapping, got {spec!r}")
        unknown = set(spec) - {"abs", "rel", "display_rel"}
        if unknown:
            raise ValueError(f"tolerance {name}: unknown keys {sorted(unknown)}")
        out[name] = Tolerance(**spec)
    return out


@dataclass(frozen=True)
class MatchedClaim:
    """One claim with its verdict and, when matched, its fact."""

    claim: Claim
    verdict: Verdict
    fact: Fact | None = None
    receipt_id: str | None = None
    note: str = ""


def _tolerance_for(fact: Fact, tolerances: dict[str, Tolerance]) -> Tolerance:
    # Unknown tolerance class means exact comparison — the conservative
    # default; a typo in a schema must not loosen verification.
    return tolerances.get(fact.tol_class, Tolerance())


def _match_cited(
    claim: Claim, receipts: list[Receipt], tolerances: dict[str, Tolerance]
) -> MatchedClaim:
    assert claim.citation is not None
    matching = [r for r in receipts if r.receipt_id.startswith(claim.citation.receipt_id)]
    if not matching:
        return MatchedClaim(claim, Verdict.UNSUPPORTED,
                            note=f"cited receipt {claim.citation.receipt_id!r} does not exist")
    if len(matching) > 1:
        return MatchedClaim(claim, Verdict.UNSUPPORTED,
                            note=f"citation {claim.citation.receipt_id!r} is ambiguous "
                                 f"({len(matching)} receipts)")
    receipt = matching[0]
    for fact in receipt.facts:
        if fact.json_ptr == claim.citation.json_ptr:
            verdict = compare(claim.value, fact.value, _tolerance_for(fact, tolerances))
            return MatchedClaim(claim, verdict, fact=fact, receipt_id=receipt.receipt_id)
    return MatchedClaim(claim, Verdict.UNSUPPORTED, receipt_id=receipt.receipt_id,
                        note=f"receipt has no fact at {claim.citation.json_ptr}")


def match_claims(
    extraction: Extraction,
    receipts: list[Receipt],
    tolerances: dict[str, Tolerance] | None = None,
) -> list[MatchedClaim]:
    """Assign a verdict to every numeric span the extractor found.

    A claim with candidate facts is SUPPORTED if any candidate is within
    tolerance, otherwise CONTRADICTED against the closest candidate. A
    claim no receipt covers is UNSUPPORTED — fabricated from parametric
    memory. Unresolved spans are UNVERIFIABLE, and counted, because
    silently dropping them would overstate coverage.
    """
    tol = DEFAULT_TOLERANCES if tolerances is None else tolerances
    conn = build_index(receipts)
    out: list[MatchedClaim] = []

    for claim in extraction.claims:
        if claim.citation is not None:
            out.append(_match_cited(claim, receipts, tol))
            continue

        assert claim.entity is not None and claim.metric is not None
        candidates = facts_for(conn, claim.entity, claim.metric, claim.timeframe)
        if not candidates:
            out.append(MatchedClaim(claim, Verdict.UNSUPPORTED,
                                    note=f"no receipt covers ({claim.entity}, {claim.metric})"))
            continue

        best: tuple[float, str, Fact] | None = None
        for receipt_id, fact in candidates:
            if compare(claim.value, fact.value, _tolerance_for(fact, tol)) is Verdict.SUPPORTED:
                out.append(MatchedClaim(claim, Verdict.SUPPORTED, fact=fact, receipt_id=receipt_id))
                break
            distance = abs(claim.value - fact.value)
            if best is None or distance < best[0]:
                best = (distance, receipt_id, fact)
        else:
            assert best is not None
            _, receipt_id, fact = best
            out.append(MatchedClaim(claim, Verdict.CONTRADICTED, fact=fact, receipt_id=receipt_id,
                                    note=f"closest receipted value is {fact.value}"))

    for claim in extraction.unresolved:
        out.append(MatchedClaim(claim, Verdict.UNVERIFIABLE,
                                note="no entity/metric resolution (Tier 3 not enabled)"))
    out.sort(key=lambda mc: mc.claim.span)
    return out
