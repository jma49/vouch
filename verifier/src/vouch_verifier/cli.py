"""vouch-verify: audit an agent answer against a receipt log.

    vouch-verify --answer answer.txt --receipts receipts/receipts.jsonl \
        --public-key vouch.pub.pem [--public-key ...] \
        [--tolerances tolerance.yaml] [--format md|json]

Trusted Ed25519 public keys come from --public-key (repeatable, for key
rotation) or, when none is given, from $VOUCH_PUBLIC_KEY (paths
separated by the OS path separator). Without any, signatures are not
checked (structural and digest checks still run) and a warning says so.

Exit codes: 0 when no claim fails; 1 when any claim is CONTRADICTED,
UNSUPPORTED, or STALE; 2 for usage errors and for input that cannot be
verified at all (unreadable files, a malformed or tampered receipt log,
a bad tolerance policy).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from vouch_verifier.claims import extract_claims
from vouch_verifier.matcher import DEFAULT_TOLERANCES, load_tolerances, match_claims
from vouch_verifier.receipts import ReceiptError, load_log
from vouch_verifier.report import build_report, to_json, to_markdown
from vouch_verifier.signing import load_keyring
from vouch_verifier.verdict import FAILURES


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vouch-verify", description=__doc__)
    p.add_argument("--answer", required=True, help="file containing the agent's final answer")
    p.add_argument("--receipts", required=True, help="receipt log (JSONL)")
    p.add_argument("--tolerances", help="tolerance policy YAML (default: built-in policy)")
    p.add_argument("--format", choices=["md", "json"], default="md")
    p.add_argument(
        "--public-key",
        action="append",
        default=[],
        help="trusted Ed25519 public key PEM (repeatable); default $VOUCH_PUBLIC_KEY",
    )
    args = p.parse_args(argv)

    key_paths = args.public_key or [
        p for p in os.environ.get("VOUCH_PUBLIC_KEY", "").split(os.pathsep) if p
    ]
    if not key_paths:
        print(
            "vouch-verify: warning: no public key given (--public-key or VOUCH_PUBLIC_KEY), "
            "signatures not checked",
            file=sys.stderr,
        )

    # Exit 2 for anything that is not a verdict: an unreadable answer, a
    # missing or tampered log, a bad policy file. Exit 1 stays reserved
    # for "the answer misreports its tools", so CI can tell the two
    # apart (issue #16).
    try:
        answer = Path(args.answer).read_text(encoding="utf-8")
        keys = load_keyring(key_paths) if key_paths else None
        receipts = load_log(args.receipts, keys)
        tolerances = load_tolerances(args.tolerances) if args.tolerances else DEFAULT_TOLERANCES
    except (OSError, UnicodeDecodeError, ReceiptError, ValueError) as e:
        print(f"vouch-verify: error: {e}", file=sys.stderr)
        return 2

    entities = {f.entity for r in receipts for f in r.facts if f.entity}
    extraction = extract_claims(answer, known_entities=entities)
    matched = match_claims(extraction, receipts, tolerances)
    report = build_report(extraction, matched, tolerances)

    print(to_json(report) if args.format == "json" else to_markdown(report))

    bad = sum(1 for mc in matched if mc.verdict in FAILURES)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
