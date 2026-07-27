"""Synthesize clean answers from a receipt log.

The gold set needs known-good answers to mutate. Rather than shipping
hand-written prose, answers are generated from the receipts themselves
— fully deterministic, regenerated from fixtures at any time (design
section 9), and phrased so the verifier's own Tier 2 keyword table
resolves them.
"""

from __future__ import annotations

from vouch_verifier.receipts import Fact, Receipt

# metric -> sentence template. Phrasing must stay in sync with
# vouch_verifier.claims.DEFAULT_METRIC_SYNONYMS or Tier 2 extraction
# will not resolve the generated claims.
_TEMPLATES: dict[str, str] = {
    "rsi_14": "{entity} RSI is {value}",
    "macd_hist": "{entity} MACD histogram is {value}",
    "close_price": "{entity} closed at {value}",
    "open_price": "{entity} opened at {value}",
    "last_price": "{entity} last price is {value}",
    "change_pct": "{entity} change is {value}%",
    "volume": "{entity} volume is {value}",
}


def _format_value(fact: Fact) -> str:
    if fact.value == int(fact.value):
        return str(int(fact.value))
    return repr(fact.value)


def sentence_for(fact: Fact) -> str | None:
    """Render one fact as a claim sentence, or None if untemplated."""
    template = _TEMPLATES.get(fact.metric)
    if template is None:
        return None
    return template.format(entity=fact.entity, value=_format_value(fact))


def synthesize(receipts: list[Receipt], cited: bool) -> str:
    """Build one clean answer asserting every templated fact.

    cited=True appends the citation protocol marker to each sentence
    (a Tier 1 answer); cited=False produces the same claims bare (a
    Tier 2 answer).
    """
    sentences = []
    for r in receipts:
        for fact in r.facts:
            s = sentence_for(fact)
            if s is None:
                continue
            if cited:
                s += f" [[r:{r.receipt_id}#{fact.json_ptr}]]"
            sentences.append(s + ".")
    return " ".join(sentences)
