"""Numeric span tokenization for claim extraction (design section 5).

Finding the numbers in agent prose is the first place Tier 2 goes
wrong: a date, a clock time, or the "50" in "50-day" is a number but
not a claim, and a multiplier or a range is a claim but not a point
value. This module decides which spans are numeric *claims* and of
what kind, before any entity or metric resolution happens.

Masking is deliberately conservative: a span is only excluded when a
pattern recognizes it as non-claim structure. Anything unrecognized
stays a candidate, and resolution — not tokenization — decides whether
it can be judged.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

Kind = Literal["point", "multiple", "range"]

# Thousands groups must be exactly three digits, so "62," in running
# prose is 62 followed by a comma, not a malformed thousands group.
_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"

_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
_MONTH_NAMED = _MONTH.replace("(?:jan", "(?P<mon>jan", 1)
_MONTH_NUMBERS = {
    name: i + 1
    for i, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
    )
}

# Calendar dates, with named groups (y, mo or mon, d) so the same
# patterns both mask dates out of the claim stream and parse them for
# date-scoped matching.
_DATE_RES = [
    # ISO dates and timestamps: 2026-07-24, 2026-07-24T20:00:00Z
    re.compile(
        r"\b(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})"
        r"(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
    ),
    # Numeric dates: 7/24, 7/24/2026
    re.compile(r"\b(?P<mo>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2,4}))?\b"),
    # Month-name dates: July 24, Jul 24th, July 24, 2026; 24 July 2026
    re.compile(
        rf"\b{_MONTH_NAMED}\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(?P<y>\d{{4}})\b)?",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH_NAMED}(?:,?\s+(?P<y>\d{{4}})\b)?",
        re.IGNORECASE,
    ),
]

_PERIOD_UNIT = (
    r"(?:days?|weeks?|months?|years?|sessions?|hours?|minutes?|mins?|quarters?|periods?|bars?)"
)

# A minute chart timeframe: "15m chart", "5 min candles" (issue #11).
MINUTE_TIMEFRAME = r"\b\d+\s?(?:m|min)\b(?=[\s-]+(?:charts?|candles?|bars?|timeframe|interval))"

# Spans that are structure, not claims. A pattern with a group named
# "m" masks only that group. Order does not matter; overlaps are merged.
_MASKS = [
    *_DATE_RES,
    # Month and year without a day: July 2026
    re.compile(rf"\b{_MONTH}\s+\d{{4}}\b", re.IGNORECASE),
    # Clock times: 4:00 pm, 16:00, 4pm
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\s?[ap]\.?m\.?)?", re.IGNORECASE),
    re.compile(r"\b\d{1,2}\s?[ap]\.?m\.?(?!\w)", re.IGNORECASE),
    # Fiscal periods: Q3, Q3 2026, FY2026, FY 26, H1
    re.compile(r"\b[QH][1-4]\b(?:\s+\d{4}\b)?", re.IGNORECASE),
    re.compile(r"\bFY\s?\d{2,4}\b", re.IGNORECASE),
    # Years introduced by a temporal preposition: "since 2024", "in 2026"
    re.compile(
        r"\b(?:in|since|during|until|through|from|by|of|before|after|fiscal|year)\s+"
        r"(?P<m>(?:19|20)\d{2})\b",
        re.IGNORECASE,
    ),
    # List markers: "1. ", "2) " at the start of a line (issue #15)
    re.compile(r"(?m)^[ \t]*(?P<m>\d+)[.)](?=[ \t])"),
    # Ordinals: 3rd, 52nd
    re.compile(r"\b\d+(?:st|nd|rd|th)\b", re.IGNORECASE),
    # Period lengths: 50-day, 52-week, 5 sessions, 14 days, 3 trading days
    re.compile(rf"\b\d+(?:\.\d+)?[-\s](?:trading\s+)?{_PERIOD_UNIT}\b", re.IGNORECASE),
    # Chart timeframes: 1d, 4h, 1w; and minute charts, "15m chart", only
    # when a chart word follows, since "52.4m shares" is 52.4 million
    re.compile(r"\b\d+[hdw]\b"),
    re.compile(MINUTE_TIMEFRAME, re.IGNORECASE),
    # Indicator parameters: "RSI (14)", "MACD(12, 26, 9)"; the unspaced
    # single-argument form is also caught by _is_parameter
    re.compile(
        r"\b(?:rsi|ema|sma|wma|ma|atr|adx|cci|roc|mfi|macd|stoch(?:astic)?|bollinger|bb)"
        r"\s*\(\s*\d+(?:\s*,\s*\d+)*\s*\)",
        re.IGNORECASE,
    ),
]

# Ranges: "60-65", "between 60 and 65". "from 55 to 62" is deliberately
# absent: in market prose it describes a move, and its endpoint is a
# point claim ("RSI moved from 55.1 to 62.3").
_RANGE_RES = [
    re.compile(rf"(?<![\w.])(?P<a>{_NUM})\s?[-\u2013\u2014]\s?(?P<b>{_NUM})(?![\w.]*\d)"),
    re.compile(rf"\bbetween\s+(?P<a>{_NUM})\s+and\s+(?P<b>{_NUM})", re.IGNORECASE),
]

# Magnitude words and suffixes scale both the value and its displayed
# resolution: "52.4 million" is 52,400,000 give or take 50,000.
_MAGNITUDES = {
    "thousand": 1e3,
    "k": 1e3,
    "million": 1e6,
    "mln": 1e6,
    "mn": 1e6,
    "m": 1e6,
    "billion": 1e9,
    "bn": 1e9,
    "b": 1e9,
    "trillion": 1e12,
    "t": 1e12,
}

# Minus signs agents and renderers actually emit, besides the ASCII
# hyphen: U+2212 MINUS SIGN, U+2012 FIGURE DASH, U+2013 EN DASH, U+FE63
# SMALL HYPHEN-MINUS, U+FF0D FULLWIDTH HYPHEN-MINUS. Dropping one reads
# a negative value as positive (issue #8).
MINUS_SIGNS = "-\u2212\u2012\u2013\ufe63\uff0d"
# Inside a character class the ASCII hyphen must be escaped, or it
# forms a range with its neighbors.
_MINUS_CLASS = "\\-" + MINUS_SIGNS[1:]

_TOKEN_RE = re.compile(
    rf"(?<![\w.+/:{_MINUS_CLASS}])(?P<sign>[+{_MINUS_CLASS}])?(?P<num>{_NUM})"
    r"(?P<mag>\s(?:thousand|million|mln|mn|billion|bn|trillion)\b|(?:bn|[kKmMbBtT])(?!\w))?"
    r"(?P<pct>\s?%|\s(?:percent|per cent|pct)\b)?(?P<mult>[x\u00d7](?!\w))?"
)

# "$181.52", "USD 181.52", "181.52 USD": the currency marks a price.
_CURRENCY_BEFORE_RE = re.compile(r"(?:\$|\bUSD\s?)$")
_CURRENCY_AFTER_RE = re.compile(r"^\s?USD\b")


@dataclass(frozen=True)
class NumberToken:
    """One numeric span that may be a claim."""

    start: int
    end: int
    text: str
    value: float
    unit: str | None  # "pct", "USD", or None
    kind: Kind
    resolution: float  # unit of the last displayed digit: 0.1 for "62.3"
    signed: bool = False  # written with an explicit + or minus sign
    # Wrapped in parentheses, "(1.35%)": an accounting negative or an
    # aside, which only the metric can tell apart (see claims._resolve).
    parenthesized: bool = False


def _merge(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _inside(pos: int, spans: Sequence[tuple[int, int]]) -> bool:
    """pos falls in one of spans, which must be sorted and non-overlapping
    (as _merge returns them): a bisect, not a scan (#19)."""
    i = bisect_right(spans, (pos, float("inf"))) - 1
    return i >= 0 and spans[i][0] <= pos < spans[i][1]


def _is_parameter(text: str, start: int, end: int) -> bool:
    # "RSI(14)": a number in function-call-style parens names the
    # metric's parameter; it is not a claim.
    return (
        start >= 2
        and text[start - 1] == "("
        and text[start - 2].isalnum()
        and text[end : end + 1] == ")"
    )


def _value(num: str) -> float:
    return float(num.replace(",", ""))


def _resolution(num: str) -> float:
    _, dot, decimals = num.partition(".")
    return 10.0 ** -len(decimals) if dot else 1.0


def find_dates(text: str, start: int = 0, end: int | None = None) -> list[tuple[int, str]]:
    """Calendar dates in text[start:end] as (position, date), in order.

    A date with a year is "YYYY-MM-DD"; without one it is "--MM-DD"
    (the ISO 8601 form for a recurring day), and callers match it
    against any year.
    """
    stop = len(text) if end is None else end
    found: dict[int, tuple[int, str]] = {}
    for rx in _DATE_RES:
        for m in rx.finditer(text, start, stop):
            groups = m.groupdict()
            month = (
                _MONTH_NUMBERS[groups["mon"][:3].lower()]
                if groups.get("mon")
                else int(groups["mo"])
            )
            day = int(groups["d"])
            if not (1 <= month <= 12 and 1 <= day <= 31):
                continue
            year = groups.get("y")
            if year is None:
                date = f"--{month:02d}-{day:02d}"
            else:
                full = int(year) + (2000 if len(year) == 2 else 0)
                date = f"{full:04d}-{month:02d}-{day:02d}"
            # Overlapping patterns: keep the longest match at a position.
            prev = found.get(m.start())
            if prev is None or m.end() > prev[0]:
                found[m.start()] = (m.end(), date)
    return [(pos, date) for pos, (_, date) in sorted(found.items())]


def tokenize(text: str, exclude: Sequence[tuple[int, int]] = ()) -> list[NumberToken]:
    """Return candidate numeric claims in text order.

    `exclude` adds spans the caller already knows are not prose (the
    citation markers of the Tier 1 protocol).
    """
    masked = _merge(
        [
            *exclude,
            *(
                m.span("m") if "m" in rx.groupindex else m.span()
                for rx in _MASKS
                for m in rx.finditer(text)
            ),
        ]
    )
    tokens: list[NumberToken] = []

    ranges: list[tuple[int, int]] = []
    for rx in _RANGE_RES:
        for m in rx.finditer(text):
            if _inside(m.start("a"), masked) or _inside(m.start("b"), masked):
                continue
            if any(s <= m.start() < e for s, e in ranges):  # few ranges per answer
                continue
            ranges.append(m.span())
            for g in ("a", "b"):
                tokens.append(
                    NumberToken(
                        start=m.start(g),
                        end=m.end(g),
                        text=m[g],
                        value=_value(m[g]),
                        unit=None,
                        kind="range",
                        resolution=_resolution(m[g]),
                    )
                )
    masked = _merge([*masked, *ranges])

    for m in _TOKEN_RE.finditer(text):
        if _inside(m.start("num"), masked) or _is_parameter(text, m.start(), m.end("num")):
            continue
        value = _value(m["num"])
        scale = _MAGNITUDES[m["mag"].strip().lower()] if m["mag"] else 1.0
        value *= scale
        if m["sign"] and m["sign"] != "+":
            value = -value
        kind: Kind = "multiple" if m["mult"] else "point"
        unit = "pct" if m["pct"] else None
        if unit is None and (
            # Only the few characters before the number can be "$" or "USD ";
            # searching the whole prefix made tokenization quadratic (#19).
            _CURRENCY_BEFORE_RE.search(text, max(0, m.start() - 4), m.start())
            or _CURRENCY_AFTER_RE.match(text[m.end() :])
        ):
            unit = "USD"
        tokens.append(
            NumberToken(
                start=m.start(),
                end=m.end(),
                text=m.group(),
                value=value,
                unit=unit,
                kind=kind,
                resolution=_resolution(m["num"]) * scale,
                signed=m["sign"] is not None,
                parenthesized=m.start() > 0
                and text[m.start() - 1] == "("
                and text[m.end() : m.end() + 1] == ")",
            )
        )

    tokens.sort(key=lambda t: t.start)
    return tokens
