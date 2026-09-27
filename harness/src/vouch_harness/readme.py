"""Regenerate the measured sections of README.md.

Numbers in the README are outputs, not prose (AGENTS.md invariant 7):
this module recomputes them from the golden receipt log and splices
them between generated-block markers. `--check` exits non-zero when the
README is stale, which is how CI keeps hand edits out.

    python -m vouch_harness.readme README.md [--check]
"""

from __future__ import annotations

import argparse
import os
import random
import re
import sys
from pathlib import Path

from vouch_harness.eval import EvalResult, run_eval
from vouch_harness.gold import build_gold_set
from vouch_harness.report import overall_stats, per_mutation_stats
from vouch_verifier.claims import extract_claims
from vouch_verifier.matcher import load_tolerances, match_claims
from vouch_verifier.receipts import Receipt, load_log
from vouch_verifier.verdict import Tolerance

EVAL_RUNS = 10
EVAL_SEED = 0

_BLOCK_RE = r"(<!-- BEGIN GENERATED {name}[^>]*-->\n).*?(<!-- END GENERATED {name} -->)"


def splice(text: str, name: str, body: str) -> str:
    """Replace the contents of one generated block, keeping its markers."""
    pattern = re.compile(_BLOCK_RE.format(name=re.escape(name)), re.DOTALL)
    if not pattern.search(text):
        raise ValueError(f"README has no generated block named {name!r}")
    return pattern.sub(lambda m: m.group(1) + body + m.group(2), text, count=1)


def render_metrics(result: EvalResult, receipts: list[Receipt]) -> str:
    rng = random.Random(EVAL_SEED)
    overall = overall_stats(result, rng)
    per_mutation = per_mutation_stats(result, rng)
    cases = build_gold_set(receipts, seed=EVAL_SEED)
    clean = sum(1 for c in cases if not c.expect_bad)
    n_facts = sum(len(r.facts) for r in receipts)

    def cell(key: str) -> str:
        s = overall[key]
        return f"{s.mean:.2f} ± {s.std:.2f} | [{s.ci_lo:.2f}, {s.ci_hi:.2f}]"

    lines = [
        f"Gold set: {len(cases)} cases per run ({clean} clean, {len(cases) - clean} mutants), "
        f"derived from {n_facts} facts in {len(receipts)} receipts. "
        f"N = {len(result.runs)} runs.",
        "",
        "| Metric | Mean ± std | 95% bootstrap CI |",
        "|---|---|---|",
        f"| Mutation detection rate | {cell('detection')} |",
        f"| False-positive rate on clean answers | {cell('false_positive_rate')} |",
        f"| Claim coverage (non-`UNVERIFIABLE`) | {cell('coverage')} |",
        f"| Tier 1 (cited) share of claims | {cell('tier1_share')} |",
        "",
        "| Mutation | Recall | 95% bootstrap CI |",
        "|---|---|---|",
    ]
    for mutation, s in per_mutation.items():
        lines.append(f"| `{mutation}` | {s.mean:.2f} | [{s.ci_lo:.2f}, {s.ci_hi:.2f}] |")
    return "\n".join(lines) + "\n"


def render_example(answer: str, receipts: list[Receipt], tolerances: dict[str, Tolerance]) -> str:
    entities = {f.entity for r in receipts for f in r.facts if f.entity}
    matched = match_claims(extract_claims(answer, known_entities=entities), receipts, tolerances)
    lines = [
        "```text",
        answer.strip(),
        "```",
        "",
        "| Claim | Verdict | Receipted value | Receipt | Note |",
        "|---|---|---|---|---|",
    ]
    for mc in matched:
        receipted = "" if mc.fact is None else f"{mc.fact.value:g}"
        lines.append(
            f"| `{mc.claim.text.strip()}` | **{mc.verdict.value}** | {receipted} "
            f"| {mc.receipt_id or ''} | {mc.note} |"
        )
    return "\n".join(lines) + "\n"


def regenerate(readme: str, root: Path, key: bytes) -> str:
    receipts = load_log(root / "testdata" / "receipts_golden.jsonl", key=key)
    tolerances = load_tolerances(root / "tolerance.yaml")
    result = run_eval(receipts, n=EVAL_RUNS, seed=EVAL_SEED, tolerances=tolerances)
    answer = (root / "examples" / "answer.txt").read_text(encoding="utf-8")

    readme = splice(readme, "eval-metrics", render_metrics(result, receipts))
    return splice(readme, "example-report", render_example(answer, receipts, tolerances))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m vouch_harness.readme", description=__doc__)
    p.add_argument("readme", type=Path)
    p.add_argument("--check", action="store_true", help="fail if the README is stale")
    args = p.parse_args(argv)

    key = os.environ.get("VOUCH_HMAC_KEY", "").encode()
    if not key:
        print("readme: VOUCH_HMAC_KEY must be set to verify the golden log", file=sys.stderr)
        return 2

    current = args.readme.read_text(encoding="utf-8")
    updated = regenerate(current, args.readme.resolve().parent, key)
    if args.check:
        if updated != current:
            print("readme: generated sections are stale; run `make readme`", file=sys.stderr)
            return 1
        return 0
    args.readme.write_text(updated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
