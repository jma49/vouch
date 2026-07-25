# vouch

> Every number, vouched for. A verification layer for tool-using LLM agents: every tool call gets a signed receipt, and every numeric claim in the agent's answer is audited against those receipts.

**Status:** early development — MVP in progress. See [docs/design.md](docs/design.md) for the full design.

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

| Metric | Value |
|---|---|
| Hallucination detection rate (per mutation type) | _pending first eval_ |
| False-positive rate | _pending_ |
| Claim coverage | _pending_ |
| Verification latency p50 / p99 | _pending_ |
| Verdict stability across 10 runs | _pending_ |

## Quickstart

_Coming with the MVP. The intended flow:_

```bash
vouch proxy --upstream <mcp-server> --receipts ./receipts   # run the proxy
vouch verify --answer answer.txt --receipts ./receipts       # audit an answer
```

## Repository layout

| Path | Language | Role |
|---|---|---|
| `proxy/` | Go | Federating MCP proxy; receipt emission; HMAC signing |
| `verifier/` | Python | Claim extraction, fact matching, verdict assignment |
| `harness/` | Python | Fixture record/replay, mutation injection, variance reports |
| `schemas/` | YAML | Per-tool fact-extraction configs |
| `testdata/` | JSON | Cross-language canonicalization test vectors |

## Non-goals

Read-only, research-only. No order execution, no brokerage credentials, no trading capability — ever. No market-data rebuild (upstreams are dependencies, not competition). No return or Sharpe claims anywhere in this repo: this is infrastructure, not alpha.

## License

MIT
