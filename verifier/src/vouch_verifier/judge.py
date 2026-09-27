"""The verifier's one entry point: judge an answer against receipts.

Extraction and matching take the same inputs everywhere (entities from
the receipts, a domain vocabulary, a tolerance policy, an optional
backtest moment), and five callers used to assemble them by hand, so
only some honoured --vocabulary or --as-of (#105). They call this.
"""

from __future__ import annotations

from datetime import datetime

from vouch_verifier.claims import Extraction, extract_claims
from vouch_verifier.matcher import MatchedClaim, match_claims
from vouch_verifier.receipts import Receipt
from vouch_verifier.verdict import Tolerance
from vouch_verifier.vocabulary import FINANCE, Vocabulary


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
) -> tuple[Extraction, list[MatchedClaim]]:
    """Extract the answer's claims and assign each a verdict."""
    extraction = extract_claims(answer, receipt_entities(receipts), vocabulary)
    return extraction, match_claims(
        extraction, receipts, tolerances, as_of=as_of, vocabulary=vocabulary
    )
