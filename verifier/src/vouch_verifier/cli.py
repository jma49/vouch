"""vouch-verify: audit an agent answer against a receipt log.

    vouch-verify --answer answer.txt --receipts receipts/receipts.jsonl \
        --public-key vouch.pub.pem [--public-key ...] \
        [--require-sealed] [--expect-head sha256:...] \
        [--tolerances tolerance.yaml] [--format md|json] \
        [--as-of 2026-07-20T15:00:00Z]

Trusted Ed25519 public keys come from --public-key (repeatable, for key
rotation) or, when none is given, from $VOUCH_PUBLIC_KEY (paths
separated by the OS path separator). Without any, signatures are not
checked (structural, digest, and chain checks still run) and a warning
says so. The log's hash chain is always checked. Truncation of its tail
is detected only with --require-sealed (the log must end in the
checkpoint the proxy writes when a session ends cleanly) or
--expect-head (a head digest kept outside the log).

--as-of verifies a backtest (design section 8.4): the agent was meant to
act at that moment, so any receipt with later data is a look-ahead
violation, and claims are judged only against data available then (a
claim that matches only later data is STALE). A bare date means the end
of that day.

Exit codes: 0 when no claim fails; 1 when any claim is CONTRADICTED,
UNSUPPORTED, or STALE, or, with --as-of, any receipt holds later data;
2 for usage errors and for input that cannot be
verified at all (unreadable files, a malformed or tampered receipt log,
a bad tolerance policy).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from vouch_verifier.claims import extract_claims
from vouch_verifier.lookahead import find_lookahead, parse_moment
from vouch_verifier.matcher import DEFAULT_TOLERANCES, load_tolerances, match_claims
from vouch_verifier.receipts import ReceiptError, audit_log
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
    p.add_argument(
        "--require-sealed",
        action="store_true",
        help="fail unless the log ends in a checkpoint",
    )
    p.add_argument("--expect-head", help="fail unless the chain head is this digest")
    p.add_argument(
        "--as-of",
        help="a backtest's simulated moment (ISO 8601): flag data from after it",
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
        receipts = audit_log(
            args.receipts,
            keys,
            require_sealed=args.require_sealed,
            expect_head=args.expect_head,
        ).receipts
        tolerances = load_tolerances(args.tolerances) if args.tolerances else DEFAULT_TOLERANCES
        as_of = parse_moment(args.as_of) if args.as_of else None
    except (OSError, UnicodeDecodeError, ReceiptError, ValueError) as e:
        print(f"vouch-verify: error: {e}", file=sys.stderr)
        return 2

    entities = {f.entity for r in receipts for f in r.facts if f.entity}
    extraction = extract_claims(answer, known_entities=entities)
    matched = match_claims(extraction, receipts, tolerances, as_of=as_of)
    lookahead = find_lookahead(receipts, as_of) if as_of is not None else []
    report = build_report(
        extraction, matched, tolerances, as_of=args.as_of if as_of else None, lookahead=lookahead
    )

    print(to_json(report) if args.format == "json" else to_markdown(report))

    bad = sum(1 for mc in matched if mc.verdict in FAILURES)
    return 1 if bad or lookahead else 0


if __name__ == "__main__":
    sys.exit(main())
