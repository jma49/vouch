"""Differential test: Go and Python canonical JSON agree on any input.

The shared vectors pin the cases someone thought of; this test runs the
cases nobody did (docs/roadmap.md Phase 4). Every document goes through
both implementations, Python in process and Go through `vouch canon
--lines`, and the two must either produce identical bytes or both
refuse. Inputs come from two sources:

- Hypothesis, generating JSON text that leans on the hard parts of the
  contract (number literals, escapes, surrogates, duplicate keys,
  whitespace, deep nesting) plus byte-level mutations of it, so the
  rejection paths are exercised as hard as the happy one;
- the corpus `go test -fuzz` has built for FuzzCanonicalize, committed
  under proxy/internal/receipt/testdata/fuzz, replayed here.

It needs the Go binary: `make build`, or $VOUCH_BIN. Without it the test
is skipped, and CI runs it in the job that has both toolchains.
$VOUCH_DIFF_EXAMPLES raises the example budget for a longer run.
"""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vouch_verifier import canonicalize

ROOT = Path(__file__).resolve().parents[2]
VOUCH = Path(os.environ.get("VOUCH_BIN", ROOT / "proxy" / "bin" / "vouch"))
GO_CORPUS = ROOT / "proxy" / "internal" / "receipt" / "testdata" / "fuzz" / "FuzzCanonicalize"
EXAMPLES = int(os.environ.get("VOUCH_DIFF_EXAMPLES", "150"))

pytestmark = pytest.mark.skipif(
    not VOUCH.is_file(), reason=f"no Go binary at {VOUCH}; run `make build`"
)


def _python(doc: bytes) -> str | None:
    """Python's canonical bytes in base64, or None if it refuses."""
    try:
        return base64.b64encode(canonicalize(doc).encode("utf-8")).decode("ascii")
    except ValueError:  # includes UnicodeDecodeError
        return None


def _go(docs: list[bytes]) -> list[str | None]:
    """Go's canonical bytes in base64 for each doc, or None if refused."""
    stdin = "".join(base64.b64encode(d).decode("ascii") + "\n" for d in docs)
    out = subprocess.run(
        [str(VOUCH), "canon", "--lines"],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.splitlines()
    assert len(out) == len(docs), "vouch canon --lines lost a line"
    return [None if line.startswith("!") else line for line in out]


def assert_agree(docs: list[bytes]) -> None:
    for doc, go in zip(docs, _go(docs), strict=True):
        py = _python(doc)
        if py != go:
            pytest.fail(
                f"Go and Python disagree on {doc!r}\n    go: {_show(go)}\npython: {_show(py)}"
            )


def _show(result: str | None) -> str:
    return "rejected" if result is None else repr(base64.b64decode(result))


# --- generating JSON text -------------------------------------------------

_WS = st.sampled_from(["", "", "", " ", "\n", "\t", "\r\n", "  "])

_digits = st.text("0123456789", min_size=1, max_size=25)
_int_part = st.one_of(
    st.just("0"),
    st.builds(lambda d, r: d + r, st.sampled_from("123456789"), st.text("0123456789", max_size=25)),
)
_number = st.builds(
    lambda sign, i, frac, exp: sign + i + frac + exp,
    st.sampled_from(["", "", "-"]),
    _int_part,
    st.one_of(st.just(""), _digits.map(lambda d: "." + d)),
    st.one_of(
        st.just(""),
        st.builds(
            lambda e, s, d: e + s + d,
            st.sampled_from("eE"),
            st.sampled_from(["", "+", "-"]),
            _digits,
        ),
    ),
)

# Code points that stress string handling: controls, the escapes, the
# separators Go escapes, noncharacters, the BMP edge, astral planes.
_special_chars = st.sampled_from(
    [
        "\x00",
        "\x07",
        "\b",
        "\t",
        "\n",
        "\x0b",
        "\f",
        "\r",
        "\x1f",
        '"',
        "\\",
        "/",
        "\x7f",
        "\x80",
        "\u00e9",
        "\u2028",
        "\u2029",
        "\ufeff",
        "\ufffd",
        "\uffff",
        "\U0001f600",
        "\U0010ffff",
        "<",
        ">",
        "&",
    ]
)
_char = st.one_of(
    st.characters(codec="utf-8"),  # any scalar value, surrogates excluded
    _special_chars,
)


def _escape(c: str) -> str:
    """One character written as JSON source, chosen among its spellings."""
    code = ord(c)
    if code > 0xFFFF:
        hi, lo = 0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)
        return f"\\u{hi:04x}\\u{lo:04X}"
    return f"\\u{code:04x}"


@st.composite
def _string(draw: st.DrawFn) -> str:
    chars = draw(st.lists(_char, max_size=12))
    out = []
    for c in chars:
        short = {
            '"': '\\"',
            "\\": "\\\\",
            "\b": "\\b",
            "\f": "\\f",
            "\n": "\\n",
            "\r": "\\r",
            "\t": "\\t",
        }
        must_escape = c in short or ord(c) < 0x20
        how = draw(st.sampled_from(["raw", "raw", "u", "short"]))
        if how == "short" and c in short:
            out.append(short[c])
        elif how == "u" or must_escape:
            out.append(_escape(c))
        else:
            out.append(c)
    # Occasionally an escape that decodes to a lone surrogate, or a pair
    # split by other text: both implementations must refuse it.
    if draw(st.integers(0, 20)) == 0:
        lone = draw(st.sampled_from(["\\ud800", "\\udfff", "\\uDBFF\\u0041", "\\udc00\\ud800"]))
        out.insert(draw(st.integers(0, len(out))), lone)
    return '"' + "".join(out) + '"'


def _json_text(max_leaves: int = 20) -> st.SearchStrategy[str]:
    scalar = st.one_of(_number, _string(), st.sampled_from(["true", "false", "null"]))

    def containers(children: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
        array = st.builds(
            lambda ws, items: "[" + ws + ",".join(items) + ws + "]",
            _WS,
            st.lists(st.builds(lambda a, v, b: a + v + b, _WS, children, _WS), max_size=5),
        )

        @st.composite
        def obj(draw: st.DrawFn) -> str:
            keys = draw(st.lists(_string(), max_size=5))
            if keys and draw(st.integers(0, 8)) == 0:
                keys.append(draw(st.sampled_from(keys)))  # a duplicate, spelled the same
            members = [
                draw(_WS) + k + draw(_WS) + ":" + draw(_WS) + draw(children) + draw(_WS)
                for k in keys
            ]
            return "{" + ",".join(members) + draw(_WS) + "}"

        return st.one_of(array, obj())

    return st.builds(
        lambda a, v, b: a + v + b, _WS, st.recursive(scalar, containers, max_leaves=max_leaves), _WS
    )


@st.composite
def _mutated(draw: st.DrawFn) -> bytes:
    """A generated document with a few bytes inserted, removed, or changed."""
    doc = bytearray(draw(_json_text(8)).encode("utf-8"))
    for _ in range(draw(st.integers(1, 3))):
        pos = draw(st.integers(0, len(doc)))
        op = draw(st.sampled_from(["insert", "delete", "replace"]))
        byte = draw(st.sampled_from(b'{}[],:"\\-+.eE0 \x00\xff\xc3\xed' + b"tfnu"))
        if op == "insert":
            doc[pos:pos] = bytes([byte])
        elif pos < len(doc):
            doc[pos : pos + 1] = b"" if op == "delete" else bytes([byte])
    return bytes(doc)


_document = st.one_of(
    _json_text().map(lambda s: s.encode("utf-8")),
    _mutated(),
    st.binary(max_size=40),
)


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(_document, min_size=1, max_size=40))
def test_go_and_python_agree_on_generated_documents(docs: list[bytes]) -> None:
    assert_agree(docs)


# --- replaying the Go fuzz corpus -----------------------------------------


def _go_bytes_literal(src: str) -> bytes:
    """Decode the Go string literal inside a corpus line `[]byte("...")`.

    Go writes it with %q: printable characters as themselves, the usual
    short escapes, \\xNN for bytes that are not UTF-8, and \\uNNNN or
    \\UNNNNNNNN for other characters.
    """
    assert src.startswith('[]byte("') and src.endswith('")'), src
    body, out, i = src[8:-2], bytearray(), 0
    short = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, "\\": 92, '"': 34, "'": 39}
    while i < len(body):
        c = body[i]
        if c != "\\":
            out += c.encode("utf-8")
            i += 1
            continue
        e = body[i + 1]
        if e in short:
            out.append(short[e])
            i += 2
        elif e == "x":
            out.append(int(body[i + 2 : i + 4], 16))
            i += 4
        elif e in "uU":
            n = 4 if e == "u" else 8
            out += chr(int(body[i + 2 : i + 2 + n], 16)).encode("utf-8")
            i += 2 + n
        elif e in "01234567":
            out.append(int(body[i + 1 : i + 4], 8))
            i += 4
        else:
            raise ValueError(f"unknown escape \\{e} in corpus entry")
    return bytes(out)


def _corpus() -> list[bytes]:
    docs = []
    for path in sorted(GO_CORPUS.glob("*")):
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "go test fuzz v1", path
        docs.append(_go_bytes_literal(lines[1]))
    return docs


def test_go_bytes_literal_decoding() -> None:
    assert _go_bytes_literal('[]byte("a\\x00\\xff\\u2028\\U0001f600\\"\\\\é")') == (
        b"a\x00\xff" + "\u2028\U0001f600".encode() + b'"\\' + "é".encode()
    )


def test_go_and_python_agree_on_the_go_fuzz_corpus() -> None:
    docs = _corpus()
    assert docs, f"no corpus under {GO_CORPUS}"
    assert_agree(docs)
