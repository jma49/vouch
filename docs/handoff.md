# Handoff

Snapshot of where work stands, for the next session. Overwrite
"Current state" and "Next steps" each session; append to "Session log".

**Last updated:** 2026-09-27

## Current state

- **Phases 0 and 1 complete; Phase 2 tooling complete, data pending.**
  Four stacked branches, each with a PR, none merged. Merge in order:
  - `docs/agent-workflow` → `main` (PR #1)
  - `chore/phase0-hygiene` → #1 (PR #2)
  - `feat/phase1-verifier-prose` → #2 (PR #3)
  - `feat/phase2-real-eval` → #3 (PR #4)
- Phase 2 pipeline, all tested offline, plus the agent end to end through
  the real Go proxy in CI:
  - `vouch_harness.market`: synthetic market MCP server (8 real tickers,
    generated data ending 2026-07-24).
  - `vouch-agent` / `make agent`: any OpenAI-compatible model; cached,
    resumable, `--dry-run` estimates requests.
  - `vouch-label`: blind labeling UI (tested in a browser),
    `docs/labeling.md`, Cohen's kappa.
  - `vouch-eval-real` / `make eval-real`: verifier vs. labels, per-model
    misreport rate.
- Gemini: `gemini-3.8-flash` via the OpenAI-compatible endpoint works
  end to end (one smoke run, 2 requests, cached in `eval/.cache`; the
  run itself was not kept). `GEMINI_API_KEY` is set in the
  maintainer's shell, so `make agent` will spend real requests.
- Tests: verifier 247 + 1 xfail, harness 65, Go under `-race`.

## Next steps

1. Maintainer: merge PRs #1-#4.
2. Maintainer: approve and run the first Gemini pass. Suggested: start
   with `make agent MODEL=gemini-flash ARGS="--samples 3"` (90 runs,
   ~270 requests at 8/min, about 35 min), check a few answers, then
   raise to 5 samples; completed runs and cached responses are reused.
   Set `rpm` in `eval/models.yaml` to the account's real limit.
3. Label with `vouch-label serve --labeler <name>`; target 200-300
   claims. Commit `eval/runs/` and `eval/labels/`.
4. Then: switch the README headline to `make eval-real` output (a
   generated block), add providers, build the citation channel (P-044).

## Open questions for the maintainer

- Is committing Gemini outputs under `eval/runs/` acceptable? (Proposed:
  yes; they are the evaluation's raw data and make it reproducible.)
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
