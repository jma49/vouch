# Threat model

What vouch's receipts protect, against whom, and what they do not.
Every property claimed here is pinned by a test; every limit is stated
so that nobody relies on a guarantee vouch does not give.

## What vouch attests

A verified receipt log says one thing: **this proxy, holding this
signing key, saw these tool calls return these results, in this order.**
It does not say the results were true, that the proxy was honest, or
that the agent used only these tools. Every guarantee below is relative
to that statement.

## Assets

| Asset | Why it matters |
|---|---|
| Receipts | The evidence a verdict rests on: what each tool returned |
| The signing private key | Whoever holds it can produce receipts others will trust |
| Head digests | The only defence against a log cut back to an earlier point |
| Verdict reports and eval results | What people act on and publish |
| Human labels | The ground truth for the real evaluation |

## Actors

| Actor | Holds | Trusted for |
|---|---|---|
| Agent (the LLM and its host) | nothing of vouch's | nothing: it is what vouch audits |
| Upstream MCP server | nothing of vouch's | nothing: its answers are recorded, not believed |
| Proxy operator | the signing private key | running the proxy honestly |
| Log holder (storage, transport, a colleague) | the log file | nothing |
| Verifier / third-party auditor | public keys, optionally a head digest | checking; they need no secret |
| Labeler | a label file | labeling honestly (agreement is measured) |

## Properties provided

| # | Property | Against | Mechanism | Pinned by |
|---|---|---|---|---|
| P1 | No receipt can be forged or edited without the private key | log holder | Ed25519 signature over the exact payload bytes, in a DSSE envelope (#53) | tamper suites: edit-fact, unknown-key, swap-sig; `sign` package tests |
| P2 | No entry can be deleted, inserted, duplicated, or reordered without breaking the log | log holder | hash chain: signed `seq` and `prev_digest` on every entry, checked with or without keys (#54) | tamper suites: delete-first, delete-middle, reorder, duplicate-line |
| P3 | A cleanly ended session is sealed, and a log that stops mid-session is visible | log holder, crashes | signed checkpoint with the receipt count; `--require-sealed` (#54) | tamper suites: miscount-checkpoint, truncate-tail |
| P4 | A log cut back to an earlier point is detected, **given an external head** | log holder | head digest printed at seal, recorded in eval runs' `meta.json`; `--expect-head` (#54) | `test_tail_truncation_needs_sealing_or_an_external_head`; `test_a_run_whose_log_no_longer_matches_its_head_is_refused` |
| P5 | The receipt covers what the agent actually received, not only the extracted payload | a misleading upstream response | `response_canonical` and its digest, signed (#20) | `TestReceiptBindsTheResponse`; `test_tampered_response_is_rejected_without_a_key` |
| P6 | Numbers are recorded exactly as the tool wrote them; ambiguous input is refused | canonicalization drift | vouch canonical JSON v2: literals verbatim; duplicate keys, lone surrogates, and nesting past 256 levels rejected in both languages (#9, #27, #52, #62) | `testdata/canonical_vectors.json` in Go and Python; differential fuzzing (`test_differential.py`, `FuzzCanonicalize`) |
| P7 | A (session, turn) is recorded once | replay within a log | uniqueness checked by the store and the verifier | duplicate tests, including a validly signed replay |
| P8 | Anyone can verify, without a secret and without vouch's code | n/a (a capability) | public keys; DSSE verification needs only base64 and Ed25519 | cross-language golden log, checked by Go and Python in CI |

## Not protected

These are limits of the design, not bugs. Several are unavoidable for
any system of this shape; the rest are open work.

1. **A dishonest proxy operator.** Whoever holds the signing key can
   sign anything: invented receipts, omitted calls, a whole fabricated
   log with a valid chain. vouch attests what the proxy saw; trusting
   that requires trusting the operator. Keep the operator separate from
   whoever builds the agent under test, and keep the key on the proxy
   host only (the proxy refuses a key other users can read).
2. **An upstream that lies.** A receipt records what the upstream
   returned, not whether it was correct. vouch checks that the agent
   reported its tools faithfully, not that the tools were right.
3. **Tail truncation with no external head.** A prefix of a valid
   chain is a valid chain. Cutting a log back to an earlier checkpoint
   is undetectable from the log alone. Keep the head the proxy prints,
   or publish it; for eval runs it is committed with the run.
4. **Calls that bypass the proxy.** If the agent has another route to
   data, receipts cannot show it. Claims based on such data come out
   UNSUPPORTED, which is the right verdict but not proof of a bypass.
5. **Substituting a whole log.** A different, validly signed log (say,
   another session's) verifies on its own. Bind the log you expect with
   `--expect-head` or by session id.
6. **Key compromise and revocation.** There is no revocation list. A
   verifier trusts whatever keys it is given; after a compromise, drop
   the key from every keyring and treat its receipts as unverified.
7. **Time.** `wall_time` and `sealed_at` are the proxy's clock. They
   are signed, so nobody else can change them, but the proxy's clock is
   not trusted time.
8. **Confidentiality.** Receipts are plaintext and contain tool
   arguments and results. Protect logs as you would the data they hold.
9. **Verdict correctness.** Extraction and matching are heuristics
   (docs/handoff.md, "Verifier heuristics"). A verdict can be wrong;
   how often is measured against human labels (Phase 2), not
   guaranteed.
10. **Prompt injection through tool results.** vouch records and
    audits; it does not sanitize what the model reads.
11. **The committed test keys.** `testdata/keys` holds private keys on
    purpose (testdata/keys/README.md). Receipts signed with them prove
    the integrity of published test and eval data, never authorship.

## Assumptions

- Ed25519 and SHA-256 are secure.
- The proxy host is not compromised (see limit 1).
- Verifiers obtain public keys, and head digests where used, through a
  channel the attacker does not control.

## Operating guidance

- Generate a key per proxy deployment with `vouch keygen`; keep the
  private key on that host, mode 0600; distribute only the public key.
- When a session ends, keep the head digest the proxy prints, or
  publish it with the results it supports.
- Audit with the strongest checks available:
  `vouch receipts verify --require-sealed --expect-head <head> --public-key <key> <log>`,
  or `vouch-verify` with the same options.
- Rotate keys by adding the new public key to verifiers' keyrings
  before switching the proxy; keyrings accept any trusted signature.
- Keep `--listen` on a loopback address. The HTTP endpoint checks
  browser origins (against DNS rebinding) but has no authentication:
  anyone who can reach it can make calls that get receipted. Pass
  upstream credentials as `env:VAR`, not literally, so they stay out of
  the process list.
