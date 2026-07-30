"""Repeated-run evaluation with variance reporting (design sections 8.3, 10).

Benchmarks that print a single-run score are part of the reliability
problem this project exists to address. Every eval here runs N times
(each run regenerates the gold set with a distinct seed) and reports
mean, standard deviation, range, and a bootstrap confidence interval.
run_eval refuses N < 2 outright — that refusal is a product decision.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field

from vouch_verifier.claims import extract_claims
from vouch_verifier.matcher import DEFAULT_TOLERANCES, match_claims
from vouch_verifier.receipts import Receipt
from vouch_verifier.verdict import Tolerance, Verdict

from vouch_harness.gold import build_gold_set
from vouch_harness.mutate import MUTATIONS


@dataclass(frozen=True)
class RunResult:
    seed: int
    flagged: dict[str, bool]            # case name -> was flagged
    mutation_of: dict[str, str | None]  # case name -> mutation (None = clean)
    tier1_share: float
    coverage: float

    def recall(self, mutation: str) -> float | None:
        cases = [n for n, m in self.mutation_of.items() if m == mutation]
        if not cases:
            return None
        return sum(1 for n in cases if self.flagged[n]) / len(cases)

    @property
    def false_positive_rate(self) -> float:
        clean = [n for n, m in self.mutation_of.items() if m is None]
        if not clean:
            return 0.0
        return sum(1 for n in clean if self.flagged[n]) / len(clean)


@dataclass(frozen=True)
class EvalResult:
    runs: tuple[RunResult, ...]
    tolerances: dict[str, Tolerance] = field(default_factory=lambda: DEFAULT_TOLERANCES)

    @property
    def mutations(self) -> list[str]:
        present = {m for r in self.runs for m in r.mutation_of.values() if m}
        return [m for m in MUTATIONS if m in present]

    def recall_series(self, mutation: str) -> list[float]:
        return [v for r in self.runs if (v := r.recall(mutation)) is not None]

    @property
    def stability(self) -> float:
        """Fraction of gold cases on which every run agrees."""
        names = set.intersection(*(set(r.flagged) for r in self.runs))
        if not names:
            return 0.0
        agreed = sum(
            1 for n in names if len({r.flagged[n] for r in self.runs}) == 1
        )
        return agreed / len(names)


@dataclass(frozen=True)
class Stats:
    mean: float
    std: float
    lo: float
    hi: float
    ci_lo: float
    ci_hi: float


def summarize(values: list[float], rng: random.Random, resamples: int = 1000) -> Stats:
    """Mean, std, range, and a bootstrap 95% CI over per-run scores."""
    mean = statistics.mean(values)
    std = statistics.stdev(values) if len(values) > 1 else 0.0
    boots = []
    for _ in range(resamples):
        sample = [values[rng.randrange(len(values))] for _ in values]
        boots.append(statistics.mean(sample))
    boots.sort()
    return Stats(
        mean=mean,
        std=std,
        lo=min(values),
        hi=max(values),
        ci_lo=boots[int(0.025 * resamples)],
        ci_hi=boots[int(0.975 * resamples) - 1],
    )


def _run_once(receipts: list[Receipt], seed: int, tolerances: dict[str, Tolerance]) -> RunResult:
    entities = {f.entity for r in receipts for f in r.facts if f.entity}
    flagged: dict[str, bool] = {}
    mutation_of: dict[str, str | None] = {}
    tier1 = judged = total = 0

    for case in build_gold_set(receipts, seed=seed):
        extraction = extract_claims(case.answer, known_entities=entities)
        matched = match_claims(extraction, receipts, tolerances)
        flagged[case.name] = any(
            mc.verdict in (Verdict.CONTRADICTED, Verdict.UNSUPPORTED) for mc in matched
        )
        mutation_of[case.name] = case.mutation
        tier1 += sum(1 for mc in matched if mc.claim.tier == 1)
        judged += sum(1 for mc in matched if mc.verdict is not Verdict.UNVERIFIABLE)
        total += len(matched)

    return RunResult(
        seed=seed,
        flagged=flagged,
        mutation_of=mutation_of,
        tier1_share=tier1 / total if total else 0.0,
        coverage=judged / total if total else 0.0,
    )


def run_eval(
    receipts: list[Receipt],
    n: int = 10,
    seed: int = 0,
    tolerances: dict[str, Tolerance] | None = None,
) -> EvalResult:
    """Run the gold-set eval N times. Refuses N < 2: single-run scores
    are exactly the artifact this project refuses to produce."""
    if n < 2:
        raise ValueError(
            "vouch refuses to report a single-run score (docs/design.md "
            "section 8.3); use n >= 2"
        )
    tol = DEFAULT_TOLERANCES if tolerances is None else tolerances
    runs = tuple(_run_once(receipts, seed + i, tol) for i in range(n))
    return EvalResult(runs=runs, tolerances=tol)
