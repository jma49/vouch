# vouch

> Every number, vouched for. A verification layer for tool-using LLM agents: every tool call gets a signed receipt, and every numeric claim in the agent's answer is audited against those receipts.

**Status:** MVP complete and tested end to end (proxy → receipts → verifier → eval). See [docs/design.md](docs/design.md) for the full design.

---

## The problem

Tool-calling agents fail in a specific, dangerous way in numeric domains: the tool returns `RSI = 62.3`, and the agent reports `RSI = 68`. The text reads perfectly fluent. Generic hallucination evals cannot catch this class of error, because the answer is *plausible* — it is just *not what the tool said*.

vouch answers exactly one question: **is the number the agent just said actually a number its tools returned?**

## How it works

```
        Agent
          │  MCP
          ▼
   ┌──────────────┐        ┌──────────────┐
   │ vouch proxy  │ ─────▶ │ receipt log  │
   │ (Go)         │  sign  │ (JSONL)      │
   └──────┬───────┘        └──────┬───────┘
          ▼                       ▼
   upstream MCP servers    ┌──────────────┐
   (market data, etc.)     │ verifier     │◀── agent's final answer
                           │ (Python)     │
                           └──────┬───────┘
                                  ▼
                           verdict report
```

The proxy is a federating MCP server: the agent connects to it instead of the upstreams. Every tool call is forwarded unchanged and recorded as an HMAC-signed receipt with extracted facts. The verifier then matches every numeric claim in the agent's answer against those receipts and assigns one of six verdicts: `SUPPORTED`, `CONTRADICTED`, `UNSUPPORTED`, `STALE`, `DERIVED`, or `UNVERIFIABLE`.

No cooperation from the agent or the upstream is required — the proxy sits on the only path where ground truth exists.

## What makes this different

- **Six verdicts, not two.** "Fabricated from memory" (`UNSUPPORTED`) and "contradicts the tool" (`CONTRADICTED`) are different failures with different fixes.
- **Rounding is not hallucination.** Two-level tolerance policy: `62.3` reported as "62" is display rounding; "68" is a contradiction.
- **Distributions, not points.** Every eval runs N times and reports mean, variance, and confidence intervals. The CLI refuses to print a single-run score.
- **A gold set, not a demo.** A mutation injector generates known-bad traces (digit swaps, entity swaps, fabricated citations), so detection precision/recall are measured, not asserted.

## Metrics

Produced by `vouch-eval` over the gold set in `testdata/receipts_golden.jsonl`, N=10 runs. Reproduce with `make eval`.

| Metric | Value |
|---|---|
| Mutation detection rate | 0.89 ± 0.00 (95% CI [0.89, 0.89]) |
| False-positive rate on clean answers | 0.00 ± 0.00 |
| Claim coverage (non-`UNVERIFIABLE`) | 1.00 ± 0.00 |
| Tier 1 (cited) share of claims | 0.46 ± 0.00 |
| Verdict stability across 10 runs | 1.00 |

Per-mutation recall: `digit_swap`, `magnitude_shift`, `entity_swap`, `sign_flip`, `fabricated_citation` all 1.00; `false_absence` 0.00. The last one is a known MVP gap — an absent claim produces no verdict to flag — and it stays in the gold set precisely so the table reports it.

## Quickstart

```bash
make build           # build the Go proxy
make install-py      # install verifier + harness into a venv

export VOUCH_HMAC_KEY="$(openssl rand -hex 32)"

# 1. Put the proxy in front of your upstream MCP server(s).
#    Point your agent at this process instead of the upstream.
./proxy/bin/vouch proxy \
    --upstream "uvx some-market-data-mcp" \
    --receipts ./receipts --schemas ./schemas

# 2. Audit the agent's final answer against the receipts it generated.
vouch-verify --answer answer.txt \
    --receipts ./receipts/receipts.jsonl --tolerances tolerance.yaml

# 3. Measure the verifier itself against machine-generated known-bad answers.
vouch-eval --receipts ./receipts/receipts.jsonl --n 10
```

Record once, then replay deterministically (no network, logical clock):

```bash
./proxy/bin/vouch proxy --mode=record --upstream "..." --fixtures ./fixtures ...
./proxy/bin/vouch proxy --mode=replay --fixtures ./fixtures ...
```

## Repository layout

| Path | Language | Role |
|---|---|---|
| `proxy/` | Go | Federating MCP proxy; receipt emission; HMAC signing; fixture record/replay |
| `verifier/` | Python | Claim extraction, fact matching, verdict assignment, `vouch-verify` |
| `harness/` | Python | Mutation injection, gold set, repeated-run eval, `vouch-eval` |
| `schemas/` | YAML | Per-tool fact-extraction configs |
| `tolerance.yaml` | YAML | Tolerance policy, versioned with the eval |
| `testdata/` | JSON | Cross-language canonicalization vectors and the golden receipt log |

## Non-goals

Read-only, research-only. No order execution, no brokerage credentials, no trading capability — ever. No market-data rebuild (upstreams are dependencies, not competition). No return or Sharpe claims anywhere in this repo: this is infrastructure, not alpha.

## License

MIT
