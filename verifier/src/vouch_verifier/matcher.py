"""Claim -> Fact matching and verdict assignment (design sections 3.3, 6).

MVP scope is the three-verdict line: SUPPORTED / CONTRADICTED /
UNSUPPORTED, plus UNVERIFIABLE for numeric spans extraction could not
resolve. STALE and DERIVED are explicitly later (design section 11).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from vouch_verifier.claims import Claim, Extraction, negated_by_parentheses
from vouch_verifier.index import build_index, facts_for
from vouch_verifier.receipts import Fact, Receipt
from vouch_verifier.verdict import Tolerance, Verdict, compare

DEFAULT_TOLERANCES: dict[str, Tolerance] = {
    "price": Tolerance(abs=0.01, display_round=True),
    "indicator": Tolerance(rel=1.0e-6, display_rel=0.005, display_round=True),
    "percentage": Tolerance(abs=0.05, display_round=True),
    "count": Tolerance(abs=0, display_round=True),
}


def load_tolerances(path: str | Path) -> dict[str, Tolerance]:
    """Load a tolerance policy file (design section 6.3)."""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    out: dict[str, Tolerance] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            raise ValueError(f"tolerance {name}: expected a mapping, got {spec!r}")
        unknown = set(spec) - {"abs", "rel", "display_rel", "display_round"}
        if unknown:
            raise ValueError(f"tolerance {name}: unknown keys {sorted(unknown)}")
        if not isinstance(spec.get("display_round", False), bool):
            raise ValueError(f"tolerance {name}: display_round must be true or false")
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


def _judge(claim: Claim, fact: Fact, tolerances: dict[str, Tolerance]) -> Verdict:
    return compare(claim.value, fact.value, _tolerance_for(fact, tolerances), claim.resolution)


def _match_cited(
    claim: Claim, receipts: list[Receipt], tolerances: dict[str, Tolerance]
) -> MatchedClaim:
    citation = claim.citation
    assert citation is not None
    cited = citation.receipt_id
    # An exact id wins; a prefix is a convenience for long ids (issue #17).
    matching = [r for r in receipts if r.receipt_id == cited] or [
        r for r in receipts if r.receipt_id.startswith(cited)
    ]
    if not matching:
        return MatchedClaim(
            claim,
            Verdict.UNSUPPORTED,
            note=f"cited receipt {citation.receipt_id!r} does not exist",
        )
    if len(matching) > 1:
        return MatchedClaim(
            claim,
            Verdict.UNSUPPORTED,
            note=f"citation {citation.receipt_id!r} is ambiguous ({len(matching)} receipts)",
        )
    receipt = matching[0]
    for fact in receipt.facts:
        if fact.json_ptr == citation.json_ptr:
            if negated_by_parentheses(claim.parenthesized, claim.value, fact.metric):
                claim = replace(claim, value=-claim.value)
            # A citation says which fact is meant, not that any number the
            # fact happens to equal is the same kind of quantity: "$1.92"
            # citing a percentage is a contradiction (issue #13). A bare
            # number carries no unit and is taken as the cited fact's.
            if claim.unit and fact.unit and claim.unit != fact.unit:
                return MatchedClaim(
                    claim,
                    Verdict.CONTRADICTED,
                    fact=fact,
                    receipt_id=receipt.receipt_id,
                    note=f"claim is in {claim.unit}, cited fact is in {fact.unit}",
                )
            verdict = _judge(claim, fact, tolerances)
            return MatchedClaim(claim, verdict, fact=fact, receipt_id=receipt.receipt_id)
    return MatchedClaim(
        claim,
        Verdict.UNSUPPORTED,
        receipt_id=receipt.receipt_id,
        note=f"receipt has no fact at {citation.json_ptr}",
    )


_KIND_NOTES = {
    "multiple": "a multiplier is not a point value",
    "range": "a range is not a point value",
}


def _unresolved_note(claim: Claim) -> str:
    return _KIND_NOTES.get(claim.kind, "no entity/metric resolution (Tier 3 not enabled)")


def _day(as_of: str | None) -> str | None:
    return as_of[:10] if as_of else None


def _on_date(fact: Fact, date: str) -> bool:
    day = _day(fact.as_of)
    if day is None:
        return False
    # "--MM-DD" (no year in the text) matches that day in any year.
    return day[4:] == date[1:] if date.startswith("--") else day == date


def _closest(claim: Claim, pool: list[tuple[str, Fact]]) -> tuple[str, Fact]:
    return min(pool, key=lambda rf: abs(claim.value - rf[1].value))


def _match_uncited(
    claim: Claim, conn: sqlite3.Connection, tol: dict[str, Tolerance]
) -> MatchedClaim:
    """Judge a Tier 2 claim against the receipted facts for its time window.

    A stated date selects that day's facts; with no date the window is
    the latest receipted day. A value that matches only outside the
    window is STALE rather than SUPPORTED: it was true, but not for the
    time the claim is about (design section 6.1).
    """
    assert claim.entity is not None and claim.metric is not None
    key = ", ".join(x for x in (claim.entity, claim.metric, claim.timeframe) if x)
    candidates = facts_for(conn, claim.entity, claim.metric, claim.timeframe)
    if not candidates:
        return MatchedClaim(claim, Verdict.UNSUPPORTED, note=f"no receipt covers ({key})")

    if claim.as_of is not None:
        window = [rf for rf in candidates if _on_date(rf[1], claim.as_of)]
        if not window:
            return MatchedClaim(
                claim, Verdict.UNSUPPORTED, note=f"no receipt covers ({key}) on {claim.as_of}"
            )
        outside: list[tuple[str, Fact]] = []
    else:
        latest = max((_day(f.as_of) or "" for _, f in candidates), default="")
        window = [rf for rf in candidates if (_day(rf[1].as_of) or "") == latest]
        outside = [rf for rf in candidates if rf not in window]

    for receipt_id, fact in window:
        if _judge(claim, fact, tol) is Verdict.SUPPORTED:
            return MatchedClaim(claim, Verdict.SUPPORTED, fact=fact, receipt_id=receipt_id)
    for receipt_id, fact in outside:
        if _judge(claim, fact, tol) is Verdict.SUPPORTED:
            _, latest_fact = _closest(claim, window)
            return MatchedClaim(
                claim,
                Verdict.STALE,
                fact=fact,
                receipt_id=receipt_id,
                note=(
                    f"matches the value as of {_day(fact.as_of)}; "
                    f"latest receipted ({_day(latest_fact.as_of)}) is {latest_fact.value}"
                ),
            )
    receipt_id, fact = _closest(claim, window)
    return MatchedClaim(
        claim,
        Verdict.CONTRADICTED,
        fact=fact,
        receipt_id=receipt_id,
        note=f"closest receipted value is {fact.value}",
    )


def match_claims(
    extraction: Extraction,
    receipts: list[Receipt],
    tolerances: dict[str, Tolerance] | None = None,
) -> list[MatchedClaim]:
    """Assign a verdict to every numeric span the extractor found.

    Cited claims are judged against the fact their citation names;
    uncited ones against the facts in their time window (see
    _match_uncited). A claim no receipt covers is UNSUPPORTED —
    fabricated from parametric memory. Unresolved spans are
    UNVERIFIABLE, and counted, because silently dropping them would
    overstate coverage.
    """
    tol = DEFAULT_TOLERANCES if tolerances is None else tolerances
    out: list[MatchedClaim] = []

    # The index is per call and in-memory; close it so repeated calls
    # (the eval runs thousands) do not leak connections.
    with closing(build_index(receipts)) as conn:
        for claim in extraction.claims:
            if claim.citation is not None:
                out.append(_match_cited(claim, receipts, tol))
                continue

            out.append(_match_uncited(claim, conn, tol))

    for claim in extraction.unresolved:
        out.append(MatchedClaim(claim, Verdict.UNVERIFIABLE, note=_unresolved_note(claim)))
    out.sort(key=lambda mc: mc.claim.span)
    return out
