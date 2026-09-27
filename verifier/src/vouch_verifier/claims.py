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


def _sentence_bounds(answer: str, pos: int) -> tuple[int, int]:
    start = 0
    for m in _SENTENCE_SPLIT_RE.finditer(answer, 0, pos):
        start = m.end()
    end = _SENTENCE_SPLIT_RE.search(answer, pos)
    return start, end.start() + 1 if end else len(answer)


def _keyword_hits(sentence: str, offset: int, num_start: int, table: dict[str, str]) -> list[str]:
    """Metrics named in the sentence, nearest keyword to the number first."""
    hits: list[tuple[int, str]] = []
    lowered = sentence.lower()
    for kw in sorted(table, key=len, reverse=True):
        for m in re.finditer(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", lowered):
            hits.append((abs((offset + m.start()) - num_start), table[kw]))
    return [metric for _, metric in sorted(hits, key=lambda h: h[0])]


def _unit_compatible(claim_unit: str | None, metric_unit: str | None) -> bool:
    if claim_unit is None:
        return metric_unit != "pct"
    return claim_unit == metric_unit


def _pick_metric(hits: list[str], unit: str | None, units: dict[str, str | None]) -> str | None:
    metric = next((h for h in hits if _unit_compatible(unit, units.get(h))), None)
    if metric is None and unit == "pct" and (not hits or units.get(hits[0]) == "USD"):
        metric = _PCT_FALLBACK_METRIC
    return metric


def _nearest_entity(sentence: str, offset: int, num_start: int, entities: set[str]) -> str | None:
    best: tuple[int, str] | None = None
    for ent in entities:
        for m in re.finditer(r"(?<!\w)" + re.escape(ent) + r"(?!\w)", sentence):
            distance = abs((offset + m.start()) - num_start)
            if best is None or distance < best[0]:
                best = (distance, ent)
    return best[1] if best else None


def _resolve(
    answer: str,
    m: NumberToken,
    known_entities: set[str] | frozenset[str],
    synonyms: dict[str, str],
    units: dict[str, str | None],
) -> Claim:
    """Attach entity and metric to one Tier 2 numeric token."""
    value, unit = m.value, m.unit
    sent_start, sent_end = _sentence_bounds(answer, m.start)
    sentence = answer[sent_start:sent_end]
    if (
        unit == "pct"
        and value > 0
        and not m.text.startswith(("+", "-"))
        and _NEGATION_RE.search(sentence[: m.start - sent_start])
    ):
        value = -value
    entity = _nearest_entity(sentence, sent_start, m.start, set(known_entities))
    metric = _pick_metric(_keyword_hits(sentence, sent_start, m.start, synonyms), unit, units)
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
