"""Claim extraction from agent answers (design section 5).

Tier 1 — citation protocol: numeric claims carrying a structured
citation ``[[r:<receipt_id>#<json_ptr>]]`` are deterministically
matchable and extracted first.

Tier 2 — deterministic candidate scan: remaining numeric spans become
candidate claims when an entity and a metric keyword appear in the same
sentence. Spans that cannot be resolved are reported, not guessed —
they are Tier 3 (LLM) input, which is out of MVP scope.

Every extraction result carries enough structure for the report to
state the tier mix (design: "Tier 1 covered N% of numeric claims").
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from vouch_verifier.tokens import Kind, NumberToken, tokenize


@dataclass(frozen=True)
class Citation:
    """A receipt reference from the citation protocol."""

    receipt_id: str  # may be a prefix of the full id
    json_ptr: str


@dataclass(frozen=True)
class Claim:
    """A numeric assertion extracted from the answer (design section 3.3)."""

    value: float
    span: tuple[int, int]  # character offsets in the answer text
    text: str
    tier: int
    entity: str | None = None
    metric: str | None = None
    unit: str | None = None
    timeframe: str | None = None
    citation: Citation | None = None
    kind: Kind = "point"  # "multiple" and "range" are never judged as points
    resolution: float = 0.0  # unit of the last displayed digit (see tokens)


@dataclass(frozen=True)
class Extraction:
    """All numeric material found in one answer."""

    claims: tuple[Claim, ...]
    unresolved: tuple[Claim, ...]  # numeric spans with no entity/metric resolution

    def tier_share(self, tier: int) -> float:
        """Fraction of all numeric spans extracted at the given tier."""
        total = len(self.claims) + len(self.unresolved)
        if total == 0:
            return 0.0
        return sum(1 for c in self.claims if c.tier == tier) / total


_CITATION_RE = re.compile(r"\[\[r:([A-Za-z0-9_-]+)#((?:/[^/\]\s]*)+|/?)\]\]")
_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n](?:\s|$)")

# Deterministic keyword -> metric mapping, longest match first. This is
# config in spirit; callers can pass their own table built from their
# schemas' metric names.
DEFAULT_METRIC_SYNONYMS: dict[str, str] = {
    "macd histogram": "macd_hist",
    "macd hist": "macd_hist",
    "last price": "last_price",
    "trading at": "last_price",
    "rsi(14)": "rsi_14",
    "closed at": "close_price",
    "closing": "close_price",
    "closed": "close_price",
    "close": "close_price",
    "opened at": "open_price",
    "opened": "open_price",
    "open": "open_price",
    "volume": "volume",
    "change": "change_pct",
    "macd": "macd_hist",
    "rsi": "rsi_14",
    "last": "last_price",
}

# The unit each metric is reported in; metrics not listed have none. A
# claim's unit must agree with its metric's: "1.35%" is never a price
# and a bare "172.04" is never a day change. Like the synonym table,
# callers can derive their own from their schemas.
DEFAULT_METRIC_UNITS: dict[str, str | None] = {
    "close_price": "USD",
    "open_price": "USD",
    "last_price": "USD",
    "change_pct": "pct",
}

# A percentage with no percentage keyword is read as a day change when
# the sentence talks about price or names no metric at all: "AMD is down
# 1.35%", "NVDA closed up 1.92%". Next to a non-price metric ("volume
# rose 12%") it is a change in that metric, which no receipt records.
_PCT_FALLBACK_METRIC = "change_pct"

# "down 1.35%" claims -1.35, not 1.35 — without this, sign flips are
# invisible to the matcher.
_NEGATION_RE = re.compile(r"\b(down|fell|dropped|declined|lost|slid)\b", re.IGNORECASE)


# Scope boundaries inside a sentence (P-032). Independent clauses split
# at semicolons, and a metric keyword never reaches *back* across one to
# an earlier number: "top 5 holdings; it closed at 181.52" does not make
# 5 a close, while "NVDA RSI peaked; it is now 62.3" still reads 62.3 as
# RSI. Phrases split at commas and coordinating words; entity, metric,
# and direction are looked for in the phrase first.
_CLAUSE_SPLIT_RE = re.compile(r";\s*")
_PHRASE_SPLIT_RE = re.compile(
    r",\s+|\s+(?:and|but|while|whereas|versus|vs\.?|compared (?:with|to))\s+", re.IGNORECASE
)

# A sentence that opens with one of these, and names no entity itself,
# continues the previous sentence's subject: "AMD last traded at 172.04.
# It is down 1.35%."
_PRONOUN_START_RE = re.compile(
    r"^\s*(?:it|its|it's|the stock|the shares|shares|the company)\b", re.IGNORECASE
)


_Bounds = tuple[int, int]  # absolute [start, end) offsets in the answer


@dataclass(frozen=True)
class _Scope:
    """Absolute bounds of the sentence, clause, and phrase around a number."""

    sentence: _Bounds
    clause: _Bounds
    phrase: _Bounds


def _sentence_bounds(answer: str, pos: int) -> _Bounds:
    start = 0
    for m in _SENTENCE_SPLIT_RE.finditer(answer, 0, pos):
        start = m.end()
    end = _SENTENCE_SPLIT_RE.search(answer, pos)
    return start, end.start() + 1 if end else len(answer)


def _segment(answer: str, bounds: _Bounds, pos: int, splitter: re.Pattern[str]) -> _Bounds:
    start, end = bounds
    for m in splitter.finditer(answer, start, end):
        if m.end() <= pos:
            start = m.end()
        elif m.start() >= pos:
            end = m.start()
            break
    return start, end


def _scope(answer: str, pos: int) -> _Scope:
    sentence = _sentence_bounds(answer, pos)
    clause = _segment(answer, sentence, pos, _CLAUSE_SPLIT_RE)
    phrase = _segment(answer, clause, pos, _PHRASE_SPLIT_RE)
    return _Scope(sentence, clause, phrase)


def _mentions(
    answer: str, bounds: _Bounds, names: Iterable[str], flags: int = 0
) -> list[tuple[int, str]]:
    """(position, name) for every whole-word mention of a name, in order."""
    start, end = bounds
    found: list[tuple[int, str]] = []
    for name in names:
        rx = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", flags)
        found.extend((m.start(), name) for m in rx.finditer(answer, start, end))
    return sorted(found)


def _within(p: int, bounds: _Bounds) -> bool:
    return bounds[0] <= p < bounds[1]


def _entity(answer: str, pos: int, scope: _Scope, entities: set[str]) -> str | None:
    mentions = _mentions(answer, scope.sentence, entities)
    before = [(p, e) for p, e in mentions if p < pos]
    after = [(p, e) for p, e in mentions if p > pos]
    # Nearest preceding in the phrase, then following in the phrase, then
    # preceding in the clause and sentence, then following in the clause.
    for pool, pick_last in (
        ([m for m in before if _within(m[0], scope.phrase)], True),
        ([m for m in after if _within(m[0], scope.phrase)], False),
        ([m for m in before if _within(m[0], scope.clause)], True),
        (before, True),
        ([m for m in after if _within(m[0], scope.clause)], False),
    ):
        if pool:
            return (pool[-1] if pick_last else pool[0])[1]
    if not mentions and scope.sentence[0] > 0:
        head = answer[scope.sentence[0] : scope.sentence[1]]
        if _PRONOUN_START_RE.match(head):
            previous = _sentence_bounds(answer, max(0, scope.sentence[0] - 2))
            earlier = _mentions(answer, previous, entities)
            if earlier:
                return earlier[0][1]  # the previous sentence's subject
    return None


def _keyword_hits(answer: str, pos: int, scope: _Scope, table: dict[str, str]) -> list[str]:
    """Metrics named around the number, in the order they should be tried:
    the phrase by distance, then the rest of the clause before and after
    the number (nearest first), then earlier clauses of the sentence."""
    keywords = sorted(table, key=len, reverse=True)
    hits = [(p, table[kw]) for p, kw in _mentions(answer, scope.sentence, keywords, re.IGNORECASE)]
    phrase: list[tuple[int, str]] = []
    before: list[tuple[int, str]] = []
    after: list[tuple[int, str]] = []
    earlier: list[tuple[int, str]] = []
    for p, metric in hits:
        if _within(p, scope.phrase):
            phrase.append((abs(p - pos), metric))
        elif _within(p, scope.clause):
            (before if p < pos else after).append((abs(p - pos), metric))
        elif p < scope.clause[0]:
            earlier.append((pos - p, metric))
    return [m for bucket in (phrase, before, after, earlier) for _, m in sorted(bucket)]


def _unit_compatible(claim_unit: str | None, metric_unit: str | None) -> bool:
    if claim_unit is None:
        return metric_unit != "pct"
    return claim_unit == metric_unit


def _pick_metric(hits: list[str], unit: str | None, units: dict[str, str | None]) -> str | None:
    metric = next((h for h in hits if _unit_compatible(unit, units.get(h))), None)
    if metric is None and unit == "pct" and (not hits or units.get(hits[0]) == "USD"):
        metric = _PCT_FALLBACK_METRIC
    return metric


def _resolve(
    answer: str,
    m: NumberToken,
    known_entities: set[str] | frozenset[str],
    synonyms: dict[str, str],
    units: dict[str, str | None],
) -> Claim:
    """Attach entity and metric to one Tier 2 numeric token."""
    value, unit = m.value, m.unit
    scope = _scope(answer, m.start)
    # A direction word only signs the number it governs: in "Unlike AMD,
    # which fell 1.35%, NVDA rose 1.92%" the "fell" stays in its phrase.
    if (
        unit == "pct"
        and value > 0
        and not m.text.startswith(("+", "-"))
        and _NEGATION_RE.search(answer, scope.phrase[0], m.start)
    ):
        value = -value
    entity = _entity(answer, m.start, scope, set(known_entities))
    metric = _pick_metric(_keyword_hits(answer, m.start, scope, synonyms), unit, units)
    return Claim(
        value=value,
        span=(m.start, m.end),
        text=m.text,
        tier=2,
        entity=entity,
        metric=metric,
        unit=unit,
        kind=m.kind,
        resolution=m.resolution,
    )


def extract_claims(
    answer: str,
    known_entities: set[str] | frozenset[str] = frozenset(),
    metric_synonyms: dict[str, str] | None = None,
    metric_units: dict[str, str | None] | None = None,
) -> Extraction:
    """Extract numeric claims from an answer, Tier 1 then Tier 2."""
    synonyms = DEFAULT_METRIC_SYNONYMS if metric_synonyms is None else metric_synonyms
    units = DEFAULT_METRIC_UNITS if metric_units is None else metric_units

    citations = list(_CITATION_RE.finditer(answer))
    numbers = tokenize(answer, exclude=[c.span() for c in citations])
    consumed: set[int] = set()
    claims: list[Claim] = []
    unresolved: list[Claim] = []

    # Tier 1: each citation binds to the nearest preceding number in the
    # same sentence.
    for cit in citations:
        sent_start, _ = _sentence_bounds(answer, cit.start())
        candidates = [
            i
            for i, m in enumerate(numbers)
            if i not in consumed
            and m.kind == "point"
            and sent_start <= m.start
            and m.end <= cit.start()
        ]
        if not candidates:
            continue  # dangling citation; the matcher flags it via coverage
        i = candidates[-1]
        m = numbers[i]
        consumed.add(i)
        claims.append(
            Claim(
                value=m.value,
                span=(m.start, m.end),
                text=m.text,
                tier=1,
                unit=m.unit,
                resolution=m.resolution,
                citation=Citation(receipt_id=cit.group(1), json_ptr=cit.group(2) or "/"),
            )
        )

    # Tier 2: remaining numbers need an entity and a metric in-sentence.
    for i, m in enumerate(numbers):
        if i in consumed:
            continue
        claim = _resolve(answer, m, known_entities, synonyms, units)
        if claim.kind != "point" or claim.entity is None or claim.metric is None:
            unresolved.append(claim)
        else:
            claims.append(claim)

    claims.sort(key=lambda c: c.span)
    return Extraction(claims=tuple(claims), unresolved=tuple(unresolved))
