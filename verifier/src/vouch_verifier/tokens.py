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
_PERIOD_UNIT = (
    r"(?:days?|weeks?|months?|years?|sessions?|hours?|minutes?|mins?|quarters?|periods?|bars?)"
)

# Spans that are structure, not claims. A pattern with a group named
# "m" masks only that group. Order does not matter; overlaps are merged.
_MASKS = [
    # ISO dates and timestamps: 2026-07-24, 2026-07-24T20:00:00Z
    re.compile(
        r"\b\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
    ),
    # Numeric dates: 7/24, 7/24/2026
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),
    # Month-name dates: July 24, Jul 24th, July 24, 2026; 24 July 2026; July 2026
    re.compile(rf"\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?\b(?:,?\s+\d{{4}}\b)?", re.IGNORECASE),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}(?:,?\s+\d{{4}}\b)?", re.IGNORECASE),
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
    # Ordinals: 3rd, 52nd
    re.compile(r"\b\d+(?:st|nd|rd|th)\b", re.IGNORECASE),
    # Period lengths: 50-day, 52-week, 5 sessions, 14 days
    re.compile(rf"\b\d+(?:\.\d+)?[-\s]{_PERIOD_UNIT}\b", re.IGNORECASE),
    # Chart timeframes: 1d, 4h, 1w
    re.compile(r"\b\d+[hdw]\b"),
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

_TOKEN_RE = re.compile(
    rf"(?<![\w.\-+/:])(?P<sign>[-+])?(?P<num>{_NUM})"
    r"(?P<mag>\s(?:thousand|million|mln|mn|billion|bn|trillion)\b|(?:bn|[kKmMbBtT])(?!\w))?"
    r"(?P<pct>\s?%)?(?P<mult>[x\u00d7](?!\w))?"
)


@dataclass(frozen=True)
class NumberToken:
    """One numeric span that may be a claim."""

    start: int
    end: int
    text: str
    value: float
    unit: str | None  # "pct" or None
    kind: Kind
    resolution: float  # unit of the last displayed digit: 0.1 for "62.3"


def _merge(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _inside(pos: int, spans: Sequence[tuple[int, int]]) -> bool:
    return any(s <= pos < e for s, e in spans)


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
            if _inside(m.start(), ranges):
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
        if m["sign"] == "-":
            value = -value
        kind: Kind = "multiple" if m["mult"] else "point"
        tokens.append(
            NumberToken(
                start=m.start(),
                end=m.end(),
                text=m.group(),
                value=value,
                unit="pct" if m["pct"] else None,
                kind=kind,
                resolution=_resolution(m["num"]) * scale,
            )
        )

    tokens.sort(key=lambda t: t.start)
    return tokens
