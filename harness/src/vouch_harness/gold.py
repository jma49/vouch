"""Gold set construction (design section 9).

A verifier without a gold set is a demo, not a measurement. The gold
set is derived entirely from a receipt log: clean answers synthesized
from the receipts (cited and uncited variants), plus every applicable
mutant of each. Same log + same seed -> same gold set, bit for bit.
"""

from __future__ import annotations

from dataclasses import dataclass

from vouch_verifier.receipts import Receipt

from vouch_harness.answers import synthesize
from vouch_harness.mutate import inject


@dataclass(frozen=True)
class GoldCase:
    name: str
    answer: str
    mutation: str | None  # None = clean; the verifier must NOT flag it
    description: str = ""

    @property
    def expect_bad(self) -> bool:
        return self.mutation is not None


def build_gold_set(receipts: list[Receipt], seed: int = 0) -> list[GoldCase]:
    cases: list[GoldCase] = []
    for style, cited in (("cited", True), ("uncited", False)):
        answer = synthesize(receipts, cited=cited)
        if not answer:
            continue
        cases.append(GoldCase(name=f"clean_{style}", answer=answer, mutation=None))
        for i, m in enumerate(inject(answer, receipts, seed=seed)):
            cases.append(
                GoldCase(
                    name=f"{m.mutation}_{style}_{i}",
                    answer=m.answer,
                    mutation=m.mutation,
                    description=m.description,
                )
            )
    return cases
