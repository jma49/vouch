# AGENTS.md

Operating manual for AI coding agents (and humans) working in this
repository. Read this first, then `docs/handoff.md` for where the last
session stopped.

## Language policy

- **Conversation with the maintainer: Chinese.** Explanations,
  questions, plans, and status updates in chat are written in Chinese.
- **Everything that lands in git: English.** Code, identifiers,
  comments, docstrings, docs, test names, commit messages, PR titles
  and descriptions, issue text, and log/error strings. No exceptions,
  including the maintained docs below.
- Technical terms stay in their original form in both (`receipt`,
  `verdict`, `JCS`, `tools/call`), never translated.

## What this project is

vouch answers one question: *is the number the agent just said
actually a number its tools returned?* A Go MCP proxy records a signed
receipt per tool call; a Python verifier matches numeric claims in the
agent's answer against those receipts; a Python harness measures the
verifier. Full design: `docs/design.md`. Plan: `docs/roadmap.md`.

| Path | Language | Role |
|---|---|---|
| `proxy/` | Go | Federating MCP proxy, receipts, signing, record/replay |
| `verifier/` | Python | Claim extraction, matching, verdicts, `vouch-verify` |
| `harness/` | Python | Mutation injection, gold set, repeated-run eval, `vouch-eval` |
| `schemas/` | YAML | Per-tool fact-extraction configs |
| `testdata/` | JSON/JSONL | Cross-language vectors, Go-written golden receipt log |

## Commands

```bash
make test          # go vet + go test -race, then both Python suites
make lint          # gofmt, go vet, ruff check + format --check, mypy --strict
make fmt           # apply gofmt and ruff fixes
make cover         # coverage for Go and Python (reported, not gated)
make golden        # regenerate testdata/receipts_golden.jsonl from Go
make eval          # vouch-eval over the golden log, N=10
make build         # proxy/bin/vouch
```

`make test lint` must pass before any commit; CI runs the same checks.
`make install-py` (a dependency of the Python targets) rebuilds the
venv on its own if it is stale.

## Non-negotiable invariants

Breaking one of these is a bug even if every test passes.

1. **Cross-language byte equality.** Go writes receipts, Python
   verifies them. Canonical JSON and signing payloads must be
   byte-identical across both. Any change to `proxy/internal/receipt`
   or `verifier/src/vouch_verifier/canonical.py` requires updating
   `testdata/canonical_vectors.json`, running `make golden`, and
   passing both suites. CI fails on golden-log drift.
2. **The receipt is the product.** If a receipt cannot be written, the
   tool call fails. Never pass unverifiable data through silently.
3. **The verifier never guesses.** Ambiguity yields `UNVERIFIABLE` or
   `UNSUPPORTED`, not a best-effort `SUPPORTED`. A config typo must
   tighten verification, never loosen it.
4. **No single-run scores.** Eval output is a distribution (N >= 2).
5. **No time.Now() in business logic.** Time goes through
   `proxy/internal/clock`.
6. **Scope guard.** Read-only, research-only. No order execution, no
   brokerage credentials, no return or Sharpe claims. Ever.
7. **Honest numbers.** Metrics in README or docs must be reproducible
   by a command in the repo. Do not hand-edit a number; regenerate it.
   A known gap stays visible in the report rather than being removed.

## Workflow

- **Branching.** Never commit directly to `main` for non-trivial work;
  use `feat/…`, `fix/…`, `docs/…`, `chore/…` branches.
- **Commit only when asked.** Agents do not commit, push, or open PRs
  unless the maintainer requests it.
- **Commits.** Conventional Commits with a scope:
  `feat(verifier): …`, `fix(proxy): …`, `test(harness): …`,
  `docs: …`, `chore: …`, `ci: …`. Subject in imperative mood, lowercase
  after the colon, no trailing period, <= 72 chars. Body wrapped at 72,
  explaining *why* and what trade-off was made, not restating the diff.
  Reference the design or roadmap section when relevant. One logical
  change per commit; tests land in the same commit as the code.
- **Bugs go through GitHub issues.** A bug found by an audit or while
  working is filed first (English, with a reproduction and the
  expected behavior), then fixed in a PR whose body says `Fixes #N`.
  A fix that is out of scope for the current PR gets its own issue.
- **Merging.** Use merge commits (`gh pr merge --merge`), never squash
  or rebase: docs cite commit hashes, and rewriting them breaks the
  citations. Before merging a PR whose CI ran against an older `main`,
  merge current `main` into it locally and run
  `make build test lint readme-check` (P-007). Merge a stack of PRs
  bottom-up and retarget each dependent to `main` before deleting its
  base branch (P-006).
- **Before declaring done:** tests pass (including `-race` for Go),
  `gofmt -l` is empty, golden log is unchanged or intentionally
  regenerated, and the maintained docs below are updated.

## Engineering standards

**General**
- Write code that reads like the surrounding code: match comment
  density, naming, and error style.
- Comments explain *why* and cite design sections
  (`docs/design.md section 6.3`); do not narrate what the code does.
- Every bug fix starts with a failing test that reproduces it.
- Prefer config over code for new tools (`schemas/*.yaml`).
- Do not add dependencies without a stated reason. The Go proxy is
  stdlib + `yaml.v3` by design.

**Go (`proxy/`)**
- Wrap errors with package context: `fmt.Errorf("store: open: %w", err)`.
- No panics except for unrecoverable states (e.g. `crypto/rand`).
- Table-driven tests; `-update` flag pattern for golden files.
- Concurrency changes must pass `go test -race`.

**Python (`verifier/`, `harness/`)**
- Python >= 3.11, full type hints, `from __future__ import annotations`.
- Frozen dataclasses for data; no mutable module state.
- Raise specific exceptions (`ReceiptError`), never bare `Exception`.
- pytest; tests live in `<package>/tests/`. Warnings are errors.
- ruff (config in `ruff.toml`) and `mypy --strict` over src and tests.

## Maintained documents

These are part of the work, not an afterthought. Update them in the
same change that makes them stale.

| File | Purpose | Update when |
|---|---|---|
| `docs/roadmap.md` | Phased plan with exit criteria | A phase item starts, finishes, or is re-scoped |
| `docs/pitfalls.md` | Known traps: symptom, cause, fix | You hit or discover a non-obvious trap, or fix one |
| `docs/handoff.md` | Session-to-session state | End of every working session |
| `docs/design.md` | Architecture and rationale | Design decisions change; keep its claims true |
| `README.md` | The project's front page | Any user-visible change: behavior, CLI, verdicts, roadmap status, limitations |

`README.md` rules: it is the first thing a reviewer reads, so hold it
to a professional standard. Lead with the problem and a concrete
example, not a feature list. Every claim must be true of the current
code. Measured sections sit between `BEGIN/END GENERATED` markers and
are produced by `make readme`; never edit them by hand (CI runs
`make readme-check`). After any significant change, re-read the whole
README: update the roadmap status table, "Known limitations", and any
example the change affects. Plain, precise English; no hype, no emoji
beyond status marks.

`docs/handoff.md` rules: overwrite the "Current state" and "Next
steps" sections each session (it is a snapshot, not a log); append one
line to "Session log". Keep it under ~150 lines. Record facts: what is
done and verified, what is in progress, what is blocked and why.

`docs/pitfalls.md` rules: one entry per trap, with **Symptom**,
**Cause**, **Fix / workaround**, and **Status** (`open`, `fixed in
<commit>`). Mark whether it was reproduced or found by code reading.
Do not delete fixed entries; mark them fixed so the history teaches.
