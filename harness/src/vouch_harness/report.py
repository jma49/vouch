"""Eval report rendering: distributions, not points (design section 10)."""

from __future__ import annotations

import json
import random

from vouch_harness.eval import EvalResult, Stats, summarize


def _stats_for(result: EvalResult, rng: random.Random) -> dict[str, Stats]:
    out = {}
    for mutation in result.mutations:
        out[mutation] = summarize(result.recall_series(mutation), rng)
    return out


def _overall(result: EvalResult, rng: random.Random) -> dict[str, Stats]:
    return {
        "detection": summarize(
            [
                sum(1 for n, m in r.mutation_of.items() if m and r.flagged[n])
                / max(1, sum(1 for m in r.mutation_of.values() if m))
                for r in result.runs
            ],
            rng,
        ),
        "false_positive_rate": summarize([r.false_positive_rate for r in result.runs], rng),
        "coverage": summarize([r.coverage for r in result.runs], rng),
        "tier1_share": summarize([r.tier1_share for r in result.runs], rng),
    }


def to_markdown(result: EvalResult, seed: int = 0) -> str:
    rng = random.Random(seed)
    overall = _overall(result, rng)
    per_mutation = _stats_for(result, rng)
    n = len(result.runs)

    def fmt(s: Stats) -> str:
        return f"{s.mean:.2f} ± {s.std:.2f} [{s.ci_lo:.2f}, {s.ci_hi:.2f}]"

    lines = [
        "# vouch eval report",
        "",
        f"Runs: {n} (vouch does not report single-run scores; design section 8.3)",
        "",
        "## Overall (mean ± std [bootstrap 95% CI])",
        "",
        f"- Mutation detection rate: {fmt(overall['detection'])}",
        f"- False positive rate (clean answers): {fmt(overall['false_positive_rate'])}",
        f"- Claim coverage: {fmt(overall['coverage'])}",
        f"- Tier 1 (cited) share of claims: {fmt(overall['tier1_share'])}",
        f"- Verdict stability across runs: {result.stability:.2f}",
        "",
        "## Per-mutation recall",
        "",
        "| mutation | recall | std | range | 95% CI |",
        "|---|---|---|---|---|",
    ]
    for mutation in result.mutations:
        s = per_mutation[mutation]
        lines.append(
            f"| {mutation} | {s.mean:.2f} | {s.std:.2f} "
            f"| [{s.lo:.2f}, {s.hi:.2f}] | [{s.ci_lo:.2f}, {s.ci_hi:.2f}] |"
        )
    lines += [
        "",
        "## Tolerance policy",
        "",
    ]
    for name, t in sorted(result.tolerances.items()):
        lines.append(f"- `{name}`: abs={t.abs} rel={t.rel} display_rel={t.display_rel}")
    lines.append("")
    return "\n".join(lines)


def to_json(result: EvalResult, seed: int = 0) -> str:
    rng = random.Random(seed)

    def dump(s: Stats) -> dict:
        return {"mean": s.mean, "std": s.std, "min": s.lo, "max": s.hi,
                "ci95": [s.ci_lo, s.ci_hi]}

    payload = {
        "runs": len(result.runs),
        "overall": {k: dump(v) for k, v in _overall(result, rng).items()},
        "stability": result.stability,
        "per_mutation_recall": {
            m: dump(s) for m, s in _stats_for(result, rng).items()
        },
        "tolerance_policy": {
            name: {"abs": t.abs, "rel": t.rel, "display_rel": t.display_rel}
            for name, t in sorted(result.tolerances.items())
        },
    }
    return json.dumps(payload, indent=2)
