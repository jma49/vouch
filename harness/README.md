# vouch-harness

Eval harness for vouch (docs/design.md sections 8–10).

- **Answer synthesis** (`answers.py`) — clean answers generated from a receipt log, cited (Tier 1) and uncited (Tier 2) variants.
- **Mutation injector** (`mutate.py`) — machine-generated known-bad variants: digit swap, magnitude shift, entity swap, timeframe swap, sign flip, fabricated citation, false absence.
- **Gold set** (`gold.py`) — clean + mutant cases, regenerated deterministically from the log and seed.
- **Repeated-run eval** (`eval.py`) — N runs reporting mean, std, range, and bootstrap 95% CI. Refuses N < 2.

```bash
vouch-eval --receipts ./receipts/receipts.jsonl --n 10 --tolerances tolerance.yaml
```

Fixture record/replay lives in the Go proxy (`vouch proxy --mode=record|replay`), because it has to sit on the upstream call path.

Two mutations are known not to be caught by the MVP verifier and are kept in the gold set on purpose, so the per-mutation recall table reports the gap rather than hiding it: `timeframe_swap` (Tier 2 claims carry no timeframe yet) and `false_absence` (an omitted claim produces no verdict to flag).
