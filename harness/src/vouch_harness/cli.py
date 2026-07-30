"""vouch-eval: run the gold-set eval against a receipt log.

    vouch-eval --receipts receipts/receipts.jsonl [--n 10] [--seed 0] \
        [--tolerances tolerance.yaml] [--format md|json]

--n 1 is refused: vouch does not print single-run scores.
"""

from __future__ import annotations

import argparse
import os
import sys

from vouch_verifier.matcher import load_tolerances
from vouch_verifier.receipts import load_log

from vouch_harness.eval import run_eval
from vouch_harness.report import to_json, to_markdown


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vouch-eval", description=__doc__)
    p.add_argument("--receipts", required=True, help="receipt log (JSONL)")
    p.add_argument("--n", type=int, default=10, help="number of runs (minimum 2)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--tolerances", help="tolerance policy YAML")
    p.add_argument("--format", choices=["md", "json"], default="md")
    args = p.parse_args(argv)

    key = os.environ.get("VOUCH_HMAC_KEY", "").encode() or None
    if key is None:
        print("vouch-eval: warning: VOUCH_HMAC_KEY not set, signatures not checked",
              file=sys.stderr)

    receipts = load_log(args.receipts, key=key)
    tolerances = load_tolerances(args.tolerances) if args.tolerances else None
    try:
        result = run_eval(receipts, n=args.n, seed=args.seed, tolerances=tolerances)
    except ValueError as e:
        print(f"vouch-eval: {e}", file=sys.stderr)
        return 2

    print(to_json(result, seed=args.seed) if args.format == "json"
          else to_markdown(result, seed=args.seed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
