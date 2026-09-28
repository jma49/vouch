"""Record the backtest example's receipt log (examples/backtest/README.md).

A backtest agent is meant to decide at the July 22, 2026 close. Its
data tool, like most live APIs, answers with the latest data it has:
bars through July 24 and a July 24 quote. The proxy receipts both
calls as they happened; `vouch-verify --as-of` then shows what the
agent could not yet have known.

    verifier/.venv/bin/python examples/backtest/record.py [--out DIR]

The upstream is the deterministic synthetic market server, so no
network is used and the facts are the same on every run; receipt ids,
wall times, and signatures differ. Signed with the committed, public
eval key (testdata/keys/README.md): fine for an example, never for
real receipts.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from vouch_harness.agent.mcp_client import StdioMCPClient
from vouch_harness.signing import EVAL_SIGNING_KEY, private_copy

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CALLS = [("get_ohlcv", {"symbol": "NVDA", "limit": 5}), ("get_quote", {"symbol": "NVDA"})]


def record(out: Path, proxy: Path = ROOT / "proxy" / "bin" / "vouch") -> str:
    """Run the two calls through the proxy into out/receipts.jsonl and
    return the log's head digest."""
    out.mkdir(parents=True, exist_ok=True)
    upstream = f"{shlex.quote(sys.executable)} -m vouch_harness.market"
    with tempfile.TemporaryDirectory() as keydir:
        key = private_copy(ROOT / EVAL_SIGNING_KEY, Path(keydir))
        argv = [str(proxy), "proxy", "--signing-key", str(key), "--upstream", f"market={upstream}",
                "--receipts", str(out), "--schemas", str(ROOT / "schemas"),
                "--session", "s-backtest"]  # fmt: skip
        env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": ":".join(sys.path)}
        with StdioMCPClient(argv, env=env) as mcp:
            for name, arguments in CALLS:
                result = mcp.call_tool(name, arguments)
                if result.get("isError"):
                    raise SystemExit(f"{name} failed: {result}")
    head = subprocess.run(
        [str(proxy), "receipts", "head", str(out / "receipts.jsonl")],
        check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip
    return head


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--out", type=Path, default=HERE / "receipts", help="receipt directory")
    args = p.parse_args()
    head = record(args.out)
    # The witness step: keep the head outside the log.
    (args.out / "HEAD").write_text(head + "\n")
    print(head)


if __name__ == "__main__":
    main()
