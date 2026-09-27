# vouch canonical JSON, version 2

The byte form vouch uses for every JSON value it digests: tool
arguments, tool results, the full response an agent received, and the
receipt body itself. Two implementations exist, the Go proxy
(`proxy/internal/receipt/canonical.go`, which writes receipts) and the
Python verifier (`verifier/src/vouch_verifier/canonical.py`, which
reads them). They must agree byte for byte, and
`testdata/canonical_vectors.json` is the normative test suite: both
implementations run every vector in CI.

## Why not RFC 8785 (JCS)

JCS parses every number as an IEEE 754 double and re-serializes it.
That is the right choice for signing data you author, and the wrong
one for notarizing data someone else authored. A receipt must record
exactly what a tool returned (design.md section 3.1, issue #20). Under
JCS, `12345678901234567890` becomes `12345678901234567000`, an
18-decimal token amount loses digits, and `181.50` loses the trailing
zero that states the upstream's precision. The receipt would then
attest to numbers the agent never saw.

Third-party verification does not need a standard canonicalization:
signatures cover exact payload bytes (DSSE envelopes, issue #53), so a
verifier needs only an Ed25519 library. Canonicalization exists to make
vouch's own digests reproducible, and this page specifies it.

The full reasoning and its costs are recorded in docs/handoff.md,
"Decisions and trade-offs".

## Rules

A canonical document is produced from a parsed JSON value as follows.

1. **Input.** Exactly one JSON value (RFC 8259), with optional
   surrounding whitespace and nothing after it. The input is **rejected**,
   not repaired, if it contains:
   - invalid UTF-8;
   - a `\u` escape that encodes a lone surrogate (an unpaired
     `\uD800`-`\uDFFF`);
   - two members of one object whose names are equal after unescaping
     (`"a"` and `"\u0061"` collide);
   - arrays and objects nested more than 256 levels deep (`[[0]]` is
     two levels). Implementations disagree on deep input otherwise:
     Go's decoder stops at 10000 levels, CPython's recursion near 1000
     (#62). A tool's arguments and response sit one level down in a
     receipt body, so a call whose response nests 256 levels fails
     rather than going unreceipted (invariant 2).

   Repairing the first three would change what the receiver of the
   original document saw. Nothing after the value is allowed, not even
   a stray `}` or `]` (#61).
2. **Whitespace.** None outside strings.
3. **Literals.** `true`, `false`, `null`.
4. **Numbers.** Copied **exactly** as written in the input, including
   exponent case and sign (`1E+2`), trailing zeros (`1.50`), negative
   zero (`-0`), and digits beyond double precision. The number is never
   parsed into a machine type for output.
5. **Strings.** Enclosed in `"`. The following are escaped:
   - `"` as `\"` and `\` as `\\`;
   - U+0008, U+0009, U+000A, U+000C, U+000D as `\b`, `\t`, `\n`, `\f`,
     `\r`;
   - every other code point below U+0020 as `\u00XX` (lowercase hex);
   - U+2028 and U+2029 as `\u2028` and `\u2029`.

   Every other code point, including U+007F, non-ASCII characters, and
   characters outside the Basic Multilingual Plane, is written as its
   UTF-8 encoding. `/` is not escaped. Escapes in the input are decoded
   first, so `"é"` and `"\u00e9"` produce the same output.
6. **Arrays.** Elements in input order, separated by `,`.
7. **Objects.** Members sorted by name, compared as sequences of
   **Unicode code points** (equivalently, as UTF-8 byte strings),
   written as `name:value` separated by `,`. This differs from JCS,
   which compares UTF-16 code units; the two orders disagree only when
   one name has a character in U+E000-U+FFFF where the other has a
   character above U+FFFF at the same position.
8. **Output encoding.** UTF-8, no byte-order mark.

## Consequences worth knowing

- Two documents that are equal as JSON values can have different
  canonical forms when their numbers are written differently (`62.3`
  and `62.30`). That is intended: they are different documents, and
  either may be what an agent saw.
- Where semantic equality is what matters, the caller normalizes before
  canonicalizing. Fixture replay keys are the case for this today:
  `"limit": 5` and `"limit": 5.0` from an agent are the same request,
  so the key spells every number as `<digits>e<exponent>` before
  hashing (`5`, `5.0`, and `50e-1` all become `5e0`), exactly and
  without going through a double (#64).
- The rules are a strict subset of what JSON permits, so a canonical
  document is valid JSON and canonicalizing it again returns it
  unchanged.

## Versioning

A change to any rule is a new version. The receipt payload type
(issue #53) names the receipt format version, and a change here
requires a new receipt version, regenerated golden data, and new
vectors, all in one change.

| Version | Change | Payload types |
|---|---|---|
| 1 | Initial specification (issue #52) | receipt `version=3`, checkpoint `version=1` |
| 2 | Nesting limit of 256 levels (#62). Canonical bytes of every document accepted by both versions are unchanged; v2 only refuses more. | receipt `version=4`, checkpoint `version=2` |

Rejecting a stray `}` or `]` after the value (#61) is not a version
change: rule 1 always required it, and the Go implementation was wrong.
