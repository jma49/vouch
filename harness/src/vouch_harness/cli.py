"""vouch-eval: run the gold-set eval against a receipt log.

    vouch-eval --receipts receipts/receipts.jsonl [--n 10] [--seed 0] \
        [--tolerances tolerance.yaml] [--format md|json]

--n 1 is refused: vouch does not print single-run scores.
"""

from __future__ import annotations

import argparse
import os
import sys

from vouch_harness.eval import run_eval
from vouch_harness.report import to_json, to_markdown
from vouch_verifier.matcher import load_tolerances
from vouch_verifier.receipts import ReceiptError, load_log
from vouch_verifier.signing import load_keyring


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vouch-eval", description=__doc__)
    p.add_argument("--receipts", required=True, help="receipt log (JSONL)")
    p.add_argument("--n", type=int, default=10, help="number of runs (minimum 2)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--tolerances", help="tolerance policy YAML")
    p.add_argument("--format", choices=["md", "json"], default="md")
    p.add_argument(
        "--public-key",
        action="append",
        default=[],
        help="trusted Ed25519 public key PEM (repeatable); default $VOUCH_PUBLIC_KEY",
    )
    args = p.parse_args(argv)

    key_paths = args.public_key or [
        k for k in os.environ.get("VOUCH_PUBLIC_KEY", "").split(os.pathsep) if k
    ]
    if not key_paths:
        print("vouch-eval: warning: no public key given, signatures not checked", file=sys.stderr)

    try:
        receipts = load_log(args.receipts, load_keyring(key_paths) if key_paths else None)
    except (OSError, ReceiptError, ValueError) as e:
        print(f"vouch-eval: error: {e}", file=sys.stderr)
        return 2
    tolerances = load_tolerances(args.tolerances) if args.tolerances else None
    try:
        result = run_eval(receipts, n=args.n, seed=args.seed, tolerances=tolerances)
    except ValueError as e:
        print(f"vouch-eval: {e}", file=sys.stderr)
        return 2

    print(
        to_json(result, seed=args.seed)
        if args.format == "json"
        else to_markdown(result, seed=args.seed)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
