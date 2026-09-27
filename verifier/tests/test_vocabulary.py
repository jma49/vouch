"""Domain vocabularies (#88): finance is the default; others load from YAML."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch_verifier.claims import DEFAULT_METRIC_SYNONYMS, extract_claims
from vouch_verifier.vocabulary import FINANCE, load_vocabulary

PACK = Path(__file__).resolve().parents[2] / "examples" / "analytics" / "vocabulary.yaml"


def test_finance_is_the_default_and_unchanged() -> None:
    assert dict(FINANCE.synonyms) == DEFAULT_METRIC_SYNONYMS
    [claim] = extract_claims("NVDA closed at 181.52.", {"NVDA"}).claims
    assert claim.metric == "close_price"


def test_analytics_pack_loads_and_resolves() -> None:
    vocab = load_vocabulary(PACK)
    assert vocab.units["revenue"] == "USD" and not vocab.signed and vocab.pct_fallback is None
    [claim] = extract_claims("EMEA's average order value was $405.50.", {"EMEA"}, vocab).claims
    assert (claim.entity, claim.metric) == ("EMEA", "avg_order_value")


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("synonyms: {sales: revenue}\ncolour: red\n", "unknown keys"),
        ("units: {revenue: USD}\n", "non-empty mapping"),
        ("synonyms: {sales: revenue}\nunits: {revenue: EUR}\n", "unit must be one of"),
        ("synonyms: {sales: revenue}\npct_fallback: growth\n", "not a metric"),
        ("synonyms: {sales: revenue}\nsigned: revenue\n", "must be a list"),
        ("- a\n", "expected a mapping"),
    ],
)
def test_bad_vocabularies_are_rejected(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "v.yaml"
    path.write_text(body)
    with pytest.raises(ValueError, match=message):
        load_vocabulary(path)
