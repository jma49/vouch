"""The second domain (#88): text-to-SQL analytics, end to end.

Nothing in the proxy or the verifier knows this domain. The schema and
vocabulary in examples/analytics are all it supplies; this test shows
they are enough, and that the finance defaults are not.
"""

from __future__ import annotations

import io
import json
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


def test_a_query_cannot_choose_its_own_facts() -> None:
    """#103: the agent writes the SQL, so a literal named revenue must not
    become a receipted fact. Facts are the server's own figures for the
    (region, quarter) the rows mention."""
    fake = analytics.run_sql(
        "SELECT 'AMER' AS region, '2026-06-30' AS period_end, 9999999.0 AS revenue"
    )
    assert fake["rows"] == [{"region": "AMER", "period_end": "2026-06-30", "revenue": 9999999.0}]
    [fact] = fake["facts"]
    assert fact["revenue"] == rows("2026-Q2")["AMER"]["revenue"] != 9999999.0
    by_quarter = analytics.run_sql("SELECT region, quarter FROM sales WHERE quarter = '2026-Q1'")
    assert {f["region"] for f in by_quarter["facts"]} == set(analytics.REGIONS)
    assert analytics.run_sql("SELECT SUM(revenue) AS revenue FROM sales")["facts"] == []
    unknown = analytics.run_sql("SELECT 'MARS' AS region, '2026-06-30' AS period_end")
    assert unknown["facts"] == []


def test_queries_are_bounded_and_odd_values_do_not_crash() -> None:
    runaway = analytics.call_tool(
        "run_sql",
        {
            "sql": "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) "
            "SELECT count(*) FROM c"
        },
    )
    assert runaway["isError"] and "exceeded" in runaway["content"][0]["text"]
    assert analytics.call_tool("run_sql", {"sql": "SELECT zeroblob(900000000) AS b"})["isError"]
    blob = analytics.call_tool("run_sql", {"sql": "SELECT x'00ff' AS b"})
    assert blob["structuredContent"]["rows"] == [{"b": "x'00ff'"}]


def test_the_stdio_loop_survives_bad_messages() -> None:
    lines = ["not json", "[1]", '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":[1]}',
             '{"jsonrpc":"2.0","id":2,"method":"ping"}']  # fmt: skip
    out = io.StringIO()
    analytics.serve(io.StringIO("\n".join(lines) + "\n"), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r.get("error", {}).get("code") for r in replies] == [-32700, -32600, -32600, None]
    assert replies[-1] == {"jsonrpc": "2.0", "id": 2, "result": {}}


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
        fabricated = "SELECT 'EMEA' AS region, '2026-06-30' AS period_end, 1.0 AS orders"
        assert not mcp.call_tool("run_sql", {"sql": fabricated}).get("isError")
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
        f"LATAM revenue was ${q1['LATAM']['revenue']:,.2f}. "
        "EMEA orders came to 1."  # the fabricated query's number
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
        Verdict.CONTRADICTED,  # the fabricated row: facts carry the real orders
    ]
    # With the finance vocabulary the same answer confirms nothing: its
    # metric words mean nothing there (a bare percentage even falls back
    # to finance's day change). The domain lives in the config files.
    assert Verdict.SUPPORTED not in verdicts(FINANCE)
