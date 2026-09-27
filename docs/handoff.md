# Handoff

Snapshot of where work stands, for the next session. Overwrite
"Current state" and "Next steps" each session; append to "Session log".

**Last updated:** 2026-09-27

## Current state

- MVP pipeline works end to end: proxy (Go) → signed JSONL receipts →
  verifier (Python) → eval harness. Tests green: Go (`-race`),
  verifier 49, harness 16.
- A full project audit was completed. Findings are recorded in
  `docs/pitfalls.md`; the resulting plan is `docs/roadmap.md`.
- Agent operating rules are in `AGENTS.md` (`CLAUDE.md` imports it):
  conversation in Chinese, everything committed in English.
- No roadmap phase has started. All items are `todo`.

## Known broken locally

- `verifier/.venv` points at an interpreter from a previous checkout
  location (P-001). Rebuild with `rm -rf verifier/.venv && make install-py`.

## Next steps

1. Phase 0 hygiene: fix the Makefile venv target, add `-race`, ruff,
   and mypy to CI, correct overstated claims in `docs/design.md`.
2. Phase 1: fix the reproduced verifier false verdicts (P-030 to
   P-033), each starting with a failing test, and build the adversarial
   regression corpus.
3. Then Phase 2 (real LLM eval), which depends on Phase 1.

## Open questions for the maintainer

- Which models and API budget for the Phase 2 real eval?
- Replace HMAC with Ed25519 outright, or support both (Phase 3)?
- Implement real JCS, or document the literal-preserving contract
  and drop the RFC 8785 claim (Phase 4)?

## Session log

- 2026-09-27: audit; added `AGENTS.md`, `CLAUDE.md`, `docs/roadmap.md`,
  `docs/pitfalls.md`, `docs/handoff.md`. No code changes.
