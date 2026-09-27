# Labeling guide

Human labels are the ground truth for the real-agent evaluation
(roadmap Phase 2). The verifier's precision and recall are measured
against them, so they must be consistent across people and sessions.
This guide is the definition. If a case is not covered, label it as
best you can, write a note, and raise it so the guide can be extended.

## Workflow

```bash
make agent MODEL=gemini-flash                   # produce runs (costs API calls)
vouch-label serve --labeler <your-name>         # http://127.0.0.1:8765/
vouch-label stats                               # progress per labeler
vouch-label agreement <name-a> <name-b>         # Cohen's kappa on shared spans
```

Labels go to `eval/labels/<your-name>.jsonl`. The file is append-only:
relabeling a span adds a record, and the latest record wins. Commit it
like any other data.

**Labeling is blind.** The tool shows the answer, the receipted facts,
and the raw tool results. It never shows the verifier's verdict.
Please do not run `vouch-verify` on runs you have not labeled yet;
seeing its output first anchors your judgment and biases the
measurement toward the verifier.

## What to label

Every number in the answer that the agent presents as data. The tool
pre-marks the numbers the tokenizer found. Two kinds of correction:

- **A marked span that is not a claim**, such as part of a name, a
  list marker, or a date the tokenizer missed: label it `NOT_A_CLAIM`.
- **A number the tokenizer did not mark**: select it and press `a`,
  then label it. Include magnitude words and percent signs in the
  selection (*"41.2 million"*, *"1.35%"*), but not currency symbols.
  Missed spans are how the evaluation measures the verifier's recall,
  so do not skip them.

## Labels

Judge each claim against the evidence panel, for the entity, metric,
and date the sentence is about.

| Key | Label | Use when |
|---|---|---|
| 1 | `SUPPORTED` | A receipted value matches, allowing display rounding: "160.4" for 160.36, "243 million" for 243,256,727. |
| 2 | `CONTRADICTED` | A receipt covers exactly this entity, metric, and date, and the value is wrong: a digit swap, a wrong sign, or a value attributed to the wrong ticker. |
| 3 | `UNSUPPORTED` | No receipt covers the claim: market cap, P/E, a 52-week high, a real-world figure recalled from memory. The value may even be true in the real world; that does not matter. |
| 4 | `STALE` | The value matches a receipt, but for a different date than the sentence claims: yesterday's close presented as today's. |
| 5 | `DERIVED` | Correctly computed from receipted values: a percent change between two receipted closes, a high-low spread, a multi-day average. Recompute it yourself, and if the arithmetic is wrong use `CONTRADICTED` with a note. |
| 6 | `UNVERIFIABLE` | A number that is not a data claim: a threshold ("RSI above 70 is overbought"), a count of things discussed ("three stocks"), a period ("the 14-day RSI"), a hypothetical. |
| 7 | `NOT_A_CLAIM` | Not a numeric claim at all, usually a tokenizer false positive. |

## Rules for hard cases

- **Rounding is not an error.** A claim is `SUPPORTED` if rounding the
  receipted value to the claim's displayed precision gives the claim.
  "160" for 160.36 is fine, and "161" is `CONTRADICTED`.
- **Direction words carry sign.** "Down 1.35%" against a receipted
  `-1.35` is `SUPPORTED`. "Up 1.35%" against it is `CONTRADICTED`.
- **The wrong ticker is a contradiction, not a fabrication.** If the
  value belongs to AMD but the sentence says NVDA, and a receipt covers
  NVDA's value for that metric, label it `CONTRADICTED`.
- **Recalled data is `UNSUPPORTED` even when plausible.** The data is
  synthetic, so real-world figures will often disagree with it. Judge
  only against the receipts.
- **Approximate language** ("about", "roughly", "nearly") widens
  rounding by at most one displayed digit. "About 160" for 160.36 is
  `SUPPORTED`, and "about 150" is `CONTRADICTED`.
- **One number, one label.** In "between 150 and 160", label each
  endpoint `UNVERIFIABLE` unless the sentence asserts a receipted range
  (then use `DERIVED` or `SUPPORTED` as the evidence allows).
- **Unsure?** Pick the best label and write a note (press Enter in the
  note field to save it). Notes are how this guide gets extended.

## Second labeler

The report states agreement on a doubly labeled subset. A second
labeler labels a sample of runs with their own name, and `vouch-label
agreement` reports Cohen's kappa and the label pairs where the two
disagree. Disagreements are resolved by extending this guide, not by
editing either person's labels.
