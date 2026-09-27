"""Synthetic text-to-SQL analytics MCP server: vouch's second domain.

vouch was built around market data, and a second domain shows whether
anything besides configuration is domain-specific (roadmap Phase 6,
issue #88). Here an agent answers business questions by writing SQL:
one tool, `run_sql`, runs a read-only SELECT over a small, seeded,
**synthetic** sales database and returns the rows. The schema and the
vocabulary that let vouch receipt and judge those answers live in
examples/analytics/, and nothing in the proxy or the verifier knows
this domain exists.

    python -m vouch_harness.analytics      # MCP over stdio

What is receipted is not the query's output. The agent writes the SQL,
so the agent could select a literal named `revenue` and have its own
number receipted as a fact (#103). Instead, every result carries a
`facts` array the server builds itself: for each (region, quarter) the
rows mention, the true figures from the sales table. The schema maps
only that array, so a fabricated number in `rows` is judged against the
real one.
"""

from __future__ import annotations

import random
import sqlite3
import sys
import time
from datetime import date
from functools import cache
from typing import Any, TextIO

from vouch_harness import stdio_server
from vouch_harness.stdio_server import make_handler, tool_error, tool_result

SERVER_NAME = "vouch-synthetic-analytics"
# The last closed quarter; results are "as of" its end.
AS_OF_DAY = date(2026, 6, 30)
MAX_ROWS = 200
# A query gets this long and this much memory per value (#103): a
# recursive CTE or zeroblob() must not hang or exhaust the server.
QUERY_SECONDS = 2.0
MAX_VALUE_BYTES = 1_000_000

REGIONS = ("AMER", "EMEA", "APAC", "LATAM")
QUARTERS = (
    ("2025-Q3", "2025-09-30"),
    ("2025-Q4", "2025-12-31"),
    ("2026-Q1", "2026-03-31"),
    ("2026-Q2", "2026-06-30"),
)
# Base quarterly revenue and orders per region. Plausible, not real.
_BASE = {"AMER": (4_200_000.0, 9_800), "EMEA": (2_900_000.0, 7_100),
         "APAC": (2_100_000.0, 6_400), "LATAM": (650_000.0, 2_300)}  # fmt: skip

SCHEMA_DOC = (
    "One table, sales(region TEXT, quarter TEXT, period_end TEXT, revenue REAL, "
    "orders INTEGER, returns INTEGER): one row per region and quarter. Regions: "
    f"{', '.join(REGIONS)}. Quarters: {', '.join(q for q, _ in QUARTERS)}. Revenue is in USD. "
    "Include region and quarter (or period_end) in the result: the server then returns the "
    "true revenue, orders, avg_order_value, and return_rate_pct for each region and quarter "
    "your rows mention, under facts."
)


@cache
def _rows() -> tuple[tuple[str, str, str, float, int, int], ...]:
    rng = random.Random(88)
    out = []
    for region in REGIONS:
        revenue, orders = _BASE[region]
        for quarter, period_end in QUARTERS:
            revenue *= 1 + rng.uniform(-0.04, 0.09)
            orders = int(orders * (1 + rng.uniform(-0.03, 0.07)))
            returns = int(orders * rng.uniform(0.02, 0.06))
            out.append((region, quarter, period_end, round(revenue, 2), orders, returns))
    return tuple(out)


def _database() -> sqlite3.Connection:
    """A fresh in-memory copy, read-only once loaded: a query cannot
    change what the next one sees."""
    db = sqlite3.connect(":memory:")
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
    db.execute(
        "CREATE TABLE sales (region TEXT, quarter TEXT, period_end TEXT, "
        "revenue REAL, orders INTEGER, returns INTEGER)"
    )
    db.executemany("INSERT INTO sales VALUES (?, ?, ?, ?, ?, ?)", _rows())
    db.commit()
    db.execute("PRAGMA query_only = ON")
    return db


def _cell(value: object) -> object:
    # Round floats as a BI tool displays them, and show bytes as hex
    # rather than failing to encode them as JSON.
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, bytes):
        return "x'" + value.hex() + "'"
    return value


_PERIOD_ENDS = dict(QUARTERS)
_QUARTERS_BY_END = {end: q for q, end in QUARTERS}


def _facts(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """The true figures for every (region, quarter) the rows mention, by
    region and period_end or quarter columns, from the server's own
    table, in a fixed order."""
    keys: set[tuple[str, str]] = set()
    for row in rows:
        region = row.get("region")
        period = row.get("period_end")
        if not isinstance(period, str) and isinstance(row.get("quarter"), str):
            period = _PERIOD_ENDS.get(str(row["quarter"]))
        if isinstance(region, str) and isinstance(period, str) and period in _QUARTERS_BY_END:
            keys.add((region, period))
    truth = {(r, end): (rev, orders, ret) for r, _, end, rev, orders, ret in _rows()}
    out = []
    for region, period in sorted(keys):
        if (region, period) not in truth:
            continue
        revenue, orders, returns = truth[(region, period)]
        out.append({
            "region": region,
            "quarter": _QUARTERS_BY_END[period],
            "period_end": period,
            "revenue": revenue,
            "orders": orders,
            "avg_order_value": round(revenue / orders, 2),
            "return_rate_pct": round(returns * 100 / orders, 2),
        })  # fmt: skip
    return out


def run_sql(sql: str) -> dict[str, Any]:
    """Run one read-only statement; raises ValueError on anything else."""
    text = sql.strip().rstrip(";")
    if not text.lower().startswith(("select", "with")):
        raise ValueError("only a single SELECT (or WITH ... SELECT) statement is allowed")
    db = _database()
    deadline = time.monotonic() + QUERY_SECONDS
    # A nonzero return aborts the statement ("interrupted").
    db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
    try:
        cursor = db.execute(text)  # sqlite3 refuses more than one statement
        columns = [d[0] for d in cursor.description or ()]
        fetched = cursor.fetchmany(MAX_ROWS + 1)
    except sqlite3.Error as e:
        if time.monotonic() > deadline:
            raise ValueError(f"query exceeded {QUERY_SECONDS:g} s") from e
        raise ValueError(f"SQL error: {e}") from e
    finally:
        db.close()
    rows = [dict(zip(columns, map(_cell, r), strict=True)) for r in fetched[:MAX_ROWS]]
    return {
        "as_of": AS_OF_DAY.isoformat(),
        "columns": columns,
        "rows": rows,
        "truncated": len(fetched) > MAX_ROWS,
        "facts": _facts(rows),
    }


TOOLS: list[dict[str, Any]] = [
    {
        "name": "run_sql",
        "description": "Run a read-only SQL query (SQLite) over the sales database. " + SCHEMA_DOC,
        "inputSchema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "one SELECT statement"}},
            "required": ["sql"],
        },
    }
]


def call_tool(name: str, arguments: object) -> dict[str, Any]:
    if name != "run_sql":
        return tool_error(f"unknown tool {name!r}; available: run_sql")
    if not isinstance(arguments, dict) or set(arguments) != {"sql"}:
        return tool_error("arguments must be an object with exactly one key, 'sql'")
    sql = arguments["sql"]
    if not isinstance(sql, str):
        return tool_error("argument 'sql' must be a string")
    try:
        return tool_result(run_sql(sql))
    except ValueError as e:
        return tool_error(str(e))


handle = make_handler(SERVER_NAME, TOOLS, call_tool)


def serve(stdin: TextIO, stdout: TextIO) -> None:
    stdio_server.serve(handle, stdin, stdout)


if __name__ == "__main__":
    serve(sys.stdin, sys.stdout)
