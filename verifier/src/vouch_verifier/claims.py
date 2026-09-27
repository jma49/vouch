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
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from vouch_verifier.tokens import MINUTE_TIMEFRAME, Kind, NumberToken, find_dates, tokenize
from vouch_verifier.vocabulary import FINANCE, Vocabulary


@dataclass(frozen=True)
class Citation:
    """A receipt reference from the citation protocol."""

    receipt_id: str  # may be a prefix of the full id
    json_ptr: str


@dataclass(frozen=True)
class Derivation:
    """A claim about a quantity computed from a receipted series rather
    than read off one fact (design section 6.2): a percentage change over
    an explicit period, or a high or low over the last N sessions."""

    op: Literal["pct_change", "max", "min"]
    metric: str  # the series it is computed over
    start: str | None = None  # pct_change from this date ("YYYY-MM-DD" or "--MM-DD")
    end: str | None = None  # the last day; None means the latest receipted day
    lookback: int | None = None  # sessions back from the last day

    def describe(self) -> str:
        to = f" to {self.end}" if self.end else ""
        if self.op == "pct_change":
            span = f"since {self.start}" if self.start else f"over {self.lookback} sessions"
            return f"{self.metric} change {span}{to}"
        return (
            f"{self.lookback}-session {'high' if self.op == 'max' else 'low'} of {self.metric}{to}"
        )


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
    as_of: str | None = None  # "YYYY-MM-DD", or "--MM-DD" when the text gives no year
    kind: Kind = "point"  # "multiple" and "range" are never judged as points
    # "(1.35%)" with no explicit sign: negative if the metric is signed.
    # Tier 2 applies this at extraction; Tier 1 only learns the metric
    # from the cited fact, so the matcher applies it there.
    parenthesized: bool = False
    resolution: float = 0.0  # unit of the last displayed digit (see tokens)
    derivation: Derivation | None = None  # judged by recomputation, not lookup


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
# Every line break ends a sentence: agents write lists and tables, and
# a row must not borrow a keyword from the row above (issue #15).
_SENTENCE_SPLIT_RE = re.compile(r"[.!?](?:\s|$)|\n")

# A markdown table: a header row, a separator row, then body rows.
_TABLE_ROW_RE = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
_TABLE_SEPARATOR_RE = re.compile(r"^[ \t]*\|(?:[ \t]*:?-{3,}:?[ \t]*\|)+[ \t]*$")

# The finance vocabulary is the default; see vouch_verifier.vocabulary.
# These names stay for callers that read the default tables.
DEFAULT_METRIC_SYNONYMS: dict[str, str] = dict(FINANCE.synonyms)
DEFAULT_METRIC_UNITS: dict[str, str | None] = dict(FINANCE.units)
DEFAULT_SIGNED_METRICS: frozenset[str] = FINANCE.signed

# "fell 1.35% to 172.04", "rose from 170 to 172.04": a bare number after
# a move and "to" is the resulting price (issue #39). Only used when no
# price keyword resolves it first, so "closed up 1.92% at 181.52" still
# reads as the close.
_MOVE_TARGET_RE = re.compile(r"(?:%|\d)\s+(?:to|at)\s+\$?$", re.IGNORECASE)

# A percentage with no percentage keyword is read as the vocabulary's
# pct_fallback (a day change, in finance) when the sentence talks about
# a USD metric or names no metric at all: "AMD is down 1.35%", "NVDA
# closed up 1.92%". Next to another metric ("volume rose 12%") it is a
# change in that metric, which no receipt records.

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

# Chart timeframe named in the clause: "on the hourly chart", "1d RSI".
_TIMEFRAME_RE = re.compile(
    rf"\b(?:(?P<word>hourly|daily|weekly)|(?P<n>\d+)(?P<u>[hdw]))\b|(?P<min>{MINUTE_TIMEFRAME})",
    re.I,
)
_TIMEFRAME_WORDS = {"hourly": "1h", "daily": "1d", "weekly": "1w"}

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


@lru_cache(maxsize=16)
def _sentence_breaks(answer: str) -> tuple[list[int], list[int]]:
    """Every sentence break in the answer, found once: (break starts,
    the offsets just past each break). Looking bounds up per number used
    to rescan from the start, which made extraction quadratic (#19)."""
    matches = list(_SENTENCE_SPLIT_RE.finditer(answer))
    return [m.start() for m in matches], [m.end() for m in matches]


def _sentence_bounds(answer: str, pos: int) -> _Bounds:
    starts, ends = _sentence_breaks(answer)
    # The last break that ends at or before pos opens the sentence ...
    i = bisect_right(ends, pos) - 1
    start = ends[i] if i >= 0 else 0
    # ... and the first break starting at or after pos closes it.
    j = bisect_left(starts, pos)
    return start, starts[j] + 1 if j < len(starts) else len(answer)


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
    # A sentence opening with a pronoun is about the previous sentence's
    # subject until it names someone before the number: in "AMD has been
    # weak. It closed at 181.52 while NVDA rallied." the close is AMD's,
    # not NVDA's (#95). With no subject to inherit, it stays unresolved.
    head = answer[scope.sentence[0] : scope.sentence[1]]
    if not before and _PRONOUN_START_RE.match(head):
        if scope.sentence[0] == 0:
            return None
        previous = _sentence_bounds(answer, max(0, scope.sentence[0] - 2))
        earlier = _mentions(answer, previous, entities)
        return earlier[0][1] if earlier else None
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
    return None


def _date(answer: str, pos: int, scope: _Scope) -> str | None:
    """The date a claim is about: the nearest one in its own phrase, else
    the nearest one before it in its clause ("On July 23, NVDA closed at
    176.10"). A date later in the clause belongs to another phrase: in
    "NVDA closed at 181.52, up from 176.10 on July 23" it dates the 176.10
    only (#95). Dates never cross a semicolon: in "NVDA reports Q2
    earnings on August 27; it closed at 181.52" the date is the
    earnings'.
    """
    dates = find_dates(answer, *scope.phrase)
    if dates:
        return min(dates, key=lambda d: abs(d[0] - pos))[1]
    # Only a phrase that is nothing but a time ("On July 23,") dates what
    # follows; "down 5% since July 20" makes its own claim and keeps its
    # date (#95).
    before = [
        d
        for d in find_dates(answer, *scope.clause)
        if d[0] < pos and _only_a_time(answer, d, scope)
    ]
    return before[-1][1] if before else None


def _only_a_time(answer: str, date: tuple[int, str], scope: _Scope) -> bool:
    """Whether the phrase holding a date makes no numeric claim of its
    own (tokenize masks the date itself)."""
    lo, hi = _segment(answer, scope.clause, date[0], _PHRASE_SPLIT_RE)
    return not tokenize(answer[lo:hi])


def _timeframe(answer: str, scope: _Scope) -> str | None:
    m = _TIMEFRAME_RE.search(answer, *scope.clause)
    if m is None:
        return None
    if m["word"]:
        return _TIMEFRAME_WORDS[m["word"].lower()]
    if m["min"]:
        digits = re.match(r"\d+", m["min"])
        assert digits is not None  # MINUTE_TIMEFRAME starts with digits
        return f"{int(digits[0])}m"
    return f"{m['n']}{m['u'].lower()}"


@dataclass(frozen=True)
class _Cell:
    """What a table cell's position says about the number in it."""

    entity: str | None
    metric: str | None
    as_of: str | None


def _line_bounds(answer: str, pos: int) -> _Bounds:
    start = answer.rfind("\n", 0, pos) + 1
    end = answer.find("\n", pos)
    return start, len(answer) if end < 0 else end


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _table_cell(
    answer: str,
    m: NumberToken,
    entities: set[str],
    vocab: Vocabulary,
) -> _Cell | None:
    """Resolve a number inside a markdown table body row, or None if it
    is not in one. Metric comes from the column header, entity from the
    row (else the column header), and a date in the row dates the value.
    A header naming no metric leaves the cell unresolved: the prose
    fallback is what used to read a price under the RSI column."""
    start, end = _line_bounds(answer, m.start)
    line = answer[start:end]
    if not _TABLE_ROW_RE.match(line) or _TABLE_SEPARATOR_RE.match(line):
        return None
    # Walk up through the body rows to the separator; the header is the
    # row above it. A number in the header row itself is not a cell.
    header: list[str] | None = None
    cursor = start
    while cursor > 0:
        above_start, above_end = _line_bounds(answer, cursor - 1)
        above = answer[above_start:above_end]
        if _TABLE_SEPARATOR_RE.match(above):
            if above_start > 0:
                head_start, head_end = _line_bounds(answer, above_start - 1)
                if _TABLE_ROW_RE.match(answer[head_start:head_end]):
                    header = _cells(answer[head_start:head_end])
            break
        if not _TABLE_ROW_RE.match(above):
            break
        cursor = above_start
    if header is None:
        return None
    column = line[: m.start - start].strip().lstrip("|").count("|")
    heading = header[column] if column < len(header) else ""

    hits = [
        vocab.synonyms[kw]
        for kw in sorted(vocab.synonyms, key=len, reverse=True)
        if re.search(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", heading, re.IGNORECASE)
    ]
    metric = _pick_metric(hits, m.unit, vocab) if hits else None

    def entity_in(text: str) -> str | None:
        found = _mentions(text, (0, len(text)), entities)
        return found[0][1] if found else None

    row = [c for i, c in enumerate(_cells(line)) if i != column]
    entity = next((e for c in row if (e := entity_in(c))), None) or entity_in(heading)
    dates = find_dates(line)
    return _Cell(entity, metric, dates[0][1] if dates else None)


def _keyword_hits(answer: str, pos: int, scope: _Scope, table: Mapping[str, str]) -> list[str]:
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


def _pick_metric(hits: list[str], unit: str | None, vocab: Vocabulary) -> str | None:
    metric = next((h for h in hits if _unit_compatible(unit, vocab.units.get(h))), None)
    if metric is None and unit == "pct" and (not hits or vocab.units.get(hits[0]) == "USD"):
        metric = vocab.pct_fallback
    return metric


def negated_by_parentheses(
    parenthesized: bool, value: float, metric: str | None, vocab: Vocabulary = FINANCE
) -> bool:
    """Whether "(1.35%)" is an accounting negative: only for a signed metric."""
    return parenthesized and value > 0 and metric in vocab.signed


def _negated_by_direction(answer: str, m: NumberToken, scope: _Scope) -> bool:
    # A direction word only signs the number it governs: in "Unlike AMD,
    # which fell 1.35%, NVDA rose 1.92%" the "fell" stays in its phrase.
    return (
        m.unit == "pct"
        and m.value > 0
        and not m.signed
        and _NEGATION_RE.search(answer, scope.phrase[0], m.start) is not None
    )


# Periods a derived claim can be computed over. "Sessions" and "trading
# days" are unambiguous; plain "days" could be calendar days, which move
# the start point, so it is never guessed (_AMBIGUOUS_PERIOD).
_LOOKBACK_RE = re.compile(
    r"\b(?:over|in|during|across|for)\s+the\s+(?:past|last|previous|prior)\s+(\d+)\s+"
    r"(sessions?|trading\s+days?|days?)\b",
    re.IGNORECASE,
)
_NDAY_EXTREMUM_RE = re.compile(r"\b(\d+)[- ](?:day|session)\s+(high|low)\b", re.IGNORECASE)
_EXTREMUM_RE = re.compile(r"\b(highest|lowest|peak|trough|high|low)\b", re.IGNORECASE)
_SINCE_RE = re.compile(r"\b(since|from)\b", re.IGNORECASE)
_AMBIGUOUS_PERIOD = "ambiguous period"


def _derivation(
    answer: str, m: NumberToken, scope: _Scope, vocab: Vocabulary
) -> Derivation | str | None:
    """The computation a claim states, if any: a Derivation, the string
    _AMBIGUOUS_PERIOD when it names a period or series that cannot be
    pinned down, or None for an ordinary point claim.

    Every cue must be in the number's own phrase (#94): in "NVDA rose
    3.08% on the day, its biggest gain since July 20" the "since" belongs
    to another phrase and the 3.08% is a day change.
    """
    if vocab.series is None:
        return None
    lo, hi = scope.phrase
    lookback = _LOOKBACK_RE.search(answer, lo, hi)
    since = _SINCE_RE.search(answer, lo, hi) if m.unit == "pct" else None
    nday = _NDAY_EXTREMUM_RE.search(answer, lo, m.start) if m.unit != "pct" else None
    extremum = _EXTREMUM_RE.search(answer, lo, m.start) if m.unit != "pct" else None
    cued = lookback or nday or (extremum and lookback)
    start: tuple[int, str] | None = None
    if since and not cued:
        dates = find_dates(answer, since.end(), hi)
        # "since July 17", "from its July 20 close": the date follows closely.
        if dates and len(answer[since.end() : dates[0][0]].split()) <= 2:
            start = dates[0]
    if not (cued or start):
        return None
    if m.unit not in (None, "USD", "pct"):
        return None

    # The series: the metric the phrase names, if it is a USD metric, else
    # the vocabulary's. A phrase naming another metric ("RSI rose 6.8%
    # since July 20", "volume hit a 5-day high") is about that metric,
    # whose change nobody receipted: unresolved, never recomputed from
    # the close (#94).
    keywords = sorted(vocab.synonyms, key=len, reverse=True)
    period = lookback.span() if lookback else (0, 0)  # "over the last 3 sessions" names no metric
    named = [
        vocab.synonyms[kw]
        for pos, kw in _mentions(answer, scope.phrase, keywords, re.IGNORECASE)
        if not period[0] <= pos < period[1]
    ]
    if any(vocab.units.get(n) != "USD" and n != vocab.series for n in named):
        return _AMBIGUOUS_PERIOD
    series = next((n for n in named if vocab.units.get(n) == "USD"), vocab.series)

    # The day the computation ends: a "to <date>", else a date the claim
    # states ("On July 23, NVDA hit a 3-day high of 176.10"), else the
    # latest receipted day.
    end: str | None = None
    if start is not None:
        later = find_dates(answer, start[0] + 1, hi)
        end = next((d[1] for d in later if re.search(r"\bto\b", answer[start[0] : d[0]])), None)
    if end is None:
        excluded = start[0] if start is not None else hi
        stated = [d for d in find_dates(answer, lo, hi) if d[0] < excluded and d[0] != m.start]
        clause_before = [d for d in find_dates(answer, *scope.clause) if d[0] < lo]
        pool = stated or clause_before
        end = pool[-1][1] if pool else None

    if lookback and not re.match(r"sessions?|trading", lookback[2], re.IGNORECASE):
        return _AMBIGUOUS_PERIOD  # plain "days": trading or calendar?
    if m.unit == "pct":
        if lookback:
            return Derivation("pct_change", series, end=end, lookback=int(lookback[1]))
        assert start is not None
        return Derivation("pct_change", series, start=start[1], end=end)
    if nday:
        op: Literal["max", "min"] = "max" if nday[2].lower() == "high" else "min"
        return Derivation(op, series, end=end, lookback=int(nday[1]))
    if extremum and lookback:
        word = extremum[1].lower()
        op = "max" if word in ("highest", "peak", "high") else "min"
        return Derivation(op, series, end=end, lookback=int(lookback[1]))
    return None


def _resolve(
    answer: str,
    m: NumberToken,
    known_entities: set[str] | frozenset[str],
    vocab: Vocabulary,
) -> Claim:
    """Attach entity and metric to one Tier 2 numeric token."""
    value, unit = m.value, m.unit
    scope = _scope(answer, m.start)
    cell = _table_cell(answer, m, set(known_entities), vocab)
    if cell is not None:
        entity, metric = cell.entity, cell.metric
    else:
        entity = _entity(answer, m.start, scope, set(known_entities))
        metric = _pick_metric(_keyword_hits(answer, m.start, scope, vocab.synonyms), unit, vocab)
    if (
        cell is None
        and metric is None
        and unit in (None, "USD")
        and _MOVE_TARGET_RE.search(answer, scope.phrase[0], m.start)
    ):
        metric = vocab.move_target
    if negated_by_parentheses(m.parenthesized and not m.signed, value, metric, vocab) or (
        _negated_by_direction(answer, m, scope)
    ):
        value = -value
    derivation = None if cell is not None else _derivation(answer, m, scope, vocab)
    if derivation == _AMBIGUOUS_PERIOD:
        # Unresolved rather than guessed: the claim becomes UNVERIFIABLE.
        metric, derivation = None, None
    if isinstance(derivation, Derivation):
        return Claim(
            value=value,
            span=(m.start, m.end),
            text=m.text,
            tier=2,
            entity=entity,
            metric=derivation.metric,
            unit=unit,
            kind=m.kind,
            resolution=m.resolution,
            derivation=derivation,
        )
    return Claim(
        value=value,
        span=(m.start, m.end),
        text=m.text,
        tier=2,
        entity=entity,
        metric=metric,
        unit=unit,
        timeframe=None if cell is not None else _timeframe(answer, scope),
        as_of=cell.as_of if cell is not None else _date(answer, m.start, scope),
        kind=m.kind,
        resolution=m.resolution,
    )


def _citation_runs(answer: str, citations: list[re.Match[str]]) -> list[list[re.Match[str]]]:
    """Group citation markers separated only by whitespace."""
    runs: list[list[re.Match[str]]] = []
    for cit in citations:
        if runs and not answer[runs[-1][-1].end() : cit.start()].strip():
            runs[-1].append(cit)
        else:
            runs.append([cit])
    return runs


def extract_claims(
    answer: str,
    known_entities: set[str] | frozenset[str] = frozenset(),
    vocabulary: Vocabulary = FINANCE,
) -> Extraction:
    """Extract numeric claims from an answer, Tier 1 then Tier 2, reading
    metric words with the given domain vocabulary."""

    citations = list(_CITATION_RE.finditer(answer))
    numbers = tokenize(answer, exclude=[c.span() for c in citations])
    consumed: set[int] = set()
    claims: list[Claim] = []
    unresolved: list[Claim] = []

    # Tier 1: a run of adjacent citations binds, left to right, to the same
    # number of preceding numbers in the sentence, so "62.3 and -0.42
    # [[rsi]][[macd]]" pairs 62.3 with rsi (issue #12). A single citation
    # is the one-element case: the nearest preceding number. A run with
    # more citations than numbers is ambiguous and binds nothing.
    for group in _citation_runs(answer, citations):
        sent_start, _ = _sentence_bounds(answer, group[0].start())
        candidates = [
            i
            for i, m in enumerate(numbers)
            if i not in consumed
            and m.kind == "point"
            and sent_start <= m.start
            and m.end <= group[0].start()
        ]
        if len(candidates) < len(group):
            continue  # dangling or ambiguous; the matcher flags it via coverage
        for i, cit in zip(candidates[-len(group) :], group, strict=True):
            m = numbers[i]
            consumed.add(i)
            # The same sign rules as Tier 2 (issue #43): a citation pins
            # which fact is meant, not how the sign was written.
            scope = _scope(answer, m.start)
            negated = _negated_by_direction(answer, m, scope)
            claims.append(
                Claim(
                    value=-m.value if negated else m.value,
                    span=(m.start, m.end),
                    text=m.text,
                    tier=1,
                    # What the prose says the number is about; the matcher
                    # checks the cited fact agrees (#95).
                    entity=_entity(answer, m.start, scope, set(known_entities)),
                    as_of=_date(answer, m.start, scope),
                    unit=m.unit,
                    resolution=m.resolution,
                    citation=Citation(receipt_id=cit.group(1), json_ptr=cit.group(2) or "/"),
                    parenthesized=m.parenthesized and not m.signed and not negated,
                )
            )

    # Tier 2: remaining numbers need an entity and a metric in-sentence.
    for i, m in enumerate(numbers):
        if i in consumed:
            continue
        claim = _resolve(answer, m, known_entities, vocabulary)
        if claim.kind != "point" or claim.entity is None or claim.metric is None:
            unresolved.append(claim)
        else:
            claims.append(claim)

    claims.sort(key=lambda c: c.span)
    return Extraction(claims=tuple(claims), unresolved=tuple(unresolved))
