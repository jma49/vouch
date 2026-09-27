"""Regenerate the measured sections of README.md.

Numbers in the README are outputs, not prose (AGENTS.md invariant 7):
this module recomputes them from the golden receipt log and splices
them between generated-block markers. `--check` exits non-zero when the
README is stale, which is how CI keeps hand edits out.

    python -m vouch_harness.readme README.md [--check]
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path
from typing import Any

from vouch_harness.eval import EvalResult, run_eval
from vouch_harness.gold import build_gold_set
from vouch_harness.report import overall_stats, per_mutation_stats
from vouch_verifier.judge import judge
from vouch_verifier.matcher import load_tolerances
from vouch_verifier.receipts import Receipt, load_log
from vouch_verifier.report import md_cell
from vouch_verifier.signing import load_keyring
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
    _, matched = judge(answer, receipts, tolerances)
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
            f"| `{md_cell(mc.claim.text.strip())}` | **{mc.verdict.value}** | {receipted} "
            f"| {md_cell(mc.receipt_id or '')} | {md_cell(mc.note)} |"
        )
    return "\n".join(lines) + "\n"


def _duration(us: float) -> str:
    return f"{us:.1f} µs" if us < 1000 else f"{us / 1000:.2f} ms"


def render_latency(report: dict[str, Any]) -> str:
    """The latency table, from docs/bench/latency.json (`make bench`).

    Rendering is deterministic, so CI can check the README against the
    committed file without re-running a hardware-dependent benchmark.
    """
    machine = report["machine"] or "unknown CPU"
    rows = [
        ("Agent to upstream, direct", report["direct"]),
        ("Agent to upstream, through vouch", report["proxied"]),
        ("of which: signed, fsynced log append", report["append"]),
    ]
    lines = [
        f"Measured by `make bench` on {machine} ({report['goos']}/{report['goarch']}, "
        f"{report['cpus']} CPUs, {report['go']}): {report['calls']} sequential `tools/call`s "
        "against an in-memory upstream, so the numbers are the proxy's own cost.",
        "",
        "| Path | p50 | p99 |",
        "|---|---|---|",
    ]
    for label, d in rows:
        lines.append(f"| {label} | {_duration(d['p50_us'])} | {_duration(d['p99_us'])} |")
    return "\n".join(lines) + "\n"


def regenerate(readme: str, root: Path) -> str:
    # The golden log is always signed with the committed golden test key.
    keys = load_keyring([root / "testdata" / "keys" / "golden.pub.pem"])
    receipts = load_log(root / "testdata" / "receipts_golden.jsonl", keys)
    tolerances = load_tolerances(root / "tolerance.yaml")
    result = run_eval(receipts, n=EVAL_RUNS, seed=EVAL_SEED, tolerances=tolerances)
    answer = (root / "examples" / "answer.txt").read_text(encoding="utf-8")

    readme = splice(readme, "eval-metrics", render_metrics(result, receipts))
    readme = splice(readme, "example-report", render_example(answer, receipts, tolerances))
    latency = json.loads((root / "docs" / "bench" / "latency.json").read_text(encoding="utf-8"))
    return splice(readme, "latency", render_latency(latency))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m vouch_harness.readme", description=__doc__)
    p.add_argument("readme", type=Path)
    p.add_argument("--check", action="store_true", help="fail if the README is stale")
    args = p.parse_args(argv)

    current = args.readme.read_text(encoding="utf-8")
    updated = regenerate(current, args.readme.resolve().parent)
    if args.check:
        if updated != current:
            print("readme: generated sections are stale; run `make readme`", file=sys.stderr)
            return 1
        return 0
    args.readme.write_text(updated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
