"""Mutation injector (design section 9).

Takes a passing answer and machine-generates known-bad variants, one
per applicable mutation type. Deterministic given (answer, receipts,
seed): the gold set must be regenerable bit-for-bit.

Two listed mutations are included but expected to evade the MVP
verifier — timeframe_swap (Tier 2 claims carry no timeframe yet) and
false_absence (an absent claim produces no verdict). They stay in the
gold set so the per-mutation recall table reports the gap instead of
hiding it.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from vouch_verifier.claims import Claim, extract_claims
from vouch_verifier.receipts import Receipt

MUTATIONS = (
    "digit_swap",
    "magnitude_shift",
    "entity_swap",
    "timeframe_swap",
    "sign_flip",
    "fabricated_citation",
    "false_absence",
)


@dataclass(frozen=True)
class Mutant:
    answer: str
    mutation: str
    description: str


def _entities(receipts: list[Receipt]) -> set[str]:
    return {f.entity for r in receipts for f in r.facts if f.entity}


def _replace_span(answer: str, span: tuple[int, int], new: str) -> str:
    return answer[: span[0]] + new + answer[span[1] :]


def _digit_swap(answer: str, claims: list[Claim], rng: random.Random) -> Mutant | None:
    for claim in claims:
        text = claim.text
        digits = [i for i, ch in enumerate(text) if ch.isdigit()]
        for a, b in zip(digits, digits[1:]):
            if text[a] != text[b]:
                chars = list(text)
                chars[a], chars[b] = chars[b], chars[a]
                mutated = "".join(chars)
                return Mutant(
                    _replace_span(answer, claim.span, mutated),
                    "digit_swap",
                    f"{text.strip()} -> {mutated.strip()}",
                )
    return None


def _magnitude_shift(answer: str, claims: list[Claim], rng: random.Random) -> Mutant | None:
    for claim in claims:
        text = claim.text
        if "." in text:
            # Move the decimal point one place right.
            m = re.match(r"([-+]?)(\d[\d,]*)\.(\d)(\d*)(\s?%)?$", text)
            if not m:
                continue
            sign, whole, first_dec, rest_dec, pct = m.groups()
            mutated = sign + whole.replace(",", "") + first_dec
            if rest_dec:
                mutated += "." + rest_dec
            mutated += pct or ""
        else:
            m = re.match(r"([-+]?)(\d[\d,]*)(\s?%)?$", text)
            if not m:
                continue
            sign, whole, pct = m.groups()
            mutated = sign + whole.replace(",", "") + "0" + (pct or "")
        return Mutant(
            _replace_span(answer, claim.span, mutated),
            "magnitude_shift",
            f"{text.strip()} -> {mutated.strip()}",
        )
    return None


def _entity_swap(answer: str, claims: list[Claim], entities: set[str], rng: random.Random) -> Mutant | None:
    for claim in claims:
        if claim.entity is None:
            continue
        others = sorted(entities - {claim.entity})
        if not others:
            return None
        other = others[rng.randrange(len(others))]
        # Attribute this claim's sentence to the other entity: replace the
        # entity occurrence closest before the number.
        window = answer[: claim.span[0]]
        idx = window.rfind(claim.entity)
        if idx < 0:
            continue
        mutated = answer[:idx] + other + answer[idx + len(claim.entity) :]
        return Mutant(mutated, "entity_swap", f"{claim.entity} -> {other} for {claim.metric}")
    return None


_TIMEFRAME_SWAPS = [("1d", "1h"), ("daily", "hourly"), ("on the day", "on the hour"), ("today", "this hour")]


def _timeframe_swap(answer: str, claims: list[Claim], rng: random.Random) -> Mutant | None:
    for old, new in _TIMEFRAME_SWAPS:
        if old in answer:
            return Mutant(answer.replace(old, new, 1), "timeframe_swap", f"{old} -> {new}")
    return None


_DIRECTION_SWAPS = [("down", "up"), ("fell", "rose"), ("dropped", "jumped"),
                    ("declined", "advanced"), ("lost", "gained"), ("slid", "climbed")]


def _sign_flip(answer: str, claims: list[Claim], rng: random.Random) -> Mutant | None:
    for claim in claims:
        if claim.unit != "pct":
            continue
        text = claim.text
        if text.lstrip().startswith("-"):
            mutated = text.replace("-", "+", 1)
        elif text.lstrip().startswith("+"):
            mutated = text.replace("+", "-", 1)
        else:
            sentence_start = answer.rfind(".", 0, claim.span[0]) + 1
            sentence = answer[sentence_start : claim.span[0]]
            for old, new in _DIRECTION_SWAPS + [(n, o) for o, n in _DIRECTION_SWAPS]:
                m = re.search(r"(?<!\w)" + old + r"(?!\w)", sentence)
                if m:
                    at = sentence_start + m.start()
                    return Mutant(
                        answer[:at] + new + answer[at + len(old) :],
                        "sign_flip",
                        f"{old} -> {new}",
                    )
            continue
        return Mutant(
            _replace_span(answer, claim.span, mutated),
            "sign_flip",
            f"{text.strip()} -> {mutated.strip()}",
        )
    return None


_CITATION_ID_RE = re.compile(r"\[\[r:([A-Za-z0-9_-]+)#")


def _fabricated_citation(answer: str, claims: list[Claim], rng: random.Random) -> Mutant | None:
    m = _CITATION_ID_RE.search(answer)
    if not m:
        return None
    fake = "0000000000000000"
    mutated = answer[: m.start(1)] + fake + answer[m.end(1) :]
    return Mutant(mutated, "fabricated_citation", f"{m.group(1)} -> {fake}")


def _false_absence(answer: str, claims: list[Claim], rng: random.Random) -> Mutant | None:
    for claim in claims:
        if claim.entity is None or claim.metric is None:
            continue
        start = answer.rfind(".", 0, claim.span[0]) + 1
        end = answer.find(".", claim.span[1])
        end = len(answer) if end < 0 else end + 1
        replacement = f" No {claim.metric} data is available for {claim.entity}."
        return Mutant(
            (answer[:start] + replacement + answer[end:]).strip(),
            "false_absence",
            f"dropped ({claim.entity}, {claim.metric}) = {claim.value}",
        )
    return None


def inject(answer: str, receipts: list[Receipt], seed: int = 0) -> list[Mutant]:
    """Generate one mutant per applicable mutation type."""
    entities = _entities(receipts)
    extraction = extract_claims(answer, known_entities=entities)
    claims = list(extraction.claims)
    rng = random.Random(seed)

    mutants = [
        _digit_swap(answer, claims, rng),
        _magnitude_shift(answer, claims, rng),
        _entity_swap(answer, claims, entities, rng),
        _timeframe_swap(answer, claims, rng),
        _sign_flip(answer, claims, rng),
        _fabricated_citation(answer, claims, rng),
        _false_absence(answer, claims, rng),
    ]
    out = [m for m in mutants if m is not None and m.answer != answer]
    for m in out:
        assert m.mutation in MUTATIONS
    return out
