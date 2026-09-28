# vouch receipt log format

This page specifies the receipt log the proxy writes, closely enough
that someone holding only this page, a log, and the signer's public key
can write a verifier. Two implementations exist: the Go proxy
(`proxy/internal/receipt`, `proxy/internal/store`: it writes logs and
checks them with `vouch receipts verify`) and the Python verifier
(`verifier/src/vouch_verifier/receipts.py`, `signing.py`). Both follow
this page, and tests on both sides read the field tables below, so the
page cannot drift from the code without failing CI.

The key words MUST, MUST NOT, and MAY are used as in RFC 2119.

Current versions: receipt payload `version=4`, checkpoint payload
`version=2`, [canonical JSON](canonical-json.md) version 2.

## The log

A log is a UTF-8 text file of lines separated by `\n`. Each non-blank
line is one **entry**: a DSSE envelope (below) holding either a receipt
(one tool call) or a checkpoint (a seal). Entries are appended and never
rewritten. Writers MUST NOT emit blank lines or a byte-order mark;
readers skip blank lines and MAY skip a byte-order mark at the start
of the file.

The proxy writes the log as `receipts.jsonl` in its `--receipts`
directory, owner-only (mode 0600), with one writer at a time.

## Envelope

Each line is a JSON object with exactly these keys, and a verifier
MUST reject a line with any other key, including one that differs only
in case:

```json
{"payload":"<base64>","payloadType":"application/vnd.vouch.receipt+json; version=4","signatures":[{"keyid":"ed25519:8bdc9a88e7cf36a6","sig":"<base64>"}]}
```

- `payload`: standard base64 (RFC 4648 section 4, with padding) of the
  body bytes.
- `payloadType`: exactly one of
  - `application/vnd.vouch.receipt+json; version=4` (a receipt),
  - `application/vnd.vouch.checkpoint+json; version=2` (a checkpoint).

  Any other value, including an earlier version, MUST be rejected.
  Older versions are not read; see [Versioning](#versioning).
- `signatures`: an array of objects with exactly the keys `keyid` and
  `sig`. `sig` is the standard base64 of a 64-byte Ed25519 signature
  over the DSSE pre-authentication encoding:

  ```
  PAE(type, body) = "DSSEv1" SP len(type) SP type SP len(body) SP body
  ```

  where `len` is the decimal byte length and `SP` a single space. The
  signature covers the exact payload bytes, so checking it needs only
  base64 and an Ed25519 library, never a canonicalizer.
  `keyid` is `ed25519:` followed by the first 16 lowercase hex digits
  of SHA-256 over the raw 32-byte public key.

The line itself is JSON under the canonical-JSON input rules (no
duplicate keys, no lone surrogates, nesting at most 256 levels, nothing
after the value), but it need not be in canonical form.

## Bodies

A body (the decoded payload) MUST be a JSON object in
[canonical form](canonical-json.md): the payload bytes equal their own
canonicalization. A verifier MUST reject a body that is not (#124);
every digest inside a body is then over one byte string, the same in
every reader.

In a body, a key that differs from a key listed below only in case
MUST be rejected (#98), a key marked required MUST be present (#124),
and a key not listed is ignored (it is still covered by the
signature). Digests are `sha256:` followed by 64 lowercase hex digits,
over the canonical bytes of the value named. Times are RFC 3339 in UTC.

### Receipt body

One per `tools/call` the proxy forwarded.

| Key | Type | Required | Meaning |
|---|---|---|---|
| `seq` | integer | yes | The entry's position in the log, from 0 |
| `prev_digest` | digest | yes | Digest of the previous entry's payload bytes, or the genesis digest for the first entry |
| `receipt_id` | string | yes | 32 random hex digits, unique in the log |
| `session_id` | string | yes | The proxy session the call belongs to |
| `turn_index` | integer | yes | The call's position within its session; (`session_id`, `turn_index`) is unique in the log |
| `tool_name` | string | yes | The tool the agent called |
| `args_canonical` | any JSON | yes | The call's arguments, canonicalized |
| `result_canonical` | any JSON | yes | The payload facts were extracted from, canonicalized |
| `result_digest` | digest | yes | Digest over `result_canonical` |
| `payload_source` | string | yes | Where the payload was found in the result: `structuredContent`, `content/<i>/text`, or `result` |
| `response_canonical` | any JSON | yes | The whole `tools/call` result the agent received, canonicalized |
| `response_digest` | digest | yes | Digest over `response_canonical` |
| `facts` | array of Fact, or null | yes | Values extracted at write time by the tool's schema; null or empty when the tool has no schema or returned an error |
| `data_asof` | string | no | Timestamp of the data, from the result, when the schema names one |
| `wall_time` | time | yes | When the proxy wrote the receipt |
| `logical_time` | integer | yes | Tick of the proxy's injected clock (deterministic under replay) |
| `upstream_latency_ms` | integer | yes | Time the upstream took to answer |

### Fact

An element of a receipt's `facts`.

| Key | Type | Required | Meaning |
|---|---|---|---|
| `entity` | string | yes | What the value is about (a ticker, a row key) |
| `metric` | string | yes | What the value measures |
| `value` | number | yes | The value, as the tool returned it |
| `unit` | string | no | Its unit, when the schema states one |
| `as_of` | string | no | The date of this value, when the schema names one |
| `timeframe` | string | no | The bar size or period, when the schema states one |
| `json_ptr` | string | yes | RFC 6901 pointer to the value inside `result_canonical` |
| `tol_class` | string | yes | The tolerance class the verifier compares it under |

### Checkpoint body

Appended when a session ends cleanly, sealing the log up to that point.

| Key | Type | Required | Meaning |
|---|---|---|---|
| `seq` | integer | yes | The entry's position in the log, from 0 |
| `prev_digest` | digest | yes | Digest of the previous entry's payload bytes |
| `receipts` | integer | yes | How many receipts precede it in the log |
| `session_id` | string | yes | The session it seals |
| `sealed_at` | time | yes | When it was written |

## The chain

The genesis digest is
`sha256:0000000000000000000000000000000000000000000000000000000000000000`
(64 zeros). Entry *n* (from
0) has `seq` = *n* and `prev_digest` = the digest of entry *n*-1's
payload bytes (the genesis digest for *n* = 0). The **head** of a log
is the digest of its last entry's payload, or the genesis digest for an
empty log. A log is **sealed** when its last entry is a checkpoint.

Deleting, inserting, or reordering entries breaks the chain for every
later entry. Cutting entries off the end does not: a prefix of a valid
log is a valid log. Only a head kept outside the log (`vouch receipts
head`, then `--expect-head`) or a requirement that the log be sealed
detects it (threat model, P4).

## Verification

Given a log and a set of trusted public keys (possibly empty), a
verifier processes entries in order, keeping a running `head` (initially
the genesis digest), a receipt count, and the sets of receipt ids and
(`session_id`, `turn_index`) pairs seen. For each entry it MUST:

1. Parse the line as JSON under the canonical-JSON input rules, and
   check the envelope's keys and `payloadType` as specified above.
2. Decode `payload` from base64.
3. If keys were given: accept the entry only if some signature's
   `keyid` names a trusted key and its `sig` verifies over
   `PAE(payloadType, payload)`. Signatures under unknown key ids are
   ignored, so logs survive key rotation. Without keys, signatures are
   not checked, and the verifier MUST say so to its user.
4. Check that the payload is in canonical form, then parse it and check
   keys as in [Bodies](#bodies).
5. Check `seq` equals the entry's position and `prev_digest` equals
   `head`; then set `head` to the digest of the payload bytes.
6. For a checkpoint: check `receipts` equals the number of receipts so
   far.
7. For a receipt: check `result_digest` and `response_digest` against
   the values they cover, and that `receipt_id` and (`session_id`,
   `turn_index`) have not been seen before.

Any failure rejects the whole log: a partially trusted log is not a
thing. After the last entry, a verifier MAY additionally require the
log to be sealed (`--require-sealed`) and its head to equal a digest
kept elsewhere (`--expect-head`).

## Conformance

A verifier conforms if, with the public key `testdata/keys/golden.pub.pem`:

- it accepts `testdata/receipts_golden.jsonl` (three receipts and a
  checkpoint; head as `vouch receipts head` prints it), and
- it rejects every altered log in the tamper suites:
  `proxy/internal/store/tamper_test.go` (`TestVerifyDetectsTampering`)
  and `verifier/tests/test_tamper.py`, which build each case from the
  golden log.

`testdata/canonical_vectors.json` is the conformance suite for
canonical JSON.

## Versioning

Any change to a body's keys or their meaning, or to canonical JSON,
is a new payload version, with regenerated golden data, in one change.
Verifiers read the current versions only and reject earlier ones: a
log is verified by the version of vouch that wrote it. Whether to
promise reading older versions to third parties is an open decision
(docs/handoff.md, Open questions).

| Receipt | Checkpoint | Change |
|---|---|---|
| `version=2` | none | DSSE envelopes signed with Ed25519, replacing HMAC (#53) |
| `version=3` | `version=1` | Hash chain and checkpoints (#54); canonical JSON v1 (#52) |
| `version=4` | `version=2` | Canonical JSON v2: nesting limit (#62) |
