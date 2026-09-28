# Backtest example: catching look-ahead

A backtest runs an agent as if it were an earlier moment. The classic
failure is look-ahead: the agent is handed data from after that moment
and its decision quietly uses the future. vouch turns this into a
question about receipts (design section 8.4): every receipt records how
recent its data is, so `vouch-verify --as-of <moment>` can list every
call that returned later data and judge each claim only against data
available at the moment.

## The scenario

An agent is meant to decide as of the **July 22, 2026 close**. Its data
tool, like most live APIs, answers with the latest data it has. The
agent calls it twice through the proxy:

- `get_ohlcv(NVDA, limit=5)`: daily bars for July 20 to 24;
- `get_quote(NVDA)`: a quote as of July 24.

and writes [`answer.txt`](answer.txt):

> As of the July 22 close, NVDA closed at 164.03, up from 158.29 on July 20, a 3.63% gain since July 20. The stock last traded at 160.36.

Every number in it is something a tool returned. Only a check against
the backtest's moment shows that one of them is from the future.

The upstream is the repository's synthetic market server: deterministic,
no network, not real market data. The log is signed with the committed
eval key, which is public (`testdata/keys/README.md`): fine for an
example, never for real receipts.

## Run it

From the repository root, after `make build install-py`:

```bash
KEY=testdata/keys/eval.pub.pem
LOG=examples/backtest/receipts

# 1. The log is intact and ends where it ended when it was recorded.
./proxy/bin/vouch receipts verify --public-key $KEY --require-sealed \
    --expect-head "$(cat $LOG/HEAD)" $LOG/receipts.jsonl

# 2. As a live answer, it checks out: exit 0.
verifier/.venv/bin/vouch-verify --answer examples/backtest/answer.txt \
    --receipts $LOG --public-key $KEY

# 3. As a backtest at the July 22 close, it does not: exit 1.
verifier/.venv/bin/vouch-verify --answer examples/backtest/answer.txt \
    --receipts $LOG --public-key $KEY --as-of 2026-07-22
```

Step 3 reports:

- **Look-ahead**: both receipts carry data from after July 22. The bars
  include July 23 and 24; the quote is from July 24.
- `164.03` and `158.29`: `SUPPORTED`. They are the July 22 and July 20
  closes, known at the time.
- `3.63%`: `DERIVED`, recomputed from the July 20 and July 22 closes in
  one receipt.
- `160.36`: `STALE` ("look-ahead"). It matches only the July 24 quote,
  which the agent could not have had on July 22.

With `--as-of 2026-07-24` the same log and answer pass. The look-ahead
is a property of when the agent was supposed to act, not of the data.

## What this does and does not show

It shows that the data the agent received, and each number it reported,
can be checked against the backtest's clock after the fact, by anyone
holding the public key. It does not detect look-ahead that never passes
through a tool: a model recalling later prices from training shows up
only as numbers no receipt supports (`UNSUPPORTED`). It says nothing
about whether a decision was good. vouch is read-only and makes no
return claims (AGENTS.md, invariant 6).

## Recording it again

```bash
verifier/.venv/bin/python examples/backtest/record.py
```

rewrites `receipts/` and `receipts/HEAD`. Facts are identical on every
run; receipt ids, times, and signatures differ. The harness test
`harness/tests/test_backtest_example.py` checks both the committed log
and a fresh recording.
