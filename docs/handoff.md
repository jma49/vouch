# Handoff

Snapshot of where work stands, for the next session. Overwrite
"Current state" and "Next steps" each session; append to "Session log".

**Last updated:** 2026-09-27

## Current state

- **Phases 0 and 1 complete; Phase 2 tooling complete, data pending**
  (the maintainer cannot provide Gemini runs for now; see memory).
  Everything is merged to `main`; no open PRs.
- **Audit 2026-09-27:** three parallel audits (proxy, verifier, harness)
  plus fixes filed issues #8-#48, all closed. Fix PRs: #40, #41, #44,
  #45, #46, #47, #49, #50, and this docs sync. Highlights:
  - receipts now bind the whole result the agent received (#20);
  - Go and Python canonical JSON agree on U+2028/2029, surrogates, and
    duplicate keys, with "rejected" shared vectors (#9, #27);
  - the proxy survives malformed frames, stray upstream output, and
    crashes mid-append (#21-#28);
  - the verifier handles Unicode minus, accounting negatives, tables,
    lists, stacked citations, and cited signs (#8-#15, #39, #43);
  - the harness survives malformed tool calls and transport errors;
    the label server refuses cross-origin writes (#29-#38);
  - extraction is linear (2000 sentences: 11.7 s to 0.3 s) (#19);
  - CI tests Python 3.11-3.14 (#48).
- Tests: verifier ~314 (+1 documented xfail), harness 115, Go under
  `-race`. Corpus 166 cases.
- Process lessons are in `docs/pitfalls.md` P-006 (stacked PR merges)
  and P-007 (semantic conflicts between green PRs), and in AGENTS.md
  (issue-first bugs, merge commits, test against current main).

## Next steps

1. Phase 2 data, when the maintainer can run a model:
   `make agent MODEL=gemini-flash ARGS="--samples 3"` (dry-run first),
   then label with `vouch-label serve`. Never run it without approval.
2. Work that needs no model calls, in suggested order:
   - Phase 3: Ed25519 signatures with key ids, hash chain
     (`prev_digest`), tamper suite, `docs/threat-model.md`.
   - Phase 4: differential fuzzing Go <-> Python (the contract is now
     specified; JCS was decided against, see below).
   - Phase 5: concurrent upstream client (P-021), session resume
     (P-020), server-to-client request forwarding (P-022).
   - P-044: citation channel so agents can cite receipts.

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

**The log reader checks structure, not signatures** (#53)
- Chosen: the proxy's store decodes envelopes on startup without
  verifying them; `vouch receipts verify` and the Python verifier do.
- Rejected: verifying in the store with the proxy's own key.
- Why: trust belongs to whoever reads the log, with their own keyring;
  the proxy would only be checking its own signatures.
- Cost: a proxy restarted on a tampered log keeps appending to it; the
  tampering is caught when the log is verified, not before.
- Revisit when: the hash chain lands (#54), since appending then
  extends a chain whose head the proxy should trust.

**docker-compose mounts the whole key volume into verify and eval**
- Chosen: one `keys` volume, read-write for the proxy, read-only for
  the containers that only need the public key.
- Cost: those containers can read the private key.
- Revisit when: compose is used beyond local runs; split the public key
  into its own volume.

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
