"""vouch-verify: audit an agent answer against a receipt log.

    vouch-verify --answer answer.txt --receipts receipts/receipts.jsonl \
        [--tolerances tolerance.yaml] [--format md|json]

The signing key is read from $VOUCH_HMAC_KEY; without it, signatures
are not checked (structural and digest checks still run) and the report
says so.

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
from vouch_verifier.verdict import FAILURES


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vouch-verify", description=__doc__)
    p.add_argument("--answer", required=True, help="file containing the agent's final answer")
    p.add_argument("--receipts", required=True, help="receipt log (JSONL)")
    p.add_argument("--tolerances", help="tolerance policy YAML (default: built-in policy)")
    p.add_argument("--format", choices=["md", "json"], default="md")
    args = p.parse_args(argv)

    key = os.environ.get("VOUCH_HMAC_KEY", "").encode() or None
    if key is None:
        print(
            "vouch-verify: warning: VOUCH_HMAC_KEY not set, signatures not checked", file=sys.stderr
        )

    # Exit 2 for anything that is not a verdict: an unreadable answer, a
    # missing or tampered log, a bad policy file. Exit 1 stays reserved
    # for "the answer misreports its tools", so CI can tell the two
    # apart (issue #16).
    try:
        answer = Path(args.answer).read_text(encoding="utf-8")
        receipts = load_log(args.receipts, key=key)
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
