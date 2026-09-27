# Handoff

Snapshot of where work stands, for the next session. Overwrite
"Current state" and "Next steps" each session; append to "Session log".

**Last updated:** 2026-09-27

## Current state

- **Phases 0, 1, 3, 4, 5 complete. Phase 2: everything buildable is
  done; runs and labels are pending** (they need the maintainer's model
  budget and a human labeler; see memory: no paid model calls without
  approval). Phase 6 (optional) not started.
- **Phase 4 (canonical JSON), 2026-09-27:** differential fuzzing Go vs
  Python (`vouch canon --lines`, `FuzzCanonicalize`, corpus committed,
  `test_differential.py`); found #61 (stray `}` accepted) and #62 (no
  nesting limit) -> canonical JSON v2, receipt payload `version=4`,
  checkpoint `version=2`. Fixture keys compare numbers by value (#64).
- **Phase 5 (proxy), 2026-09-27:** session resume via log-assigned
  turns (#69); shell-style `--upstream` splitting (#70); concurrent
  requests with cancellation and progress (#67); server-to-client
  requests, `tools/list_changed`, version negotiation (#68);
  integration tests against the official reference server, which found
  #74 (`"params":null`); `make bench` and a README latency table (#77);
  Streamable HTTP for upstreams and `--listen` (#79).
- **Phase 2 citation channel:** `vouch proxy --cite` and
  `vouch-agent --cite`, adherence in the real-eval report (#81).
- Tests: verifier 338 (+1 documented xfail), harness 119, Go under
  `-race`, integration (stdio and HTTP) in its own CI job. Corpus 166.
- Process lessons: `docs/pitfalls.md` P-006, P-007, and AGENTS.md.

## Next steps

1. Phase 2 data, when the maintainer can run a model:
   `make agent MODEL=gemini-flash ARGS="--samples 3"` (dry-run first;
   add `--cite` for the citation condition), then label with
   `vouch-label serve`. Never run it without approval.
2. Phase 6 (optional), no model calls needed: look-ahead detection
   (design section 8.4), HTML report, a second domain schema pack,
   `DERIVED` recomputation. Tier 3 LLM extraction needs model calls.
3. Proxy follow-ups, only when needed: group commit for appends under
   concurrent load; authentication before exposing `--listen` beyond
   loopback; resources and prompts federation.

## Open questions for the maintainer

- Phase 2: when can a Gemini run happen, and is committing its outputs
  under `eval/runs/` acceptable?

## Decisions and trade-offs

Every decision that gave something up. Format: what was chosen, the
alternative, why, the cost we accepted, and when to revisit. Add an
entry whenever a choice closes off an alternative (AGENTS.md).

### Integrity and formats

**Canonical JSON: vouch's own contract, not RFC 8785 (JCS)** (#52)
- Chosen: number literals copied exactly; keys by code point; spec in
  `docs/canonical-json.md`.
- Rejected: JCS, which parses numbers as doubles.
- Why: a receipt must record what the tool returned. JCS would change
  integers above 2^53, long decimals (18-decimal token amounts), and
  trailing zeros (`181.50`), so receipts would attest to numbers the
  agent never saw.
- Cost: no off-the-shelf library reproduces our digests; we maintain a
  one-page spec and two implementations; numerically equal payloads
  written differently digest differently, so fixture keys must
  normalize numbers themselves.
- Revisit when: an external system requires JCS. Then add a JCS
  projection at the publishing layer; do not change the evidence.


**A 256-level nesting limit, as canonical JSON v2** (#62)
- Chosen: both implementations refuse arrays and objects nested more
  than 256 levels; the spec, receipt payload type (`version=4`), and
  checkpoint type (`version=2`) were bumped, per the spec's own rule
  that any rule change is a new version.
- Rejected: no limit (the sides disagreed between ~1000 and 10000
  levels, and Python crashed); only catching `RecursionError` (the
  boundary would then depend on the interpreter's recursion limit and
  stack in use); a limit near 1000 (too close to CPython's default).
  Also rejected: calling it a v1 clarification, which would have been
  cheaper to explain but would break the versioning promise.
- Why: agreement needs a limit both sides can enforce exactly; 256 is
  far beyond real tool results and far below any recursion limit.
- Cost: a tool response nested 256 levels deep fails the call
  (invariant 2); logs written before the bump no longer verify (none
  exist outside test data, which was regenerated).
- Revisit when: a real upstream legitimately nests deeper.


**Fixture keys normalize numbers exactly, as decimal strings** (#64)
- Chosen: the replay key hashes the arguments with every number
  rewritten as `<digits>e<exponent>` (no leading or trailing zeros);
  the fixture file still stores the literal arguments.
- Rejected: parsing numbers as float64 (integers above 2^53 and long
  decimals would collide, replaying one request's data for another);
  normalizing in the receipt (the receipt must keep the literal).
- Why: an agent that sends `5.0` where it once sent `5` is making the
  same request, and a replay miss aborts a whole eval run.
- Cost: fixtures recorded before #64 are keyed differently and must be
  re-recorded (none were committed). Exponents beyond ±2^40 are left
  as written, so absurd spellings of one value may still miss.
- Revisit when: a tool treats `5` and `5.0` differently (then the key
  must be per-tool configurable).


**Differential fuzzing runs through a CLI, not a shared library** (#63)
- Chosen: `vouch canon --lines` (base64 in, base64 or `!error` out);
  the Python test batches documents through it. The Go fuzz target
  checks Go-only properties; its corpus is committed and replayed
  through both sides by Python.
- Rejected: calling Python from inside `go test -fuzz` (needs a Python
  environment in every Go run and caps throughput); cgo or a C
  extension (a build dependency for a test).
- Why: one process per batch keeps the test fast, and the command is
  also useful to users reproducing a digest.
- Cost: the differential test skips without a built binary; `make
  test-py` now builds first, and CI runs it only in the golden job.
  Go-found inputs reach Python only once someone commits the corpus
  (`make fuzz`), not continuously.


**Signatures: Ed25519 replaces HMAC outright** (#53)
- Chosen: Ed25519 only, keys via `vouch keygen`, verifier keyring.
- Rejected: supporting HMAC and Ed25519 side by side.
- Why: HMAC cannot give third-party verification (whoever verifies can
  forge). No external data uses HMAC yet, so there is nothing to stay
  compatible with, and dual support would double the test surface.
- Cost: a new Python dependency (`cryptography`; the stdlib has no
  Ed25519); key files to manage (the private key must stay 0600).
- Revisit when: never for HMAC; key storage beyond files (KMS, HSM)
  when a real deployment needs it.


**Envelope: DSSE, signing exact payload bytes** (#53)
- Chosen: each log line is a DSSE envelope; the signature covers the
  base64 payload bytes.
- Rejected: signing a canonical re-serialization of the receipt, as
  HMAC did.
- Why: a third party verifies with any Ed25519 library, without our
  canonicalizer; DSSE is the format Sigstore and in-toto use.
- Cost: the log is no longer human-readable or greppable (base64).
  Mitigated by `vouch receipts cat`, which decodes a log to JSON lines.
- Revisit when: readability becomes a daily pain; the answer is
  tooling, not a weaker format.


**Receipts store the full response the agent received** (#20, #50)
- Chosen: `response_canonical` plus digest, signed with the payload.
- Rejected: signing only the extraction payload.
- Why: the signature must cover what the model read (a text block can
  disagree with the structured payload).
- Cost: roughly twice the size for text-block payloads.
- Revisit when: logs get large; store the response in a
  content-addressed blob and keep only its digest in the receipt.


**Crash recovery keeps a complete but unterminated last line** (#46)
- Chosen: restore its newline and keep the receipt.
- Rejected: truncating it like an unparsable fragment.
- Why: it is a faithful, signed record of what the upstream returned.
- Cost: by Append's contract that receipt was never acknowledged, so
  the agent may not have received that result.
- Revisit when: the hash chain lands (#54). An entry that was never
  acknowledged may be better dropped so the chain states only what was
  delivered.


**Committed, public test keys** (#53)
- Chosen: `testdata/keys/{golden,eval}.pem` are in the repository.
- Rejected: generating keys in CI, or keeping the eval key secret.
- Why: the golden log and the eval runs must be reproducible by anyone;
  Ed25519 is deterministic, so a fixed key makes regeneration
  byte-stable.
- Cost: those receipts prove integrity of published data, not who
  produced it; a README next to the keys says so. The proxy refuses
  keys readable by other users and git does not keep file modes, so the
  harness copies the eval key to a private 0600 file per batch.
- Revisit when: published eval results need to prove authorship; sign
  those with a maintainer key kept out of the repository.


**The log checks the chain on open, not signatures** (#53, #54)
- Chosen: the proxy's store verifies the hash chain when it opens a log
  (a broken chain is a hard error) but not signatures; `vouch receipts
  verify` and the Python verifier check both.
- Rejected: verifying signatures in the store with the proxy's own key.
- Why: trust belongs to whoever reads the log, with their own keyring.
  The chain check is cheap, needs no key, and stops the proxy from
  appending to a log whose history was rearranged.
- Cost: a log whose entries were re-signed by someone holding the key
  is extended without complaint; that attacker is outside what the
  proxy can judge (threat model).


**Tail truncation is left to an external witness** (#54)
- Chosen: checkpoints plus `--require-sealed` and `--expect-head`; eval
  runs record their head in the committed `meta.json`.
- Rejected: claiming the log detects truncation by itself.
- Why: a prefix of a valid chain is a valid chain. Nothing inside a
  log can show that later entries once existed; only a copy of the head
  kept elsewhere can.
- Cost: operators who want truncation detected must keep the head the
  proxy prints (or publish it); without it, a log cut back to an
  earlier checkpoint verifies.
- Revisit when: logs are shared beyond the eval data; publish heads to
  an append-only witness (a transparency log) rather than trusting
  whoever holds the file.


**docker-compose mounts the whole key volume into verify and eval**
- Chosen: one `keys` volume, read-write for the proxy, read-only for
  the containers that only need the public key.
- Cost: those containers can read the private key.
- Revisit when: compose is used beyond local runs; split the public key
  into its own volume.

### Proxy and transport

**The log, not the proxy, numbers a session's turns** (#69)
- Chosen: `store.Log.AppendNextTurn` assigns `turn_index` inside the
  append lock, one past the session's highest in the log.
- Rejected: a counter in the proxy seeded from the log at startup (two
  sources of truth, and racy once calls run concurrently).
- Why: the log already indexes every (session, turn) for replay
  protection, and it is the one place appends are serialized.
- Cost: `turn_index` is the order receipts were written, not the order
  requests arrived; under concurrency (#67) the two can differ.


**Concurrency: receipt if and only if the agent was sent the result** (#67)
- Chosen: every request after `initialize` runs on its own goroutine;
  `mcp.Client` multiplexes by id. A cancelled request is cancelled
  upstream (under the upstream's id) and gets no response and no
  receipt; if its result already came back, it is receipted and sent
  anyway. `Run` waits for in-flight requests before returning, so
  sealing never races a receipt.
- Rejected: receipting a result that arrives after cancellation (it
  would attest to data no agent saw); a per-upstream worker pool
  (bounds nothing the upstream does not already bound, adds tuning).
- Why: one slow tool stalled `ping` and every other tool.
- Cost: `turn_index` is completion order, not request order. A
  JSON-RPC error with id null from an upstream can be attributed only
  when exactly one call is in flight; otherwise it is logged and the
  call waits for its real response. Concurrency is unbounded: an agent
  can open as many upstream calls as it sends requests. Only
  `notifications/progress` and `notifications/message` are forwarded
  from upstreams; the rest wait for #68.
- Revisit when: an agent floods the proxy; add a per-session limit.


**One protocol version per session, chosen by the upstreams** (#68)
- Chosen: the agent's `initialize` goes to every upstream unchanged;
  the proxy answers with the version they all chose, if it is in
  `SupportedVersions`, and fails `initialize` otherwise, naming each
  upstream's version.
- Rejected: echoing the agent's version whatever the upstreams said
  (the old behavior, which silently mixed versions); translating
  between versions (a protocol implementation vouch does not need).
- Cost: federating upstreams that speak different versions fails
  loudly instead of mostly working.
- Revisit when: a real deployment needs mixed versions.


**Server-to-client requests: an allowlist, forwarded under proxy ids** (#68)
- Chosen: `sampling/createMessage`, `roots/list`, and
  `elicitation/create` go to the agent under ids `"vouch-N"`; the
  answer goes back under the upstream's id. `ping` is answered by the
  proxy. Upstream cancellation is forwarded. When the agent leaves,
  forwarded requests fail before `Run` waits for in-flight calls, so a
  call blocked on sampling cannot hold the proxy open.
- Rejected: forwarding any method (an upstream could ask the agent
  things it never offered); rewriting ids per upstream only (collides
  across upstreams).
- Cost: sampling results are not receipted. They are the agent's own
  output, not tool data; the tool result that uses them is.
- `tools/list_changed` refreshes routes before the agent is told; a
  refresh that finds a name collision keeps the old routes and logs.


**Citation channel: a block derived from the receipt, opt-in** (#81)
- Chosen: `--cite` appends one text block per receipted result, after
  the receipt is written, listing each fact as `metric = value  ->
  [[r:<12-hex prefix>#<pointer>]]`. The receipt signs the upstream's
  result without the block. Results without facts are unchanged. The
  harness runs it as a separate condition (`<model>+cite`) with an
  extra system-prompt sentence, and the report adds citation adherence.
- Rejected: always on (the proxy would no longer be transparent, and
  the plain condition measures what agents do unprompted); signing the
  block into `response_canonical` (the receipt records what the tool
  returned; the block adds nothing the receipt does not already sign);
  full 32-digit ids (models copy long hex badly; a unique prefix is
  enough, and an ambiguous one fails as UNSUPPORTED, never as a wrong
  match).
- Cost: under `--cite` the agent sees a result that differs from the
  upstream's by one block, and its JSON is re-serialized (keys sorted).
  Values in the block are formatted from float64 facts, so a tool's
  `181.50` appears as `181.5`.
- Revisit when: agents cite reliably enough to make Tier 1 the default.


**Streamable HTTP: one session per process, simple stream routing** (#79)
- Chosen: `--listen` serves exactly one session; DELETE or a signal
  ends it and seals the log, as EOF does on stdio. Every POSTed request
  is answered on an event stream. Outgoing messages route by id
  (responses), by progressToken (progress), else to the GET stream,
  else to the newest open POST stream. Browser origins must be
  loopback; there is no authentication. Upstreams at a URL get
  per-upstream headers, `env:VAR` values read from the environment.
- Rejected: many sessions per process (a session is one log, one turn
  sequence, one seal; several would need per-session logs and a
  different CLI); stream resumability via Last-Event-ID (buffering and
  replay for a case the proxy's clients rarely hit); relating
  server-to-client requests to the POST that caused them (upstreams do
  not say which request a sampling call belongs to).
- Cost: a second agent needs a second proxy. A dropped stream loses
  its response (the call is still receipted: it happened). With no
  stream open, an unprompted notification is dropped and a forwarded
  request fails. Anyone who can reach a non-loopback `--listen` can
  make receipted calls.
- Revisit when: an agent framework needs several sessions or
  resumption, or the proxy is exposed beyond one machine (then add
  authentication first).


**Durability over latency: one fsync per receipt** (#77)
- Measured: on an M1 Pro, the proxy adds about 4.9 ms p50 and 8 ms p99
  per call, of which the signed, fsynced append is about 4.1 ms p50.
- Chosen: keep fsync before replying (invariant 2: an acknowledged
  result has a durable receipt).
- Rejected for now: group commit (batch concurrent appends into one
  fsync; worth it only under concurrent load, and adds a window where
  several calls wait on one flush); no fsync (a crash would lose
  receipts for results the agent already acted on).
- Cost: appends are serialized, so concurrent calls queue on the disk.
- Revisit when: a real agent's call rate makes the queue visible; group
  commit is then the change, not dropping fsync.


**Benchmark numbers are committed, not measured in CI** (#77)
- Chosen: `make bench` writes `docs/bench/latency.json` with the
  machine it ran on; the README table renders from it, so
  `readme-check` stays deterministic.
- Rejected: measuring in CI (shared runners make p99 noise, and the
  README would change on every run); hand-written numbers (invariant 7).
- Cost: the published numbers are as fresh as the last `make bench`.


**Integration tests: one pinned reference server, in-process proxy** (#75)
- Chosen: `@modelcontextprotocol/server-everything` at a pinned
  version, installed by `make integration` with install scripts off,
  spawned through the real `proxy.Spawn`, served by an in-process
  `proxy.Server`; its own CI job with Node.
- Rejected: a floating version (a release could break CI for reasons
  unrelated to a change); driving the `vouch` binary (the harness
  end-to-end test already covers the CLI, and in-process access lets
  the test check the log directly); more reference servers (the
  others exercise resources and prompts, which vouch does not federate).
- Why: Go fakes encode vouch's reading of the spec; only a real SDK
  shows whether that reading holds. It found #74 on its first run.
- Cost: Node in CI; the test skips locally without `make integration`;
  the pin must be bumped by hand.
- Revisit when: the SDK's behavior changes in a release worth tracking.


**Upstream commands are split like a shell, not run by one** (#70)
- Chosen: POSIX quoting and backslashes; no expansion of any kind.
- Rejected: `sh -c` (expansions make the argv, and so the default
  upstream name that keys fixtures, depend on the environment); a JSON
  argv flag (awkward to type, and unquoted commands already work).
- Cost: pipes, redirects, and `$VAR` in `--upstream` are literal
  characters; such upstreams need a wrapper script.


### Verifier heuristics

Each follows invariant 3 (never guess toward SUPPORTED); each can be
wrong in the stated way.

- **Parentheses negate only signed metrics** (#10). "(1.35%)" is
  negative for a change or MACD; "(62.3)" is an aside for RSI, a price,
  or volume. Wrong if an agent writes an aside around a change value.
- **A minute timeframe needs a chart word** (#11). "15m chart" is a
  timeframe, "52.4m shares" is 52.4 million. A bare "15m RSI" stays
  ambiguous and is read as a magnitude.
- **"from X to Y" is a move, not a range.** Its endpoint is a price
  claim (#39). "between X and Y" and "X-Y" are ranges.
- **Bare four-digit numbers are not masked as years.** "closed at 2026"
  can be a price; years are masked only after a temporal word.
- **Stacked citations with too few numbers bind nothing** (#12), rather
  than pairing arbitrarily; those numbers fall back to Tier 2.
- **Cited claims check units, not metric words** (#13). "volume was
  1.92% [[..change_pct]]" passes, because trusting the keyword table
  over an explicit citation misfires for schemas it does not know.
- **Undated facts take their receipt's date** (#14): `data_asof`, else
  the call's wall time. Wrong for an upstream that returns stale data
  with no date.
- **Rounding is judged at the claim's own precision; ties go both ways**
  (pitfall P-036, issue #42). "182" covers 181.5-182.5.

**Look-ahead: STALE and a log-level list, bare dates end the day** (#84)
- Chosen: `--as-of` makes post-moment data fall outside every time
  window (a match is `STALE`, noted "look-ahead"), lists every receipt
  with later data, and fails the run if there is any, even when no
  claim uses it. A bare date means the end of that day.
- Rejected: a seventh verdict (the claim is true of the wrong time,
  which is what `STALE` already means); reading a bare date as the start
  of the day (a same-day close would pass under an intraday as-of: a
  guess toward SUPPORTED, invariant 3).
- Cost: daily data dated the as-of day is flagged under an intraday
  as-of even if the tool meant the previous close; tools should stamp
  data with times.
- Revisit when: a data source dates bars by their open.

**HTML report: static, escaped, no scripts** (#86)
- Chosen: one self-contained page; details on hover (`title`) and via a
  link to each claim's table row; a letter per verdict beside each span
  so color is not the only signal; light and dark themes.
- Rejected: JavaScript tooltips or filtering (the page renders answers
  and receipt strings, which are untrusted; no script means nothing to
  inject into); external CSS or fonts (the report must open offline and
  leak nothing).
- Cost: `title` tooltips are plain text and slow to appear.

### Evaluation

- **Synthetic upstream with real tickers** (Phase 2). Chosen over
  recorded real data for reproducibility and redistributability; real
  tickers make a model's recalled real-world prices visible as
  UNSUPPORTED. Cost: no claim about real-market behavior; every payload
  says it is synthetic.
- **One OpenAI-compatible client for all providers.** Chosen over
  vendor SDKs for breadth and zero dependencies. Cost: provider-specific
  features (for example Gemini thinking budgets, Anthropic-native tool
  semantics) are only reachable through `params`.
- **A broken MCP session ends that run** (#29). A run in which every
  later tool call fails would be misleading data; the batch records the
  error and continues.
- **Unsaved hand-added spans are lost on page reload** (#36). Saving on
  add would need a placeholder label that leaks into label files and
  agreement statistics.

### Process

- **Merge commits, never squash or rebase.** Docs cite commit hashes.
  Cost: a noisier history.

## Session log

- 2026-09-27: audit; added `AGENTS.md`, `CLAUDE.md`, `docs/roadmap.md`,
  `docs/pitfalls.md`, `docs/handoff.md`. No code changes.
- 2026-09-27: Phase 0 complete (venv, ruff, mypy strict, race, coverage,
  design doc corrections, generated README). Fixed P-003, P-043; found
  P-034.
- 2026-09-27: PRs #1, #2 opened, CI green. Phase 1 complete: corpus
  (130 cases), fixed P-030 to P-037, STALE verdict, Hypothesis
  properties (found and fixed an epsilon bug); fixed `make fmt` order.
- 2026-09-27: Phase 2 tooling: synthetic market server, provider-agnostic
  agent runner (Gemini smoke-tested), blind labeling tool (browser-tested;
  fixed a key-press race), real-eval scorer. Found P-044.
- 2026-09-27: merged PRs #1-#7 (#2-#4 re-opened as #5-#7 after a stacked
  merge closed them, P-006). Audit by three parallel agents; 35 issues
  (32 from the audit, #8-#39; 3 found while fixing, #42, #43, #48) filed and fixed across 9 PRs.
- 2026-09-27: Phase 3: Ed25519/DSSE (#53, PR #58), hash chain,
  checkpoints and tamper suite (#54, #55, PR #59), threat model (#56).
- 2026-09-27: Phases 4 and 5 and the citation channel: issues #61-#81,
  PRs #65, #66, #71-#73, #76, #78, #80, #82. The reference-server
  integration test found #74 on its first run.
