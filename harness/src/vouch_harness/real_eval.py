"""vouch-eval-real: score real agent runs against human labels.

    vouch-eval-real --labeler NAME [--runs eval/runs] [--labels eval/labels]

Two questions, kept apart because they rest on different evidence:

1. How good is the verifier? Its verdicts are compared with a human's
   labels span by span (docs/labeling.md). Only labeled runs count.
2. How often do models misreport their tools? Measured two ways: from
   the human labels where they exist, and as the verifier's estimate
   over every run, labeled or not. The report always says which is
   which.

Confidence intervals resample runs, not claims: claims in one answer
are correlated, and treating them as independent overstates certainty.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from vouch_harness.eval import Stats, summarize
from vouch_harness.label import store
from vouch_harness.label.agreement import agreement
from vouch_harness.label.runs import discover
from vouch_verifier.claims import extract_claims
from vouch_verifier.matcher import load_tolerances, match_claims
from vouch_verifier.receipts import load_log
from vouch_verifier.verdict import FAILURES, Tolerance

# A human label that means the answer misreports its tools.
BAD = frozenset({"CONTRADICTED", "UNSUPPORTED", "STALE"})
# Human labels that are not claims the verifier should judge at all.
NOT_CLAIMS = frozenset({"NOT_A_CLAIM"})
MISSED = "(not extracted)"  # the verifier produced no verdict for this span


@dataclass
class RunScore:
    run: str
    model: str
    sample: int
    verifier: dict[tuple[int, int], str]  # span -> verdict
    human: dict[tuple[int, int], str] = field(default_factory=dict)  # span -> label

    @property
    def labeled(self) -> bool:
        return bool(self.human)

    def pairs(self) -> list[tuple[str, str]]:
        """(human label, verifier verdict) per human-labeled span.

        Exact span match first; otherwise a verifier span overlapping the
        human one (a hand-added span may be drawn slightly differently).
        """
        out = []
        for (hs, he), label in sorted(self.human.items()):
            verdict = self.verifier.get((hs, he))
            if verdict is None:
                verdict = next(
                    (v for (vs, ve), v in self.verifier.items() if vs < he and hs < ve), MISSED
                )
            out.append((label, verdict))
        return out


def score_runs(
    runs_dir: Path,
    labels: dict[store.SpanKey, store.LabelRecord],
    tolerances: dict[str, Tolerance],
    key: bytes | None,
) -> list[RunScore]:
    human: dict[str, dict[tuple[int, int], str]] = defaultdict(dict)
    for (run, start, end), rec in labels.items():
        human[run][(start, end)] = rec.label
    scores = []
    for run_id in discover(runs_dir):
        d = runs_dir / run_id
        answer = (d / "answer.txt").read_text(encoding="utf-8").rstrip("\n")
        receipts = (
            load_log(d / "receipts.jsonl", key=key) if (d / "receipts.jsonl").exists() else []
        )
        entities = {f.entity for r in receipts for f in r.facts if f.entity}
        matched = match_claims(
            extract_claims(answer, known_entities=entities), receipts, tolerances
        )
        model, _, sample = run_id.split("/")
        scores.append(
            RunScore(
                run=run_id,
                model=model,
                sample=int(sample.lstrip("s")),
                verifier={mc.claim.span: mc.verdict.value for mc in matched},
                human=human.get(run_id, {}),
            )
        )
    return scores


@dataclass(frozen=True)
class Detection:
    """Does the verifier flag the claims a human marked as misreported?"""

    tp: int
    fp: int
    fn: int
    tn: int
    precision: Stats | None
    recall: Stats | None


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def _counts(pairs: list[tuple[str, str]]) -> tuple[int, int, int, int]:
    tp = fp = fn = tn = 0
    for label, verdict in pairs:
        if label in NOT_CLAIMS:
            continue
        bad, flagged = label in BAD, verdict in {v.value for v in FAILURES}
        tp += bad and flagged
        fp += (not bad) and flagged
        fn += bad and not flagged
        tn += (not bad) and not flagged
    return tp, fp, fn, tn


def _bootstrap(
    values: list[list[tuple[str, str]]], metric: str, rng: random.Random, resamples: int = 1000
) -> Stats | None:
    """Metric over runs, with a CI from resampling whole runs."""

    def compute(sample: list[list[tuple[str, str]]]) -> float | None:
        tp, fp, fn, _ = _counts([p for run in sample for p in run])
        return _ratio(tp, tp + fp) if metric == "precision" else _ratio(tp, tp + fn)

    point = compute(values)
    if point is None:
        return None
    boots = [
        v
        for _ in range(resamples)
        if (v := compute([values[rng.randrange(len(values))] for _ in values])) is not None
    ]
    boots.sort()
    return Stats(
        mean=point,
        std=statistics.stdev(boots) if len(boots) > 1 else 0.0,
        lo=min(boots),
        hi=max(boots),
        ci_lo=boots[int(0.025 * len(boots))],
        ci_hi=boots[max(0, int(0.975 * len(boots)) - 1)],
    )


def detection(scores: list[RunScore], seed: int = 0) -> Detection:
    per_run = [s.pairs() for s in scores if s.labeled]
    tp, fp, fn, tn = _counts([p for run in per_run for p in run])
    rng = random.Random(seed)
    return Detection(
        tp, fp, fn, tn, _bootstrap(per_run, "precision", rng), _bootstrap(per_run, "recall", rng)
    )


@dataclass(frozen=True)
class ModelRates:
    model: str
    runs: int
    claim_rate: Stats | None  # misreported claims / judged claims, per sample
    answer_rate: Stats | None  # answers with at least one misreported claim, per sample


def _rates(scores: list[RunScore], source: str, rng: random.Random) -> list[ModelRates]:
    """Per model; each sample index is one run of the whole task set, so
    the spread across samples is the model's run-to-run variance."""
    out = []
    for model in sorted({s.model for s in scores}):
        mine = [s for s in scores if s.model == model and (source == "verifier" or s.labeled)]
        by_sample: dict[int, list[RunScore]] = defaultdict(list)
        for s in mine:
            by_sample[s.sample].append(s)
        claim_rates, answer_rates = [], []
        for runs in by_sample.values():
            judged = bad = bad_answers = 0
            for s in runs:
                if source == "human":
                    labels = [
                        lb for lb in s.human.values() if lb not in NOT_CLAIMS | {"UNVERIFIABLE"}
                    ]
                    n_bad = sum(lb in BAD for lb in labels)
                else:
                    labels = [v for v in s.verifier.values() if v != "UNVERIFIABLE"]
                    n_bad = sum(v in {f.value for f in FAILURES} for v in labels)
                judged += len(labels)
                bad += n_bad
                bad_answers += n_bad > 0
            if judged:
                claim_rates.append(bad / judged)
            answer_rates.append(bad_answers / len(runs))
        out.append(
            ModelRates(
                model=model,
                runs=len(mine),
                claim_rate=summarize(claim_rates, rng) if claim_rates else None,
                answer_rate=summarize(answer_rates, rng) if answer_rates else None,
            )
        )
    return out


def _fmt(s: Stats | None) -> str:
    if s is None:
        return "n/a"
    return f"{s.mean:.2f} ± {s.std:.2f} [{s.ci_lo:.2f}, {s.ci_hi:.2f}]"


def to_markdown(
    scores: list[RunScore],
    labeler: str,
    other_labels: dict[str, dict[store.SpanKey, store.LabelRecord]],
    primary: dict[store.SpanKey, store.LabelRecord],
    seed: int = 0,
) -> str:
    labeled = [s for s in scores if s.labeled]
    lines = [
        "# vouch real-agent evaluation",
        "",
        f"Runs: {len(scores)} across {len({s.model for s in scores})} model(s); "
        f"{len(labeled)} labeled by `{labeler}`. Upstream data is synthetic "
        "(vouch_harness.market).",
        "",
        "## Verifier against human labels",
        "",
    ]
    if not labeled:
        lines += ["No labeled runs yet (`vouch-label serve`).", ""]
    else:
        det = detection(scores, seed)
        lines += [
            "Flagging misreported claims (human CONTRADICTED, UNSUPPORTED, or STALE) "
            "as CONTRADICTED, UNSUPPORTED, or STALE. Mean ± std [95% CI, runs resampled].",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Precision | {_fmt(det.precision)} |",
            f"| Recall | {_fmt(det.recall)} |",
            f"| Counts | TP {det.tp}, FP {det.fp}, FN {det.fn}, TN {det.tn} |",
            "",
            "Confusion (rows: human label, columns: verifier verdict):",
            "",
        ]
        pairs = Counter(p for s in labeled for p in s.pairs())
        rows = sorted({h for h, _ in pairs})
        cols = sorted({v for _, v in pairs})
        lines.append("| human \\ verifier | " + " | ".join(cols) + " |")
        lines.append("|---" * (len(cols) + 1) + "|")
        for h in rows:
            lines.append(f"| {h} | " + " | ".join(str(pairs.get((h, v), 0)) for v in cols) + " |")
        lines.append("")
    for name, other in sorted(other_labels.items()):
        a = agreement(primary, other)
        lines.append(
            f"Agreement with `{name}`: {a.shared} shared spans, "
            f"observed {a.observed:.2f}, Cohen's kappa {a.kappa:.2f}."
        )
    if other_labels:
        lines.append("")

    rng = random.Random(seed)
    lines += [
        "## Misreported numbers by model",
        "",
        "Rate of claims that are CONTRADICTED, UNSUPPORTED, or STALE among judged claims, and "
        "of answers containing at least one. One value per sample index (a full pass over the "
        "task set); mean ± std [95% CI] across samples.",
        "",
        "| Model | Source | Runs | Claim rate | Answer rate |",
        "|---|---|---|---|---|",
    ]
    for source in ("human", "verifier"):
        for r in _rates(scores, source, rng):
            if r.runs:
                label = "human labels" if source == "human" else "verifier estimate"
                lines.append(
                    f"| {r.model} | {label} | {r.runs} | {_fmt(r.claim_rate)} | "
                    f"{_fmt(r.answer_rate)} |"
                )
    lines += [
        "",
        "The verifier estimate covers every run but inherits the verifier's errors above; "
        "the human rows are the ground truth where they exist.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="vouch-eval-real",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--labeler", required=True, help="whose labels are ground truth")
    p.add_argument("--runs", type=Path, default=Path("eval/runs"))
    p.add_argument("--labels", type=Path, default=Path("eval/labels"))
    p.add_argument("--tolerances", type=Path, default=Path("tolerance.yaml"))
    p.add_argument("--key", default="vouch-eval-key", help="receipt HMAC key for verification")
    p.add_argument("--format", choices=["md", "json"], default="md")
    args = p.parse_args(argv)

    primary = store.load(args.labels / f"{args.labeler}.jsonl")
    others = {
        path.stem: store.load(path)
        for path in sorted(args.labels.glob("*.jsonl"))
        if path.stem != args.labeler
    }
    scores = score_runs(args.runs, primary, load_tolerances(args.tolerances), args.key.encode())
    if args.format == "json":
        det = detection(scores)
        print(
            json.dumps(
                {
                    "runs": len(scores),
                    "labeled_runs": sum(s.labeled for s in scores),
                    "detection": {"tp": det.tp, "fp": det.fp, "fn": det.fn, "tn": det.tn},
                    "pairs": {s.run: s.pairs() for s in scores if s.labeled},
                },
                indent=2,
            )
        )
    else:
        print(to_markdown(scores, args.labeler, others, primary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
