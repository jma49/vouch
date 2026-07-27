"""Gold set generation and mutation detection against the golden log."""

from pathlib import Path

import pytest

from vouch_verifier.claims import extract_claims
from vouch_verifier.matcher import match_claims
from vouch_verifier.receipts import load_log
from vouch_verifier.verdict import Verdict

from vouch_harness.answers import synthesize
from vouch_harness.gold import build_gold_set
from vouch_harness.mutate import inject

GOLDEN = Path(__file__).parent.parent.parent / "testdata" / "receipts_golden.jsonl"
KEY = b"vouch-golden-key"


@pytest.fixture(scope="module")
def receipts():
    return load_log(GOLDEN, key=KEY)


def entities(receipts):
    return {f.entity for r in receipts for f in r.facts if f.entity}


def verdicts_for(answer, receipts):
    extraction = extract_claims(answer, known_entities=entities(receipts))
    return [mc.verdict for mc in match_claims(extraction, receipts)]


def is_flagged(answer, receipts):
    return any(
        v in (Verdict.CONTRADICTED, Verdict.UNSUPPORTED)
        for v in verdicts_for(answer, receipts)
    )


def test_synthesized_answers_are_clean(receipts):
    for cited in (True, False):
        answer = synthesize(receipts, cited=cited)
        vs = verdicts_for(answer, receipts)
        assert vs, "synthesized answer produced no claims"
        assert all(v is Verdict.SUPPORTED for v in vs), (cited, vs)


def test_gold_set_is_deterministic(receipts):
    assert build_gold_set(receipts, seed=7) == build_gold_set(receipts, seed=7)


def test_gold_set_has_clean_and_mutant_cases(receipts):
    cases = build_gold_set(receipts)
    names = {c.name for c in cases}
    assert "clean_cited" in names and "clean_uncited" in names
    mutations = {c.mutation for c in cases if c.mutation}
    # These five must be present; timeframe_swap needs timeframe words in
    # the answer and false_absence applies too, via entity+metric claims.
    for m in ("digit_swap", "magnitude_shift", "entity_swap", "sign_flip",
              "fabricated_citation", "false_absence"):
        assert m in mutations, f"missing mutation {m}"


def test_mutants_differ_from_original(receipts):
    for cited in (True, False):
        answer = synthesize(receipts, cited=cited)
        for m in inject(answer, receipts):
            assert m.answer != answer, m.mutation


def test_detectable_mutations_are_flagged(receipts):
    """The mutation classes the MVP verifier promises to catch."""
    detectable = {"digit_swap", "magnitude_shift", "sign_flip", "fabricated_citation"}
    for cited in (True, False):
        answer = synthesize(receipts, cited=cited)
        for m in inject(answer, receipts):
            if m.mutation in detectable:
                assert is_flagged(m.answer, receipts), (cited, m.mutation, m.description)


def test_entity_swap_flagged_on_uncited(receipts):
    # Uncited entity swaps must be flagged (wrong entity's value).
    # Cited ones are caught only if the swapped value drifts; not asserted.
    answer = synthesize(receipts, cited=False)
    swaps = [m for m in inject(answer, receipts) if m.mutation == "entity_swap"]
    assert swaps
    for m in swaps:
        assert is_flagged(m.answer, receipts), m.description


def test_clean_answers_not_flagged_false_positive_check(receipts):
    for case in build_gold_set(receipts):
        if case.mutation is None:
            assert not is_flagged(case.answer, receipts), case.name
