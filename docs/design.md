# vouch — design document

> A verification layer for tool-using LLM agents: every tool call gets a signed receipt, and every numeric claim in the agent's answer is audited against those receipts.

**Positioning in one line:** We do not build market data, strategies, or agents. We answer exactly one question — *is the number the agent just said actually a number its tools returned?*

**Status:** MVP implemented. This document states the design *intent*;
where the code does not yet meet it, the section says so and links the
tracking entry in [pitfalls.md](pitfalls.md) or [roadmap.md](roadmap.md).
**License:** MIT
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
                │   server, Go)        │         │  JSONL)          │
                └──────┬──────────────┘         │                  │
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
| `proxy/` | Go | Federating MCP server; receipt emission; Ed25519 signing (DSSE); fixture record/replay |
| `verifier/` | Python | Claim extraction, fact matching, verdict assignment, markdown/JSON reports |
| `harness/` | Python | Mutation injection, gold set, repeated-run eval, variance reports |
| `schemas/` | YAML | Per-tool fact-extraction sidecar configs |

The two runtime languages communicate only through the receipt log (JSONL). Loose coupling is deliberate: either side is replaceable. (gRPC streaming verification is a possible later addition, not implemented.)

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
  "result_canonical": { ... },          // the payload facts come from, canonicalized
  "result_digest": "sha256:...",        // over result_canonical
  "payload_source": "structuredContent",// or "content/<i>/text", or "result"
  "response_canonical": { ... },        // the whole tools/call result the agent received
  "response_digest": "sha256:...",      // over response_canonical
  "facts": [ Fact, ... ],               // extracted at write time (§3.2)
  "data_asof": "2026-07-24T20:00:00Z",  // timestamp OF THE DATA, not of the call
  "wall_time": "2026-07-25T01:12:09Z",
  "logical_time": 41,                    // injected clock tick (replay mode)
  "upstream_latency_ms": 87
}
```

On disk, each receipt body is the payload of a signed DSSE envelope, one per line:

```jsonc
{
  "payload": "<base64 of the canonical receipt body>",
  "payloadType": "application/vnd.vouch.receipt+json; version=2",
  "signatures": [{ "keyid": "ed25519:8bdc9a88e7cf36a6", "sig": "<base64 Ed25519 signature>" }]
}
```

The signature covers DSSE's pre-authentication encoding of the exact payload bytes, so verifying needs only base64 and an Ed25519 library, never vouch's canonicalizer. Canonical JSON (§7) only makes the body's digests reproducible. `vouch receipts verify` and `vouch-verify --public-key` check signatures against a keyring (several keys, for rotation), and `vouch receipts cat` decodes a log for reading.

**Payload and response.** Facts are extracted from one JSON document, the payload: `structuredContent` when present, else the first text block that parses as a JSON object or array, else the whole result. But what the model reads can differ from the payload (a prose text block next to structured content, or several blocks), so the receipt also stores the whole result as received, and the signature covers both. Without that, a receipt could sign 62.3 while the model had read "12.0" (#20). The cost is size: for text-block payloads the data appears twice.

**On the signature: what it is for and what it is not for.** In a single-process setup the LLM cannot write to our storage anyway; the signature is *not* protecting against the model. Its actual value: (a) tamper-evidence when receipts cross process or machine boundaries or rest on disk; (b) third-party verification: anyone holding the public key can re-verify that a verdict report was computed against unmodified receipts, and cannot forge one; (c) replay protection via `(session_id, turn_index)` uniqueness. Ed25519 replaced the original HMAC scheme because HMAC could not give (b): whoever can verify an HMAC can also forge it (#53).

**The chain.** Every entry's signed body carries `seq` (its position in the log) and `prev_digest` (the sha256 of the previous entry's payload bytes, or a genesis value of zeros), so deleting, inserting, or reordering entries breaks the chain for everything after them, whether or not signatures are checked (#54). When a session ends cleanly the proxy appends a **checkpoint**, a signed entry of its own payload type (`application/vnd.vouch.checkpoint+json; version=1`) that records the receipt count, and prints the resulting head digest. `--require-sealed` fails a log that does not end in a checkpoint.

**What the chain cannot do.** A log cut back to an earlier checkpoint is still a valid, sealed chain. Detecting that needs the head digest from a copy kept outside the log: `--expect-head`, or, for evaluation runs, the head recorded in each run's committed `meta.json`. The threat model covers this in full (docs/threat-model.md).

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

**Tier 2 — deterministic candidate scan (fallback).** Three stages, all deterministic:

1. *Tokenize* (`tokens.py`). Mask structure that is numeric but not a claim, such as dates, clock times, fiscal periods, ordinals, period lengths (*"50-day"*), and chart timeframes. Then classify each remaining span as a point, a multiplier (*"3x"*), or a range (*"60-65"*). Parse magnitude words and suffixes, percent, and currency, and record the resolution of the last displayed digit, which the tolerance policy uses (§6.3). Only points can be judged.
2. *Resolve* (`claims.py`). Bind each point to an entity, a metric, a date, and a timeframe within its clause. Semicolons separate independent clauses, and commas and coordinating words separate phrases. The entity is the nearest preceding mention in the phrase, widening outward. A metric keyword may come from an earlier clause but never a later one. A metric must agree with the claim's unit: a percentage is never a price. A sentence that opens with a pronoun inherits the previous subject.
3. *Match* (`matcher.py`). Compare against facts in the claim's time window: the stated date, or the latest receipted day. A value that matches only outside the window is `STALE`.

Behavior on hand-written prose is pinned by an adversarial corpus (`verifier/tests/corpus/claims.yaml`) and by property-based tests.

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
price:      { abs: 0.01, display_round: true }
indicator:  { rel: 1.0e-6, display_rel: 0.005, display_round: true }
percentage: { abs: 0.05, display_round: true }   # unit: percentage points
count:      { abs: 0, display_round: true }
```

`62.3` reported as "62" is legitimate display rounding; reported as "68" is a contradiction. Without this distinction the false-positive rate makes the tool unusable. Two mechanisms carry it:

- `display_rel`: relative slack for a class, independent of how the claim is written.
- `display_round`: half a unit of the claim's *own* last displayed digit. "182" asserts a value in 181.5–182.5 and is consistent with 181.52, "181" is not, and "52.4 million" asserts 52.35M–52.45M. Digit swaps and magnitude shifts stay contradictions, because they move the value rather than its precision. The flag is opt-in per class, so an unknown class (a schema typo) gets exact comparison and no slack.

Tolerance policy is config, versioned with the eval, and printed in every report.

---

## 7. Canonicalization

Arguments, results, the full response, and the receipt body are digested in **vouch canonical JSON v1**, specified in [canonical-json.md](canonical-json.md): keys sorted by code point, compact output, number literals copied exactly as written, and input with duplicate keys, invalid UTF-8, or lone-surrogate escapes rejected instead of repaired. The Go writer and the Python reader are pinned byte for byte by `testdata/canonical_vectors.json`, and a CI job regenerates the Go-written golden log and fails on drift.

It is deliberately not RFC 8785 (JCS). JCS rewrites every number as a double, which would make a receipt record numbers the agent never saw, such as integers above 2^53, long decimals, or the trailing zero in `181.50`. vouch notarizes data other parties wrote, so fidelity wins over a standard format. Third-party verification does not depend on canonicalization, because signatures cover exact payload bytes (section 3.1). The trade-off is recorded in handoff.md ("Decisions and trade-offs").

This is the part that silently breaks cross-language (Go writes, Python verifies) if hand-rolled inconsistently.

---

## 8. Determinism and the eval harness

Directly responding to the benchmark-reliability problem: evaluators must be reproducible before their scores mean anything.

### 8.1 Fixture record/replay

Upstream responses are recorded once and content-addressed by `hash(tool + canonical_args)`, then replayed for all subsequent runs (VCR/cassette pattern). The proxy has a `--mode=record|replay|live` flag. Replay mode never touches the network.

### 8.2 Clock injection

All time reads go through a `Clock` interface. Replay runs on a logical clock derived from the fixture timeline. No `time.Now()` in business logic.

### 8.3 Repeated runs; distributions, not points

LLMs are not deterministic even at temperature 0. Every eval runs N times (default N=10) and reports mean, standard deviation, range, and a bootstrap confidence interval. **The CLI refuses to print a single-run score.** This is an opinion expressed as a product decision.

*Current limitation:* the MVP eval has no LLM in the loop. The verifier is deterministic and the synthetic gold set varies only in entity-swap targets, so the reported variance is zero by construction, not by measurement (pitfalls P-041). The machinery becomes meaningful once real agent runs feed it (roadmap Phase 2).

### 8.3a Real-agent evaluation

The mutation gold set (§9) measures detection of known error shapes on synthetic prose. The real evaluation measures what matters: real models, real answers, human ground truth.

- **Upstream:** a deterministic synthetic market-data MCP server (`vouch_harness.market`). Real tickers, generated values, every payload marked synthetic. Recalled real-world figures therefore show up as `UNSUPPORTED`.
- **Runner:** `vouch-agent` drives any OpenAI-compatible model through a fresh proxy session per (model, task, sample). It writes the answer, the signed receipt log, and the transcript. Responses are cached by request hash, so reruns are free and runs resume.
- **Labels:** `vouch-label` is a blind labeling UI (no verifier output shown), defined by `docs/labeling.md`. Agreement is reported as Cohen's kappa.
- **Report:** `vouch-eval-real` aligns verifier verdicts with labels by span (a span the verifier never extracted counts as a miss) and reports precision/recall with run-resampled CIs. It also reports each model's misreport rate across samples, where §8.3's variance machinery finally measures real nondeterminism.

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

Each mutation type gets its own precision/recall in the report. The gold set is regenerated deterministically from a receipt log (today: the Go-written `testdata/receipts_golden.jsonl`).

*Current limitation:* clean answers are synthesized from templates that are kept in sync with the verifier's own Tier 2 keyword table, and mutations are applied to spans that same extractor found. The gold set therefore measures detection of *known mutation shapes* on *extractor-friendly prose*, not verifier quality on real agent output (pitfalls P-040). A human-labeled set over real agent answers is roadmap Phase 2.

---

## 10. Metrics (the numbers this project produces)

- Detection rate and false-positive rate, **per mutation type**
- Claim coverage: fraction of numeric claims receiving a verdict other than `UNVERIFIABLE`
- Citation-protocol adherence (Tier 1 share of claims)
- Verification latency p50 / p99 (published receipt-verification baselines run under ~15 ms; that is the bar) — *not yet measured; roadmap Phase 5*
- Verdict stability across N repeated runs (agreement rate, variance)

---

## 11. MVP plan

### Weeks 1–2 — the line at which this is resume-ready

Status: all six items are implemented. The SQLite index is built in memory by the verifier from the JSONL log, not persisted by the proxy.

1. Federating MCP proxy (Go) with receipt emission → JSONL + SQLite index
2. Schema-driven fact extraction for 3–5 upstream tools
3. Citation protocol + three-verdict matching (`SUPPORTED` / `CONTRADICTED` / `UNSUPPORTED`)
4. Fixture record/replay with clock injection
5. Mutation injector + first eval report (per-mutation precision/recall, N=10 variance)
6. Tests, CI, Docker Compose, README with architecture diagram

### Later (explicitly optional)

- `DERIVED` recomputation engine
- Look-ahead detection (the `STALE` verdict itself is implemented; see §5)
- Tier 3 LLM fallback extraction
- HTML report with span highlighting
- gRPC streaming verification (verify-as-you-stream)

**Scope guard: stop at the Weeks 1–2 line first.** A deployed, tested, CI'd MVP beats a half-finished complete version.

---

## 12. Technology choices and open questions

| Decision | Choice | Rationale |
|---|---|---|
| Proxy language | Go, stdlib JSON-RPC | I/O-bound forwarding suits Go. Resolved: the federated surface (initialize, tools/list, tools/call) is small enough that a stdlib implementation costs less than an SDK dependency |
| Verifier language | Python | Numeric tooling and eval ecosystem |
| Receipt store | JSONL, SQLite index derived by the verifier | Append-only survives crashes mid-write; the index is disposable and rebuilt from the log; no server dependency |
| Signing | Ed25519 in DSSE envelopes | Third-party verification needs a public key (HMAC verifiers can forge); DSSE signs exact bytes, so verification does not depend on canonicalization; Python needs the `cryptography` package for it |
| Canonical JSON | vouch canonical JSON v1, not RFC 8785 | Number literals kept exactly (fidelity over a standard format); shared cross-language vectors; see section 7 |
| Upstream servers | Existing open-source market-data MCP servers | We deliberately do not rebuild market data; the README says so |

**Open questions:**
- ~~Go MCP SDK maturity~~ — resolved: stdlib implementation (see table above)
- Whether fixture files containing upstream market data can be redistributed in a public repo — **check each data source's ToS; default plan is to ship the recorder + schemas and let users generate fixtures with their own API keys**
- MCP protocol details for transparent federation (capability merging, notification forwarding, server-to-client requests, cancellation) — partially open; see roadmap Phase 5 and pitfalls P-021, P-022

---

## 13. Non-goals

- **No trading.** No order placement, no brokerage credentials, no execution path. Read-only forever.
- **No market data rebuild.** Upstreams are dependencies, not competition.
- **No strategy advice.** Verdicts are about provenance, not about whether the trade is good.
- **No return/Sharpe claims anywhere in this repo.** This is infrastructure, not alpha.
- **No general-purpose hallucination detection.** Numeric, tool-grounded claims only. `UNVERIFIABLE` is a feature.

---

## 14. Relation to prior art

- **Receipt-based verification** (signed tool receipts, cross-referencing agent claims): we adopt the core mechanism and are explicit about the threat model differences in a single-process deployment (§3.1).
- **Benchmark-reliability audits** (double-digit score swings across identical re-runs): motivates §8 — repetition, variance reporting, and the refusal to print single-run scores.
- **Multi-agent trading frameworks / market-data MCP servers**: adjacent but orthogonal. They produce answers; we audit them. Crowded spaces we intentionally do not enter.
