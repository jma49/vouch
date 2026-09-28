"""judge()'s extractor seam: Tier 3 plugs in here (design section 15)."""

from __future__ import annotations

from envelopes import GOLDEN, GOLDEN_KEYS

from vouch_verifier import ClaimExtractor, judge
from vouch_verifier.claims import Extraction, extract_claims
from vouch_verifier.receipts import load_log
from vouch_verifier.verdict import Verdict
from vouch_verifier.vocabulary import FINANCE, Vocabulary


def test_a_substitute_extractor_gets_the_inputs_and_keeps_the_matching() -> None:
    receipts = load_log(GOLDEN, GOLDEN_KEYS)
    seen: list[tuple[str, frozenset[str], Vocabulary]] = []

    def stub(answer: str, entities: frozenset[str], vocabulary: Vocabulary) -> Extraction:
        # Reads the answer its own way: here, as if it had resolved the
        # prose to one plain sentence.
        seen.append((answer, entities, vocabulary))
        return extract_claims("NVDA RSI(14) is at 68 right now.", entities, vocabulary)

    extractor: ClaimExtractor = stub
    extraction, matched = judge("free-form prose", receipts, extractor=extractor)
    assert seen == [("free-form prose", frozenset({"NVDA", "AMD"}), FINANCE)]
    assert [c.value for c in extraction.claims] == [68.0]
    # Verdicts still come from the receipts, not from the extractor.
    assert [m.verdict for m in matched] == [Verdict.CONTRADICTED]
    assert matched[0].fact is not None and matched[0].fact.value == 62.3


def test_the_rule_tiers_are_the_default() -> None:
    receipts = load_log(GOLDEN, GOLDEN_KEYS)
    default = judge("NVDA closed at 181.52.", receipts)
    explicit = judge("NVDA closed at 181.52.", receipts, extractor=extract_claims)
    assert default == explicit
