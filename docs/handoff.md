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
   - Phase 4: decide JCS vs documented literal-preserving contract;
     differential fuzzing Go <-> Python.
   - Phase 5: concurrent upstream client (P-021), session resume
     (P-020), server-to-client request forwarding (P-022).
   - P-044: citation channel so agents can cite receipts.

## Open questions for the maintainer

- Phase 2: when can a Gemini run happen, and is committing its outputs
  under `eval/runs/` acceptable?
- Phase 3: replace HMAC with Ed25519 outright, or support both?
- Phase 4: implement real JCS, or document the literal-preserving
  contract and drop the RFC 8785 claim?

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
