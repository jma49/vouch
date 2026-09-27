"""Claim -> Fact matching and verdict assignment (design sections 3.3, 6).

Verdicts: SUPPORTED, CONTRADICTED, UNSUPPORTED, STALE (a value true
only outside the claim's time window, including data from after a
backtest's as-of moment, design section 8.4), DERIVED (a multi-day
change or a high/low recomputed from the receipted series, design
section 6.2), and UNVERIFIABLE for spans extraction could not resolve.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import yaml

from vouch_verifier.claims import Claim, Derivation, Extraction, negated_by_parentheses
from vouch_verifier.index import build_index, facts_for
from vouch_verifier.lookahead import after
from vouch_verifier.receipts import Fact, Receipt
from vouch_verifier.verdict import Tolerance, Verdict, compare
from vouch_verifier.vocabulary import FINANCE, Vocabulary

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


MIN_CITE_PREFIX = 8


def _prose_disagrees(claim: Claim, fact: Fact) -> str | None:
    """Why the prose around a cited number is about something other than
    the cited fact, if it is: another entity, or another date (#95). A
    citation says which fact backs the number; it does not make "AMD's
    RSI" true because NVDA's matches."""
    if claim.entity is not None and fact.entity and claim.entity != fact.entity:
        return f"the claim is about {claim.entity}; the cited fact is {fact.entity}'s"
    if claim.as_of is not None and not _on_date(fact, claim.as_of):
        return f"the claim is dated {claim.as_of}; the cited fact is as of {fact.as_of}"
    return None


def _match_cited(
    claim: Claim,
    receipts: list[Receipt],
    tolerances: dict[str, Tolerance],
    as_of: datetime | None = None,
    vocab: Vocabulary = FINANCE,
) -> MatchedClaim:
    citation = claim.citation
    assert citation is not None
    cited = citation.receipt_id
    # An exact id wins; a prefix is a convenience for long ids (issue
    # #17), but a prefix shorter than 8 characters is too easy to hit by
    # accident to count as citing anything (#95).
    matching = [r for r in receipts if r.receipt_id == cited] or [
        r for r in receipts if len(cited) >= MIN_CITE_PREFIX and r.receipt_id.startswith(cited)
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
            if (why := _prose_disagrees(claim, fact)) is not None:
                return MatchedClaim(
                    claim, Verdict.UNSUPPORTED, fact=fact, receipt_id=receipt.receipt_id, note=why
                )
            if negated_by_parentheses(claim.parenthesized, claim.value, fact.metric, vocab):
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
            if as_of is not None and verdict is Verdict.SUPPORTED and after(fact, as_of):
                return MatchedClaim(
                    claim,
                    Verdict.STALE,
                    fact=fact,
                    receipt_id=receipt.receipt_id,
                    note=f"look-ahead: cited data is as of {fact.as_of}, after {as_of.isoformat()}",
                )
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
    claim: Claim,
    conn: sqlite3.Connection,
    tol: dict[str, Tolerance],
    as_of: datetime | None = None,
) -> MatchedClaim:
    """Judge a Tier 2 claim against the receipted facts for its time window.

    A stated date selects that day's facts; with no date the window is
    the latest receipted day. A value that matches only outside the
    window is STALE rather than SUPPORTED: it was true, but not for the
    time the claim is about (design section 6.1). With a backtest's
    as-of moment, data from after it is outside every window: the agent
    should not have had it (design section 8.4).
    """
    assert claim.entity is not None and claim.metric is not None
    key = ", ".join(x for x in (claim.entity, claim.metric, claim.timeframe) if x)
    candidates = facts_for(conn, claim.entity, claim.metric, claim.timeframe)
    if not candidates:
        return MatchedClaim(claim, Verdict.UNSUPPORTED, note=f"no receipt covers ({key})")
    future = [rf for rf in candidates if as_of is not None and after(rf[1], as_of)]
    candidates = [rf for rf in candidates if rf not in future]
    if not candidates and claim.as_of is None:
        return _lookahead_only(claim, future, tol, key, as_of)

    if claim.as_of is not None:
        window = [rf for rf in candidates if _on_date(rf[1], claim.as_of)]
        if not window:
            if dated := [rf for rf in future if _on_date(rf[1], claim.as_of)]:
                return _lookahead_only(claim, dated, tol, key, as_of)
            return MatchedClaim(
                claim, Verdict.UNSUPPORTED, note=f"no receipt covers ({key}) on {claim.as_of}"
            )
        outside: list[tuple[str, Fact]] = list(future)
    else:
        latest = max((_day(f.as_of) or "" for _, f in candidates), default="")
        window = [rf for rf in candidates if (_day(rf[1].as_of) or "") == latest]
        outside = [rf for rf in candidates if rf not in window] + future

    # A claim that names no timeframe matches facts of any; when the window
    # holds several timeframes that disagree (RSI 62.3 daily, 48.0
    # hourly), which one the claim means is a guess (#95).
    if claim.timeframe is None:
        by_timeframe: dict[str, set[Verdict]] = {}
        for _, fact in window:
            if fact.timeframe:
                by_timeframe.setdefault(fact.timeframe, set()).add(_judge(claim, fact, tol))
        if len(by_timeframe) > 1 and len({frozenset(v) for v in by_timeframe.values()}) > 1:
            return MatchedClaim(
                claim,
                Verdict.UNVERIFIABLE,
                note=f"timeframe ambiguous: receipted {', '.join(sorted(by_timeframe))} disagree",
            )
    for receipt_id, fact in window:
        if _judge(claim, fact, tol) is Verdict.SUPPORTED:
            return MatchedClaim(claim, Verdict.SUPPORTED, fact=fact, receipt_id=receipt_id)
    for receipt_id, fact in outside:
        if _judge(claim, fact, tol) is Verdict.SUPPORTED:
            _, latest_fact = _closest(claim, window)
            ahead = as_of is not None and after(fact, as_of)
            return MatchedClaim(
                claim,
                Verdict.STALE,
                fact=fact,
                receipt_id=receipt_id,
                note=(
                    f"look-ahead: matches data as of {fact.as_of}, after the as-of "
                    f"{as_of.isoformat()}; latest available ({_day(latest_fact.as_of)}) is "
                    f"{latest_fact.value}"
                    if ahead and as_of is not None
                    else f"matches the value as of {_day(fact.as_of)}; "
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


def _series_in(
    receipt: Receipt, entity: str, metric: str, as_of: datetime | None
) -> dict[str, Fact] | None:
    """One receipt's daily series of a metric: day -> fact, from facts
    that carry their own date, without data after a backtest's as-of
    moment. None when the receipt gives two values for one day."""
    series: dict[str, Fact] = {}
    for fact in receipt.facts:
        if fact.entity != entity or fact.metric != metric or not fact.as_of:
            continue
        if as_of is not None and after(fact, as_of):
            continue
        day = _day(fact.as_of)
        assert day is not None
        if day in series and series[day].value != fact.value:
            return None
        series[day] = fact
    return series


def _recompute(
    d: Derivation, series: dict[str, Fact], end: str
) -> tuple[float, Fact, str, list[str]] | None:
    """The derived value over one series ending on `end`, with the fact
    it ends on (or, for a high/low, the extreme), a description of the
    points used, and their days; None when the series lacks a point."""
    days = sorted(series)
    if end not in series:
        return None
    last = days.index(end)
    if d.op == "pct_change":
        if d.start is not None:
            matching = [x for x in days[:last] if _same_day(x, d.start)]
            if not matching:
                return None
            first = matching[-1]
        else:
            assert d.lookback is not None
            if last < d.lookback:
                return None
            first = days[last - d.lookback]
        base, now = series[first].value, series[end].value
        if base == 0:
            return None
        basis = f"{first} ({base:g}) to {end} ({now:g})"
        return (now / base - 1) * 100, series[end], basis, [first, end]
    assert d.lookback is not None
    if last + 1 < d.lookback:
        return None
    used = days[last + 1 - d.lookback : last + 1]
    fact = (max if d.op == "max" else min)((series[x] for x in used), key=lambda f: f.value)
    return fact.value, fact, f"{used[0]} to {end}, {_day(fact.as_of)}", used


def _same_day(day: str, date: str) -> bool:
    # "--MM-DD" (no year in the text) is that day in any year.
    return day[4:] == date[1:] if date.startswith("--") else day == date


def _match_derived(
    claim: Claim,
    receipts: list[Receipt],
    tol: dict[str, Tolerance],
    as_of: datetime | None,
) -> MatchedClaim:
    """Recompute a derived claim from a receipted series (design section
    6.2) and judge the stated value against the result: DERIVED when
    within tolerance, CONTRADICTED when not, UNSUPPORTED when no receipt
    holds the points it needs.

    The points come from one receipt's series, never stitched across
    receipts: one OHLCV call returns consecutive sessions, while days
    gathered from several calls can have gaps, so "the past 2 sessions"
    could silently span weeks (#94). Facts without their own date are not
    part of any series.
    """
    d = claim.derivation
    assert d is not None and claim.entity is not None
    what = f"{claim.entity} {d.describe()}"
    per_receipt = [(r, _series_in(r, claim.entity, d.metric, as_of)) for r in receipts]
    if any(s is None for _, s in per_receipt):
        return MatchedClaim(
            claim, Verdict.UNSUPPORTED, note=f"a receipt gives two values for one day of {what}"
        )
    available = [day for _, s in per_receipt if s for day in s]
    if not available:
        return MatchedClaim(claim, Verdict.UNSUPPORTED, note=f"no receipted series for {what}")
    if d.end is None:
        end = max(available)
    else:
        matching = sorted(day for day in set(available) if _same_day(day, d.end))
        if not matching:
            return MatchedClaim(
                claim, Verdict.UNSUPPORTED, note=f"no receipted {d.metric} on {d.end} for {what}"
            )
        end = matching[-1]
    results = []
    for receipt, series in per_receipt:
        if series and (got := _recompute(d, series, end)) is not None:
            results.append((receipt.receipt_id, *got))
    if not results:
        return MatchedClaim(
            claim,
            Verdict.UNSUPPORTED,
            note=f"no single receipt holds the points to recompute {what}",
        )
    # Any receipt saying something else about a day the computation uses
    # makes the result unusable; the verifier does not pick a source.
    used = {day for *_, days in results for day in days}
    values: dict[str, set[float]] = {}
    for _, series in per_receipt:
        for day, fact in (series or {}).items():
            if day in used:
                values.setdefault(day, set()).add(fact.value)
    if any(len(v) > 1 for v in values.values()):
        return MatchedClaim(
            claim, Verdict.UNSUPPORTED, note=f"receipts disagree on the points of {what}"
        )
    receipt_id, computed, fact, basis, _ = results[0]
    tolerance = (
        tol.get("percentage", Tolerance()) if d.op == "pct_change" else _tolerance_for(fact, tol)
    )
    note = f"recomputed {what} = {computed:.4g} from {basis}"
    if compare(claim.value, computed, tolerance, claim.resolution) is Verdict.SUPPORTED:
        return MatchedClaim(claim, Verdict.DERIVED, receipt_id=receipt_id, note=note)
    return MatchedClaim(claim, Verdict.CONTRADICTED, receipt_id=receipt_id, note=note)


def _lookahead_only(
    claim: Claim,
    future: list[tuple[str, Fact]],
    tol: dict[str, Tolerance],
    key: str,
    as_of: datetime | None,
) -> MatchedClaim:
    """A claim whose only candidate facts are from after the as-of
    moment: a match is STALE (look-ahead); anything else is unsupported
    by data the agent could have had."""
    assert as_of is not None
    for receipt_id, fact in future:
        if _judge(claim, fact, tol) is Verdict.SUPPORTED:
            return MatchedClaim(
                claim,
                Verdict.STALE,
                fact=fact,
                receipt_id=receipt_id,
                note=f"look-ahead: matches only data as of {fact.as_of}, after {as_of.isoformat()}",
            )
    return MatchedClaim(
        claim, Verdict.UNSUPPORTED, note=f"no receipt covers ({key}) as of {as_of.isoformat()}"
    )


def match_claims(
    extraction: Extraction,
    receipts: list[Receipt],
    tolerances: dict[str, Tolerance] | None = None,
    as_of: datetime | None = None,
    vocabulary: Vocabulary = FINANCE,
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
                out.append(_match_cited(claim, receipts, tol, as_of, vocabulary))
                continue
            if claim.derivation is not None:
                out.append(_match_derived(claim, receipts, tol, as_of))
                continue

            out.append(_match_uncited(claim, conn, tol, as_of))

    for claim in extraction.unresolved:
        out.append(MatchedClaim(claim, Verdict.UNVERIFIABLE, note=_unresolved_note(claim)))
    out.sort(key=lambda mc: mc.claim.span)
    return out
