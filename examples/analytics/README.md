# A second domain: text-to-SQL analytics

vouch was built around market data. This directory is everything it
takes to point it at a different domain, an agent answering business
questions by writing SQL, without changing the proxy or the verifier:

- `schemas/run_sql.yaml`: which columns of a query result are facts,
  whose they are (each row's `region`), and when (its `period_end`);
- `vocabulary.yaml`: how analytics prose names those metrics, for
  `vouch-verify --vocabulary`.

The upstream is `python -m vouch_harness.analytics`, a synthetic sales
database behind one read-only `run_sql` tool.

```bash
./proxy/bin/vouch proxy --signing-key ~/.vouch/vouch.pem \
    --upstream "python3 -m vouch_harness.analytics" \
    --schemas examples/analytics/schemas --receipts ./receipts
# ... the agent asks: SELECT region, revenue, orders FROM sales WHERE quarter = '2026-Q2'
vouch-verify --answer answer.txt --receipts ./receipts/receipts.jsonl \
    --public-key ~/.vouch/vouch.pub.pem --vocabulary examples/analytics/vocabulary.yaml
```

What carried over unchanged: receipts, signatures, the chain, canonical
JSON, citations, tolerance classes and display rounding ("$5.16 million"
against 5,159,288.66), STALE time windows, and look-ahead. What the
domain had to supply is the two files above. The run that proved it is
`harness/tests/test_analytics.py`.

Known limits in this domain: a quarter named in prose ("in Q1") is not
read as a date, so an undated claim is judged against the latest
quarter and a correct statement about an earlier one comes out `STALE`;
and percentage changes ("grew 4%") are not recomputed: the vocabulary
names no `series`, and quarters are not dates `DERIVED` could anchor on.
