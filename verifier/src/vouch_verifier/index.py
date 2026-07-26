"""SQLite lookup index over the receipt log (design section 12).

The JSONL log is the source of truth; this index is derived and
disposable — rebuild it from the log at any time. It exists so claim
matching can look up candidate facts by (entity, metric, timeframe)
without scanning the log.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from vouch_verifier.receipts import Fact, Receipt

_SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id  TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    turn_index  INTEGER NOT NULL,
    tool_name   TEXT NOT NULL,
    data_asof   TEXT,
    wall_time   TEXT NOT NULL,
    UNIQUE (session_id, turn_index)
);
CREATE TABLE IF NOT EXISTS facts (
    receipt_id  TEXT NOT NULL REFERENCES receipts(receipt_id),
    entity      TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value       REAL NOT NULL,
    unit        TEXT,
    as_of       TEXT,
    timeframe   TEXT,
    json_ptr    TEXT NOT NULL,
    tol_class   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS facts_lookup ON facts (entity, metric, timeframe);
"""


def build_index(receipts: list[Receipt], db_path: str | Path = ":memory:") -> sqlite3.Connection:
    """Build a fresh index from receipts; existing contents are replaced."""
    conn = sqlite3.connect(db_path)
    conn.executescript("DROP TABLE IF EXISTS facts; DROP TABLE IF EXISTS receipts;")
    conn.executescript(_SCHEMA)
    with conn:
        for r in receipts:
            conn.execute(
                "INSERT INTO receipts VALUES (?, ?, ?, ?, ?, ?)",
                (r.receipt_id, r.session_id, r.turn_index, r.tool_name, r.data_asof, r.wall_time),
            )
            conn.executemany(
                "INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (r.receipt_id, f.entity, f.metric, f.value, f.unit,
                     f.as_of, f.timeframe, f.json_ptr, f.tol_class)
                    for f in r.facts
                ],
            )
    return conn


def facts_for(
    conn: sqlite3.Connection,
    entity: str,
    metric: str,
    timeframe: str | None = None,
) -> list[tuple[str, Fact]]:
    """Return (receipt_id, Fact) candidates for a claim.

    A timeframe of None matches facts of any timeframe — an uncited
    claim rarely pins one down; the matcher applies stricter rules when
    it can.
    """
    q = "SELECT receipt_id, entity, metric, value, unit, as_of, timeframe, json_ptr, tol_class FROM facts WHERE entity = ? AND metric = ?"
    params: list[object] = [entity, metric]
    if timeframe is not None:
        q += " AND timeframe = ?"
        params.append(timeframe)
    out = []
    for row in conn.execute(q, params):
        out.append(
            (row[0], Fact(entity=row[1], metric=row[2], value=row[3], unit=row[4],
                          as_of=row[5], timeframe=row[6], json_ptr=row[7], tol_class=row[8]))
        )
    return out
