# Pitfalls

Known traps in this repository. One entry per trap. Fixed entries stay
here, marked fixed, so the history keeps teaching.

Format: **Symptom** / **Cause** / **Fix / workaround** / **Status**.
"Reproduced" means observed by running code; "code reading" means
inferred from source and not yet reproduced.

---

## Environment and build

### P-001 Stale Python venv after moving the repo
- **Symptom:** `make test-py` or `vouch-eval` fails with
  `bad interpreter: .../verifier/.venv/bin/python3.x: no such file`.
- **Cause:** venvs hardcode absolute interpreter paths. The Makefile
  `$(VENV)` target only checks that the directory exists, so it never
  rebuilds a broken venv.
- **Fix / workaround:** `make install-py` now detects a venv that does
  not import both packages from this checkout and rebuilds it.
- **Status:** fixed in ddc76c4. Reproduced 2026-09-27.

### P-002 Editing canonicalization or receipt fields breaks the golden job
- **Symptom:** CI job "cross-language golden log" fails with a diff in
  `testdata/receipts_golden.jsonl`.
- **Cause:** the golden log is written by Go and read by Python; any
  change to canonical bytes, receipt fields, or signing changes it.
- **Fix / workaround:** if the change is intentional, run
  `make golden`, commit the regenerated log in the same commit, and run
  both Python suites. If unintentional, you broke cross-language
  equality — fix the code, not the file.
- **Status:** by design.

### P-003 `make lint` could never fail on formatting
- **Symptom:** unformatted Go files were listed by `make lint`, yet the
  target exited 0.
- **Cause:** `gofmt -l` prints offending files but exits 0.
- **Fix / workaround:** the target now fails on any `gofmt -l` output.
- **Status:** fixed in 95dbde8. Code reading.

### P-004 Editor reports unresolved imports for `vouch_verifier`
- **Symptom:** Pyright/Pylance flags `import vouch_verifier` and
  `import pytest` as missing, though tests and mypy pass.
- **Cause:** the editor's interpreter is not `verifier/.venv`, where
  both packages are installed in editable mode.
- **Fix / workaround:** select `verifier/.venv/bin/python` as the
  workspace interpreter.
- **Status:** by design (environment).

### P-005 README generated sections go stale
- **Symptom:** CI step "README is current" fails.
- **Cause:** a change altered verifier or eval output, or someone
  edited text between `BEGIN/END GENERATED` markers.
- **Fix / workaround:** `make readme` and commit the result with the
  change that caused it.
- **Status:** by design.

---

## Canonicalization and signing

### P-010 Canonical JSON is not RFC 8785
- **Symptom:** `62.30` and `62.3` in an upstream result produce
  different digests and signatures.
- **Cause:** both implementations preserve number literals verbatim
  rather than applying JCS number serialization. Consistent across
  languages, but not the standard the design doc names.
- **Fix / workaround:** do not assume semantic equality of numbers
  implies equal digests. Roadmap Phase 4.
- **Status:** open. Code reading (`proxy/internal/receipt/canonical.go`).

### P-011 HMAC does not provide third-party verifiability
- **Symptom:** none at runtime; this is a false security claim in
  `docs/design.md` §3.1.
- **Cause:** HMAC is symmetric. A third party needs the key to verify,
  and with the key can forge receipts.
- **Fix / workaround:** treat receipts as tamper-evident only against
  parties without the key. Roadmap Phase 3 (Ed25519).
- **Status:** open.

### P-012 Deleted, truncated, or reordered receipts are not detected
- **Symptom:** removing a line from `receipts.jsonl` still verifies.
- **Cause:** receipts are signed individually; there is no hash chain
  or checkpoint binding them together.
- **Fix / workaround:** roadmap Phase 3 (`prev_digest` chain).
- **Status:** open. Code reading.

---

## Proxy

### P-020 Reusing `--session` after restart fails every tool call
- **Symptom:** after restarting the proxy with the same `--session`,
  every `tools/call` returns a `receipt:` internal error.
- **Cause:** `Server.turn` starts at 0 each process; `store.Log`
  rejects the `(session_id, turn_index)` pairs already in the log.
- **Fix / workaround:** omit `--session` (random id) or use a new one.
  Roadmap Phase 5.
- **Status:** open. Code reading (`proxy/internal/proxy/proxy.go`).

### P-021 One slow upstream call blocks everything
- **Symptom:** `ping` and unrelated calls stall while one `tools/call`
  is in flight.
- **Cause:** `Server.Run` dispatches serially; `mcp.Client` allows one
  call in flight per upstream.
- **Status:** open. Code reading. Roadmap Phase 5.

### P-022 Server-to-client requests from an upstream break the call
- **Symptom:** a `tools/call` fails with
  `response id X does not match request id Y`.
- **Cause:** an upstream request (sampling, roots, elicitation) has
  both `method` and `id`, so `mcp.Client.Call` treats it as a
  mismatched response.
- **Status:** open. Code reading (`proxy/internal/mcp/mcp.go`).

### P-023 `--upstream` is split on whitespace
- **Symptom:** upstream commands with quoted arguments or spaces in
  paths start with wrong argv.
- **Cause:** `proxy.Spawn` uses `strings.Fields`, no shell quoting.
- **Fix / workaround:** wrap complex invocations in a script.
- **Status:** open. Documented in code.

### P-024 Schema extraction errors fail the agent's call
- **Symptom:** an upstream response shape change turns a working tool
  call into an internal error for the agent.
- **Cause:** by design (invariant 2): no receipt, no pass-through. It
  is still surprising in practice.
- **Fix / workaround:** keep schemas in sync with upstream versions;
  test schemas against recorded fixtures.
- **Status:** by design.

---

## Verifier

Reproduced 2026-09-27 against `testdata/receipts_golden.jsonl`.

### P-030 Years and dates are extracted as claims
- **Symptom:** `As of 2026-07-24, NVDA closed at 100.` yields
  `2026 → CONTRADICTED` against `close_price`.
- **Cause:** `_NUMBER_RE` accepts any digit run; the sentence has an
  entity and a metric keyword, so Tier 2 resolves it.
- **Status:** fixed in `fix(verifier): tokenize numeric spans before resolving claims`. Reproduced. Roadmap Phase 1.

### P-031 Magnitude words are ignored
- **Symptom:** `NVDA volume is 12 million shares.` claims `12`.
- **Cause:** `_parse_number` does not read scale words or suffixes.
- **Status:** fixed in `feat(verifier): scale magnitude words and suffixes in numeric claims`. Reproduced.

### P-032 Attribution ignores clause boundaries
- **Symptom:** `NVDA's RSI is 62, versus AMD's RSI of 48.` attributes
  `62` to AMD. `Unlike AMD, which fell 1.35%, NVDA rose 1.92%.` negates
  NVDA's `1.92%` because "fell" appears earlier in the sentence.
  `AMD last traded at 172.04; NVDA closed at 181.52.` reads `172.04` as
  NVDA's close.
- **Cause:** entity, metric keyword, and direction word are each chosen
  by character distance (or mere presence) across the whole sentence,
  in either direction, with no notion of clauses.
- **Status:** fixed in `fix(verifier): attribute entity, metric, and direction by clause`. Reproduced.

### P-033 Any in-tolerance candidate makes a claim SUPPORTED
- **Symptom:** with many facts for the same (entity, metric), such as
  an OHLCV series, a wrong value near any bar passes.
- **Cause:** Tier 2 claims carry no timeframe or as-of, and
  `match_claims` returns `SUPPORTED` on the first candidate within
  tolerance. False negatives grow with receipt count.
- **Status:** open. Code reading (`verifier/src/vouch_verifier/matcher.py`).

### P-034 A percentage can be attributed to a price metric
- **Symptom:** `AMD is down 1.35% on the day and is trading at 172.40.`
  yields `1.35% → CONTRADICTED` against `last_price` 172.04.
- **Cause:** `_nearest_keyword` picks the closest metric keyword
  ("trading at") regardless of the number's unit; the percentage
  fallback to `change_pct` only applies when no keyword is found.
- **Status:** fixed in `fix(verifier): require a claim's unit to agree with its metric`. Reproduced 2026-09-27.
  example is phrased to avoid it.

### P-035 An integer followed by a comma keeps the comma in its span
- **Symptom:** `NVDA RSI is 62, well above AMD.` reports the claim text
  as `62,`; report highlighting and mutations operate on the wrong span.
- **Cause:** `_NUMBER_RE` uses `\d[\d,]*` for thousands separators,
  which also swallows a trailing comma.
- **Status:** fixed in `fix(verifier): tokenize numeric spans before resolving claims`. Reproduced 2026-09-27.

### P-036 Display rounding is judged by class, not by the claim's precision
- **Symptom:** `NVDA closed at 182.` (actual 181.52) is CONTRADICTED;
  `-0.4` for a MACD of -0.42 is CONTRADICTED.
- **Cause:** tolerance is a per-class constant (`abs`, `rel`,
  `display_rel`). A claim's own displayed precision — "182" asserts
  181.5-182.5 — is never considered, so legitimate rounding outside
  `display_rel` reads as a contradiction.
- **Status:** fixed in `feat(verifier): judge display rounding against the claim's own precision`. Reproduced 2026-09-27.

### P-037 Multipliers and ranges are judged as point values
- **Symptom:** `NVDA volume was 3x its 50-day average.` yields
  `3 -> UNSUPPORTED` against volume; `in the 60-65 range` yields
  `60 -> CONTRADICTED`.
- **Cause:** the tokenizer does not recognize `3x`, `60-65`, or
  `between 60 and 65` as non-point expressions.
- **Status:** fixed in `fix(verifier): tokenize numeric spans before resolving claims`. Reproduced 2026-09-27.

---

## Evaluation

### P-040 Headline metrics are self-referential
- **Symptom:** coverage 1.00 and false-positive rate 0.00 look perfect.
- **Cause:** gold-set answers are generated from templates that must
  stay in sync with the extractor's keyword table
  (`harness/src/vouch_harness/answers.py`), and mutations are applied
  to spans that same extractor found. The golden log has 5 facts over
  2 entities.
- **Fix / workaround:** do not cite these numbers as verifier quality.
  Roadmap Phase 2.
- **Status:** open.

### P-041 Zero variance is guaranteed, not measured
- **Symptom:** every metric reports `± 0.00`; stability is 1.00.
- **Cause:** the verifier is deterministic and only `entity_swap`
  consumes the seed. No LLM is in the loop, so N runs repeat one run.
- **Status:** open. Roadmap Phase 2.

### P-042 `timeframe_swap` is never generated
- **Symptom:** the per-mutation table omits `timeframe_swap`.
- **Cause:** synthesized answers contain no timeframe words, so the
  mutation has nothing to swap.
- **Status:** open. Reproduced.

### P-043 Every `match_claims` call leaked a SQLite connection
- **Symptom:** ResourceWarnings ("unclosed database") under coverage;
  one per gold case per eval run.
- **Cause:** `with conn:` on a sqlite3 connection scopes a transaction,
  it does not close the connection.
- **Fix / workaround:** index scoped with `contextlib.closing`; pytest
  now treats warnings as errors so a regression fails the suite.
- **Status:** fixed in 0ead6f5. Reproduced.
