"""The second domain (#88): text-to-SQL analytics, end to end.

Nothing in the proxy or the verifier knows this domain. The schema and
vocabulary in examples/analytics are all it supplies; this test shows
they are enough, and that the finance defaults are not.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest
from keys import EVAL_KEYS, proxy_env

from vouch_harness import analytics
from vouch_harness.agent.mcp_client import StdioMCPClient
from vouch_verifier.claims import extract_claims
from vouch_verifier.matcher import DEFAULT_TOLERANCES, match_claims
from vouch_verifier.receipts import audit_log
from vouch_verifier.verdict import Verdict
from vouch_verifier.vocabulary import FINANCE, load_vocabulary

ROOT = Path(__file__).resolve().parents[2]
PROXY = ROOT / "proxy" / "bin" / "vouch"
PACK = ROOT / "examples" / "analytics"

Q2 = (
    "SELECT region, period_end, revenue, orders, "
    "ROUND(revenue * 1.0 / orders, 2) AS avg_order_value, "
    "ROUND(returns * 100.0 / orders, 2) AS return_rate_pct "
    "FROM sales WHERE quarter = '{q}' ORDER BY region"
)


def rows(quarter: str) -> dict[str, dict[str, object]]:
    return {r["region"]: r for r in analytics.run_sql(Q2.format(q=quarter))["rows"]}


def test_run_sql_is_read_only_and_bounded() -> None:
    assert analytics.call_tool("run_sql", {"sql": "DELETE FROM sales"})["isError"]
    assert analytics.call_tool("run_sql", {"sql": "SELECT 1; DROP TABLE sales"})["isError"]
    assert analytics.call_tool("run_sql", {"sql": "SELECT * FROM nope"})["isError"]
    assert analytics.call_tool("run_sql", {"query": "SELECT 1"})["isError"]
    ok = analytics.call_tool("run_sql", {"sql": "SELECT COUNT(*) AS n FROM sales"})
    assert ok["structuredContent"]["rows"] == [{"n": 16}]
    big = analytics.run_sql("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n) "
                            "SELECT i FROM n LIMIT 500")  # fmt: skip
    assert len(big["rows"]) == analytics.MAX_ROWS and big["truncated"]


@pytest.mark.skipif(not PROXY.exists(), reason="proxy binary not built (make build)")
def test_second_domain_through_the_proxy(tmp_path: Path) -> None:
    upstream = f"{shlex.quote(sys.executable)} -m vouch_harness.analytics"
    argv = [str(PROXY), "proxy", "--upstream", upstream, "--receipts", str(tmp_path),
            "--schemas", str(PACK / "schemas"), "--session", "s-analytics"]  # fmt: skip
    env = {**proxy_env(tmp_path), "PYTHONPATH": ":".join(sys.path)}
    with StdioMCPClient(argv, env=env, stderr=tmp_path / "proxy.log") as mcp:
        for quarter in ("2026-Q1", "2026-Q2"):
            result = mcp.call_tool("run_sql", {"sql": Q2.format(q=quarter)})
            assert not result.get("isError"), result
    receipts = audit_log(tmp_path / "receipts.jsonl", EVAL_KEYS, require_sealed=True).receipts
    facts = [f for r in receipts for f in r.facts]
    assert {f.entity for f in facts} == set(analytics.REGIONS)
    assert {f.metric for f in facts} == {"revenue", "orders", "avg_order_value", "return_rate_pct"}

    q1, q2 = rows("2026-Q1"), rows("2026-Q2")
    amer, emea, apac = q2["AMER"], q2["EMEA"], q2["APAC"]
    answer = (
        f"AMER booked ${float(amer['revenue']) / 1e6:.2f} million in revenue "  # type: ignore[arg-type]
        f"on {amer['orders']:,} orders. "
        f"EMEA's average order value was ${emea['avg_order_value']}. "
        f"APAC's return rate was {float(apac['return_rate_pct']) + 1.5:.2f}%. "  # type: ignore[arg-type]
        f"LATAM revenue was ${q1['LATAM']['revenue']:,.2f}."
    )
    vocabulary = load_vocabulary(PACK / "vocabulary.yaml")
    entities = set(analytics.REGIONS)

    def verdicts(vocab: object) -> list[Verdict]:
        extraction = extract_claims(answer, entities, vocabulary=vocab)  # type: ignore[arg-type]
        matched = match_claims(extraction, receipts, DEFAULT_TOLERANCES, vocabulary=vocab)  # type: ignore[arg-type]
        return [mc.verdict for mc in matched]

    assert verdicts(vocabulary) == [
        Verdict.SUPPORTED,  # AMER revenue, displayed in millions
        Verdict.SUPPORTED,  # AMER orders
        Verdict.SUPPORTED,  # EMEA average order value
        Verdict.CONTRADICTED,  # APAC return rate, 1.5 points off
        Verdict.STALE,  # LATAM revenue: the Q1 figure, stated as current
    ]
    # With the finance vocabulary the same answer confirms nothing: its
    # metric words mean nothing there (a bare percentage even falls back
    # to finance's day change). The domain lives in the config files.
    assert Verdict.SUPPORTED not in verdicts(FINANCE)
