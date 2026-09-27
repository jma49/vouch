# Handoff

Snapshot of where work stands, for the next session. Overwrite
"Current state" and "Next steps" each session; append to "Session log".

**Last updated:** 2026-09-27

## Current state

- **Phases 0 and 1 are complete.** Three stacked branches, each with a
  PR:
  - `docs/agent-workflow` → `main` (PR #1, CI green)
  - `chore/phase0-hygiene` → `docs/agent-workflow` (PR #2, CI green)
  - `feat/phase1-verifier-prose` → `chore/phase0-hygiene` (PR #3)
  None are merged. Merge in order: #1, #2, #3.
- Tests: verifier 247 passed + 1 strict xfail (documented known gap),
  harness 21, Go under `-race`. `make lint` clean (ruff, mypy --strict).
- The verifier now tokenizes before resolving (`tokens.py`), judges
  rounding against the claim's own precision (`display_round`), reads
  magnitudes, units, dates, and timeframes, attributes by clause, and
  emits `STALE`. Behavior on prose is pinned by a 130-case corpus
  (`verifier/tests/corpus/claims.yaml`) and Hypothesis properties.
- Harness eval metrics are unchanged (0.89 detection, 0.00 FP). They
  are still synthetic and extractor-shaped (P-040, P-041).

## Next steps

1. Merge PRs #1-#3 after review.
2. Phase 2 (real evaluation). Needs maintainer decisions first:
   which models, API budget, and who labels. Suggested order: task set
   over recorded fixtures → agent runner through the proxy in replay
   mode → labeling spec → label 200-300 claims → report.
3. Small, independent: tests for `proxy/internal/mcp` (Go coverage is
   58%, lowest package).

## Open questions for the maintainer

- Phase 2: which models (e.g. Claude, GPT, one open-weights), API
  budget, and whether you will label or we write a labeling tool first?
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
