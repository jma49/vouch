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
_NUMBER_RE = re.compile(r"(?<![\w.\-+])[-+]?\d[\d,]*(?:\.\d+)?(?:\s?%)?")
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

# A percentage with no explicit metric keyword is read as a day change:
# "AMD is down 1.35%" claims change_pct.
_PCT_FALLBACK_METRIC = "change_pct"

# "down 1.35%" claims -1.35, not 1.35 — without this, sign flips are
# invisible to the matcher.
_NEGATION_RE = re.compile(r"\b(down|fell|dropped|declined|lost|slid)\b", re.IGNORECASE)


def _parse_number(text: str) -> tuple[float, str | None]:
    """Parse one matched numeric span into (value, unit)."""
    unit = None
    cleaned = text.strip()
    if cleaned.endswith("%"):
        unit = "pct"
        cleaned = cleaned[:-1].strip()
    return float(cleaned.replace(",", "")), unit


def _sentence_bounds(answer: str, pos: int) -> tuple[int, int]:
    start = 0
    for m in _SENTENCE_SPLIT_RE.finditer(answer, 0, pos):
        start = m.end()
    m = _SENTENCE_SPLIT_RE.search(answer, pos)
    return start, m.start() + 1 if m else len(answer)


def _nearest_keyword(
    sentence: str, offset: int, num_start: int, table: dict[str, str]
) -> str | None:
    """Return the metric whose keyword sits closest to the number."""
    best: tuple[int, str] | None = None
    lowered = sentence.lower()
    for kw in sorted(table, key=len, reverse=True):
        for m in re.finditer(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", lowered):
            distance = abs((offset + m.start()) - num_start)
            if best is None or distance < best[0]:
                best = (distance, table[kw])
    return best[1] if best else None


def _nearest_entity(sentence: str, offset: int, num_start: int, entities: set[str]) -> str | None:
    best: tuple[int, str] | None = None
    for ent in entities:
        for m in re.finditer(r"(?<!\w)" + re.escape(ent) + r"(?!\w)", sentence):
            distance = abs((offset + m.start()) - num_start)
            if best is None or distance < best[0]:
                best = (distance, ent)
    return best[1] if best else None


def extract_claims(
    answer: str,
    known_entities: set[str] | frozenset[str] = frozenset(),
    metric_synonyms: dict[str, str] | None = None,
) -> Extraction:
    """Extract numeric claims from an answer, Tier 1 then Tier 2."""
    synonyms = DEFAULT_METRIC_SYNONYMS if metric_synonyms is None else metric_synonyms

    def is_parameter(m: re.Match[str]) -> bool:
        # "RSI(14)": a number in function-call-style parens names the
        # metric's parameter, it is not a claim.
        return (
            m.start() >= 2
            and answer[m.start() - 1] == "("
            and answer[m.start() - 2].isalnum()
            and answer[m.end() : m.end() + 1] == ")"
        )

    numbers = [
        m for m in _NUMBER_RE.finditer(answer)
        # Numbers inside a citation marker are not claims.
        if not any(c.start() <= m.start() < c.end() for c in _CITATION_RE.finditer(answer))
        and not is_parameter(m)
    ]
    consumed: set[int] = set()
    claims: list[Claim] = []
    unresolved: list[Claim] = []

    # Tier 1: each citation binds to the nearest preceding number in the
    # same sentence.
    for cit in _CITATION_RE.finditer(answer):
        sent_start, _ = _sentence_bounds(answer, cit.start())
        candidates = [
            i for i, m in enumerate(numbers)
            if i not in consumed and sent_start <= m.start() and m.end() <= cit.start()
        ]
        if not candidates:
            continue  # dangling citation; the matcher flags it via coverage
        i = candidates[-1]
        m = numbers[i]
        consumed.add(i)
        value, unit = _parse_number(m.group())
        claims.append(
            Claim(
                value=value,
                span=(m.start(), m.end()),
                text=m.group(),
                tier=1,
                unit=unit,
                citation=Citation(receipt_id=cit.group(1), json_ptr=cit.group(2) or "/"),
            )
        )

    # Tier 2: remaining numbers need an entity and a metric in-sentence.
    for i, m in enumerate(numbers):
        if i in consumed:
            continue
        value, unit = _parse_number(m.group())
        sent_start, sent_end = _sentence_bounds(answer, m.start())
        sentence = answer[sent_start:sent_end]
        if (
            unit == "pct"
            and value > 0
            and not m.group().lstrip().startswith(("+", "-"))
            and _NEGATION_RE.search(sentence[: m.start() - sent_start])
        ):
            value = -value
        entity = _nearest_entity(sentence, sent_start, m.start(), set(known_entities))
        metric = _nearest_keyword(sentence, sent_start, m.start(), synonyms)
        if metric is None and unit == "pct":
            metric = _PCT_FALLBACK_METRIC
        claim = Claim(
            value=value,
            span=(m.start(), m.end()),
            text=m.group(),
            tier=2,
            entity=entity,
            metric=metric,
            unit=unit,
        )
        if entity is None or metric is None:
            unresolved.append(claim)
        else:
            claims.append(claim)

    claims.sort(key=lambda c: c.span)
    return Extraction(claims=tuple(claims), unresolved=tuple(unresolved))
