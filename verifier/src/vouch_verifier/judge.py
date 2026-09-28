"""The verifier's one entry point: judge an answer against receipts.

Extraction and matching take the same inputs everywhere (entities from
the receipts, a domain vocabulary, a tolerance policy, an optional
backtest moment), and five callers used to assemble them by hand, so
only some honoured --vocabulary or --as-of (#105). They call this.

Extraction is the part expected to be replaced (design section 15):
the rule tiers are the default ClaimExtractor, and Tier 3 is meant to
be another one passed here, so no caller changes when it lands.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from vouch_verifier.claims import Extraction, extract_claims
from vouch_verifier.matcher import MatchedClaim, match_claims
from vouch_verifier.receipts import Receipt
from vouch_verifier.verdict import Tolerance
from vouch_verifier.vocabulary import FINANCE, Vocabulary


class ClaimExtractor(Protocol):
    """Turns an answer into claims. It sees the answer, the entities the
    receipts name, and the domain vocabulary; it never sees the
    receipts' values, so it cannot shape a claim to fit them."""

    def __call__(
        self, answer: str, known_entities: frozenset[str], vocabulary: Vocabulary, /
    ) -> Extraction: ...


def receipt_entities(receipts: list[Receipt]) -> set[str]:
    """Every entity the receipts name: the only ones a claim can be about."""
    return {f.entity for r in receipts for f in r.facts if f.entity}


def judge(
    answer: str,
    receipts: list[Receipt],
    tolerances: dict[str, Tolerance] | None = None,
    *,
    vocabulary: Vocabulary = FINANCE,
    as_of: datetime | None = None,
    extractor: ClaimExtractor = extract_claims,
) -> tuple[Extraction, list[MatchedClaim]]:
    """Extract the answer's claims and assign each a verdict."""
    extraction = extractor(answer, frozenset(receipt_entities(receipts)), vocabulary)
    return extraction, match_claims(
        extraction, receipts, tolerances, as_of=as_of, vocabulary=vocabulary
    )
