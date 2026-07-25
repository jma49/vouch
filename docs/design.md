# vouch — design document

> A verification layer for tool-using LLM agents: every tool call gets a signed receipt, and every numeric claim in the agent's answer is audited against those receipts.

**Positioning in one line:** We do not build market data, strategies, or agents. We answer exactly one question — *is the number the agent just said actually a number its tools returned?*

**Status:** Design / MVP in progress
**License:** MIT (planned)
**Scope guard:** Read-only, research-only. No order execution, no trading capability, ever.

---

## 1. Problem

Tool-calling agents fail in a specific, dangerous way in numeric domains (finance, medicine, engineering): the tool returns `RSI = 62.3`, and the agent reports `RSI = 68`. The text reads perfectly fluent. Generic hallucination evals (LLM-as-judge, semantic similarity) cannot catch this class of error, because the answer is *plausible* — it is just *not what the tool said*.

Two further problems compound this:

1. **Benchmarks themselves are unreliable.** Recent audits of tool-calling benchmarks show the same configuration re-evaluated repeatedly can swing by double-digit percentage points — evaluator artifacts, not agent capability. Any verification tool that prints a single-run score is part of the problem.
2. **Ground truth is unavailable after the fact.** Once the agent has answered, the upstream data has moved. If you did not capture what the tools returned *at call time*, you cannot audit the answer at all.

vouch addresses both: capture ground truth at the only moment it exists (the tool call), and evaluate with repetition and variance reporting built in.

---

## 2. Architecture

```
                ┌─────────────┐
                │    Agent    │
                └──────┬──────┘
                       │ MCP (tool calls)
                       ▼
                ┌─────────────────────┐         ┌──────────────────┐
                │  Verification proxy  │ ──────▶ │   Receipt log    │
                │  (federating MCP     │  write  │ (append-only     │
                │   server, Go)        │         │  JSONL + SQLite  │
                └──────┬──────────────┘         │  index)          │
                       │ forwards to            └────────┬─────────┘
                       ▼                                  │ read
                ┌─────────────────────┐                   │
                │ Upstream MCP servers │                   ▼
                │ (market data,        │         ┌──────────────────┐
                │  indicators, etc.)   │         │    Verifier      │
                └─────────────────────┘         │  (Python)        │
                                                 │  claims vs       │
                Agent's final answer ──────────▶ │  receipts        │
                                                 └────────┬─────────┘
                                                          ▼
                                                 ┌──────────────────┐
                                                 │  Verdict report  │
                                                 └──────────────────┘
```

The proxy is itself an MCP server that **federates** one or more upstream MCP servers. The agent connects to the proxy instead of the upstreams; the proxy forwards every call, records a signed receipt of the request/response pair, and returns the result unchanged. The verifier is a separate offline component that consumes (final answer, receipt log) and produces a verdict report.

Key property: **the proxy sits on the only path where ground truth exists.** No cooperation from the agent or the upstream is required.

### Components

| Component | Language | Role |
|---|---|---|
| `proxy/` | Go | Federating MCP server; receipt emission; HMAC signing |
| `verifier/` | Python | Claim extraction, fact matching, verdict assignment |
| `harness/` | Python | Fixture record/replay, mutation injection, repeated-run eval, variance reports |
| `schemas/` | YAML | Per-tool fact-extraction sidecar configs |
| `report/` | Python | HTML/markdown verdict report rendering |

The two runtime languages communicate only through the receipt log (JSONL) and, optionally, gRPC for streaming verification. Loose coupling is deliberate: either side is replaceable.

---

## 3. Core data structures

### 3.1 Receipt

One receipt per tool call, appended to the log. Immutable after write.

```jsonc
{
  "receipt_id": "a1b2c3...",            // uuid
  "session_id": "s-...",
  "turn_index": 3,
  "tool_name": "get_indicators",
  "args_canonical": { ... },            // canonicalized JSON (see §7)
  "result_canonical": { ... },          // canonicalized JSON
  "result_digest": "sha256:...",        // over result_canonical
  "facts": [ Fact, ... ],               // extracted at write time (§3.2)
  "data_asof": "2026-07-24T20:00:00Z",  // timestamp OF THE DATA, not of the call
  "wall_time": "2026-07-25T01:12:09Z",
  "logical_time": 41,                    // injected clock tick (replay mode)
  "upstream_latency_ms": 87,
  "sig": "hmac-sha256:..."              // over canonical(receipt minus sig)
}
```

**On the HMAC signature — what it is for and what it is not for.** In a single-process setup the LLM cannot write to our storage anyway; the signature is *not* protecting against the model. Its actual value: (a) tamper-evidence when receipts cross process/machine boundaries or rest on disk, (b) making eval results reproducible and auditable — a third party can re-verify that a verdict report was computed against unmodified receipts, (c) replay protection via `(session_id, turn_index)` uniqueness. We keep it, and we are honest about its threat model.

### 3.2 Fact

A verifiable atom extracted from a tool result, at receipt-write time, via schema-driven extraction (§4).

```jsonc
{
  "entity": "NVDA",
  "metric": "rsi_14",
  "value": 62.3,
  "unit": null,                  // null | "USD" | "pct" | ...
  "as_of": "2026-07-24T20:00:00Z",
  "timeframe": "1d",
  "json_ptr": "/indicators/rsi_14",   // provenance inside result_canonical
  "tol_class": "indicator"            // selects tolerance policy (§6)
}
```

### 3.3 Claim

An assertion extracted from the agent's final answer. Structurally aligned with Fact, plus a `span` (character offsets in the answer text, for report highlighting) and a `citation` (receipt reference, if the agent followed the citation protocol).

Verification is then a **Claim → Fact matching problem**: for each claim, find candidate facts by (entity, metric, timeframe), compare values under the tolerance policy, and assign a verdict.

---

## 4. Fact extraction: schema-driven, not regex

Each upstream tool gets a sidecar YAML mapping. Extraction is deterministic and unit-testable; adding a tool means adding config, not code.

```yaml
# schemas/get_indicators.yaml
tool: get_indicators
entity_ptr: /symbol
asof_ptr: /as_of
facts:
  - ptr: /rsi_14
    metric: rsi_14
    tol_class: indicator
  - ptr: /macd/histogram
    metric: macd_hist
    tol_class: indicator
  - ptr: /close
    metric: close_price
    unit: USD
    tol_class: price
```

Regex-based extraction breaks on nested structures and arrays. JSON-pointer-based extraction does not. Array support: a `ptr` may address an array with an `each:` sub-mapping (e.g., one fact per bar in an OHLCV series).

---

## 5. Claim extraction: three tiers, with measured coverage

**Tier 1 — citation protocol (preferred).** The agent's system prompt requires structured citations on numeric claims:

```
NVDA's RSI(14) is 62.3 [[r:a1b2#/indicators/rsi_14]]
```

Cited claims are trivially and deterministically matchable. Highest reliability.

**Tier 2 — deterministic candidate scan (fallback).** Regex over the answer for numeric spans + nearby entity/metric keywords, producing *candidate* claims.

**Tier 3 — LLM structured extraction (last resort).** Candidates that Tier 2 cannot resolve are passed to a small model with a strict JSON-schema output contract, converting spans into Claims.

**Every eval report states the tier mix** — e.g., "Tier 1 covered 87% of numeric claims." Citation-protocol adherence is itself a measured property of the agent under test, and a headline metric of this project.

---

## 6. Verdict taxonomy and tolerance policy

### 6.1 Six verdicts, not two

| Verdict | Meaning |
|---|---|
| `SUPPORTED` | Matching fact exists; value within tolerance |
| `CONTRADICTED` | Matching fact exists; value outside tolerance — **the deadly class** |
| `UNSUPPORTED` | No receipt covers this claim (fabricated from parametric memory) |
| `STALE` | Value matches a fact whose `as_of` falls outside the claim's implied time window |
| `DERIVED` | Not directly in any receipt, but recomputable from receipts via whitelisted ops |
| `UNVERIFIABLE` | Subjective / non-numeric / out of scope — explicitly not judged |

### 6.2 DERIVED: whitelisted recomputation only

Allowed operations: percentage change, difference, ratio, and min/max/count over a receipted series. Example: the agent says "up 3.2% on the day" — the verifier recomputes from the two receipted prices and compares. **Anything outside the whitelist is `UNSUPPORTED`. The verifier never guesses.**

### 6.3 Tolerance classes: rounding is not hallucination

```yaml
# tolerance.yaml
price:      { abs: 0.01 }
indicator:  { rel: 1.0e-6, display_rel: 0.005 }   # display-layer rounding allowed
percentage: { abs: 0.05 }                          # unit: percentage points
count:      { abs: 0 }
```

`62.3` reported as "62" is legitimate display rounding (`display_rel`); reported as "68" is a contradiction. Without this two-level distinction the false-positive rate makes the tool unusable. Tolerance policy is config, versioned with the eval, and printed in every report.

---

## 7. Canonicalization

Both `args_canonical` and `result_canonical` — and the digest and signature over them — depend on a stable canonical JSON form:

- Object keys sorted lexicographically (recursive)
- Numbers serialized in a fixed format: shortest round-trip representation; `-0` normalized to `0`; no exponent form below 1e21
- Strings NFC-normalized; no escaped forward slashes
- No insignificant whitespace
- UTC ISO-8601 timestamps with explicit `Z`

This is the part that silently breaks cross-language (Go writes, Python verifies) if hand-rolled inconsistently. Implementation follows RFC 8785 (JCS) semantics; both sides are tested against a shared vector file (`testdata/canonical_vectors.json`).

---

## 8. Determinism and the eval harness

Directly responding to the benchmark-reliability problem: evaluators must be reproducible before their scores mean anything.

### 8.1 Fixture record/replay

Upstream responses are recorded once and content-addressed by `hash(tool + canonical_args)`, then replayed for all subsequent runs (VCR/cassette pattern). The proxy has a `--mode=record|replay|live` flag. Replay mode never touches the network.

### 8.2 Clock injection

All time reads go through a `Clock` interface. Replay runs on a logical clock derived from the fixture timeline. No `time.Now()` in business logic.

### 8.3 Repeated runs; distributions, not points

LLMs are not deterministic even at temperature 0. Every eval runs N times (default N=10) and reports mean, standard deviation, range, and a bootstrap confidence interval. **The CLI refuses to print a single-run score.** This is an opinion expressed as a product decision.

### 8.4 Look-ahead detection (backtest integration)

Because every receipt carries `data_asof` and replay carries a logical clock, look-ahead bias detection is free: if a receipt's `data_asof` is later than the simulated current time, flag a look-ahead violation. **Backtest correctness becomes a receipt-verification problem** — an angle we have not seen elsewhere.

---

## 9. Mutation injector and gold set

A verifier without a gold set is a demo, not a measurement. The injector takes a passing trace and machine-generates known-bad variants:

| Mutation | Example |
|---|---|
| Digit swap | 62.3 → 26.3 |
| Magnitude shift | 62.3 → 623 |
| Entity swap | NVDA's value attributed to AMD |
| Timeframe swap | 1d indicator reported as 1h |
| Sign flip | +3.2% → -3.2% |
| Fabricated citation | cites a receipt_id that does not exist |
| False absence | "no data available" when a receipt exists |

Each mutation type gets its own precision/recall in the report. The gold set ships with the repo and is regenerated deterministically from fixtures.

---

## 10. Metrics (the numbers this project produces)

- Detection rate and false-positive rate, **per mutation type**
- Claim coverage: fraction of numeric claims receiving a verdict other than `UNVERIFIABLE`
- Citation-protocol adherence (Tier 1 share of claims)
- Verification latency p50 / p99 (published receipt-verification baselines run under ~15 ms; that is the bar)
- Verdict stability across N repeated runs (agreement rate, variance)

---

## 11. MVP plan

### Weeks 1–2 — the line at which this is resume-ready

1. Federating MCP proxy (Go) with receipt emission → JSONL + SQLite index
2. Schema-driven fact extraction for 3–5 upstream tools
3. Citation protocol + three-verdict matching (`SUPPORTED` / `CONTRADICTED` / `UNSUPPORTED`)
4. Fixture record/replay with clock injection
5. Mutation injector + first eval report (per-mutation precision/recall, N=10 variance)
6. Tests, CI, Docker Compose, README with architecture diagram

### Later (explicitly optional)

- `DERIVED` recomputation engine
- `STALE` verdicts and look-ahead detection
- Tier 3 LLM fallback extraction
- HTML report with span highlighting
- gRPC streaming verification (verify-as-you-stream)

**Scope guard: stop at the Weeks 1–2 line first.** A deployed, tested, CI'd MVP beats a half-finished complete version.

---

## 12. Technology choices and open questions

| Decision | Choice | Rationale |
|---|---|---|
| Proxy language | Go (pending SDK check) | I/O-bound forwarding suits Go; **verify Go MCP SDK maturity before committing** — fall back to TypeScript if the SDK costs more than two days |
| Verifier language | Python | Numeric tooling and eval ecosystem |
| Receipt store | JSONL + SQLite index | Append-only survives crashes mid-write; SQLite for lookup; no server dependency |
| Signing | HMAC-SHA256 | Symmetric is sufficient for the stated threat model (§3.1); asymmetric adds ops burden with no benefit here |
| Canonical JSON | RFC 8785 (JCS) | Cross-language stability; shared test vectors |
| Upstream servers | Existing open-source market-data MCP servers | We deliberately do not rebuild market data; the README says so |

**Open questions to resolve during week 1:**
- Go MCP SDK maturity (blocking for language choice)
- Whether fixture files containing upstream market data can be redistributed in a public repo — **check each data source's ToS; default plan is to ship the recorder + schemas and let users generate fixtures with their own API keys**
- MCP protocol details for transparent federation (capability merging, notification forwarding)

---

## 13. Non-goals

- **No trading.** No order placement, no brokerage credentials, no execution path. Read-only forever.
- **No market data rebuild.** Upstreams are dependencies, not competition.
- **No strategy advice.** Verdicts are about provenance, not about whether the trade is good.
- **No return/Sharpe claims anywhere in this repo.** This is infrastructure, not alpha.
- **No general-purpose hallucination detection.** Numeric, tool-grounded claims only. `UNVERIFIABLE` is a feature.

---

## 14. Relation to prior art

- **Receipt-based verification** (HMAC-signed tool receipts, cross-referencing agent claims): we adopt the core mechanism and are explicit about the threat model differences in a single-process deployment (§3.1).
- **Benchmark-reliability audits** (double-digit score swings across identical re-runs): motivates §8 — repetition, variance reporting, and the refusal to print single-run scores.
- **Multi-agent trading frameworks / market-data MCP servers**: adjacent but orthogonal. They produce answers; we audit them. Crowded spaces we intentionally do not enter.
