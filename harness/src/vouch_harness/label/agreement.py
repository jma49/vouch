"""Inter-annotator agreement over two labelers' span labels.

A human-labeled evaluation is only as good as its labels; the report
states agreement on the doubly labeled subset (docs/roadmap.md Phase 2)
so a reader can judge how far to trust the numbers built on them.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from vouch_harness.label.store import LabelRecord, SpanKey


@dataclass(frozen=True)
class Agreement:
    shared: int  # spans both labelers labeled
    observed: float  # raw agreement rate
    kappa: float  # Cohen's kappa; 1 = perfect, 0 = chance level
    confusion: dict[tuple[str, str], int]  # (label_a, label_b) -> count
    only_a: int
    only_b: int


def cohen_kappa(pairs: list[tuple[str, str]]) -> float:
    n = len(pairs)
    if n == 0:
        return float("nan")
    observed = sum(1 for a, b in pairs if a == b) / n
    count_a = Counter(a for a, _ in pairs)
    count_b = Counter(b for _, b in pairs)
    expected = sum(count_a[k] * count_b[k] for k in count_a) / (n * n)
    if expected == 1.0:
        return 1.0  # both labelers used one label throughout, identically
    return (observed - expected) / (1 - expected)


def agreement(a: dict[SpanKey, LabelRecord], b: dict[SpanKey, LabelRecord]) -> Agreement:
    shared = sorted(a.keys() & b.keys())
    pairs = [(a[k].label, b[k].label) for k in shared]
    return Agreement(
        shared=len(shared),
        observed=sum(1 for x, y in pairs if x == y) / len(pairs) if pairs else float("nan"),
        kappa=cohen_kappa(pairs),
        confusion=dict(Counter(pairs)),
        only_a=len(a.keys() - b.keys()),
        only_b=len(b.keys() - a.keys()),
    )
