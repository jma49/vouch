"""HTML verdict report (#86): spans marked by verdict, all input escaped."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from envelopes import GOLDEN, GOLDEN_KEYS

from vouch_verifier.claims import extract_claims
from vouch_verifier.cli import main
from vouch_verifier.lookahead import LookAhead
from vouch_verifier.matcher import DEFAULT_TOLERANCES, match_claims
from vouch_verifier.receipts import load_log
from vouch_verifier.report import build_report, to_html


class _Scan(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.attrs: list[tuple[str, str | None]] = []
        self.marks: list[str] = []
        self._in_mark = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.attrs += attrs
        if tag == "mark":
            self._in_mark += 1
            self.marks.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "mark":
            self._in_mark -= 1

    def handle_data(self, data: str) -> None:
        if self._in_mark:
            self.marks[-1] += data


def render(answer: str, **kw: object) -> str:
    receipts = load_log(GOLDEN, GOLDEN_KEYS)
    extraction = extract_claims(answer, {"NVDA", "AMD"})
    matched = match_claims(extraction, receipts, DEFAULT_TOLERANCES)
    return to_html(build_report(extraction, matched, DEFAULT_TOLERANCES, answer=answer, **kw))  # type: ignore[arg-type]


def test_every_claim_span_is_marked_with_its_verdict() -> None:
    page = render("NVDA closed at 181.52. AMD last traded at 172.40.")
    assert 'class="v-SUPPORTED"' in page and 'class="v-CONTRADICTED"' in page
    scan = _Scan()
    scan.feed(page)
    answer_marks = [m for m in scan.marks if m in {"181.52", "172.40"}]
    assert answer_marks == ["181.52", "172.40"]
    # Each mark links to its row in the claims table.
    for anchor in re.findall(r'href="#(c\d+)"', page):
        assert f'id="{anchor}"' in page


def test_untrusted_text_is_escaped_and_the_page_is_inert() -> None:
    evil = (
        '<script>alert(1)</script> "x" onload=1 NVDA closed at 181.52 <img src=x onerror=alert(2)>'
    )
    page = render(evil)
    assert "<script>alert" not in page and "<img" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    scan = _Scan()
    scan.feed(page)
    assert "script" not in scan.tags and "img" not in scan.tags and "link" not in scan.tags
    assert not any(name.startswith("on") for name, _ in scan.attrs)
    assert not any(v and re.match(r"(https?:|//)", v) for _, v in scan.attrs)


def test_lookahead_section_appears_only_under_as_of() -> None:
    assert "Look-ahead" not in render("NVDA closed at 181.52.")
    page = render(
        "NVDA closed at 181.52.",
        as_of="2026-07-20",
        lookahead=[LookAhead("golden-0", "get_indicators", "2026-07-24T20:00:00Z")],
    )
    assert "Look-ahead" in page and "golden-0" in page


def test_cli_writes_html(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    answer = tmp_path / "a.txt"
    answer.write_text("NVDA closed at 181.52.\n")
    assert main(["--answer", str(answer), "--receipts", str(GOLDEN), "--format", "html"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("<!doctype html>") and "v-SUPPORTED" in out
