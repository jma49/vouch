"""Regressions from the 2026-09-27 audit, #96: look-ahead holes, exit 2
for malformed input, strict canonical parsing, report hardening, signs."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from envelopes import GOLDEN, bodies, golden_lines, signed_chain, write_log

from vouch_verifier.canonical import canonicalize, parse_preserving
from vouch_verifier.claims import extract_claims
from vouch_verifier.cli import main
from vouch_verifier.lookahead import find_lookahead, parse_moment
from vouch_verifier.matcher import match_claims
from vouch_verifier.receipts import Fact, Receipt
from vouch_verifier.report import build_report, md_cell, to_html, to_json, to_markdown
from vouch_verifier.tokens import tokenize
from vouch_verifier.verdict import Verdict
from vouch_verifier.vocabulary import FINANCE


def _quote(as_of: str | None, data_asof: str | None, wall: str = "2026-07-20T21:00:00Z") -> Receipt:
    fact = Fact("NVDA", "close_price", 181.52, "USD", as_of, None, "/close", "price")
    return Receipt(
        receipt_id="q", session_id="q", turn_index=0, tool_name="get_quote", args_canonical="{}",
        result_canonical="{}", result_digest="", facts=(fact,), data_asof=data_asof,
        wall_time=wall, logical_time=0, upstream_latency_ms=0,
    )  # fmt: skip


AS_OF = parse_moment("2026-07-20")


@pytest.mark.parametrize("stamp", ["07/25/2026 16:00", "1753473600", "Jul 25, 2026"])
def test_an_unreadable_date_is_look_ahead(stamp: str) -> None:
    receipts = [_quote(None, stamp)]
    assert find_lookahead(receipts, AS_OF)
    [mc] = match_claims(extract_claims("NVDA closed at 181.52.", {"NVDA"}), receipts, as_of=AS_OF)
    assert mc.verdict is Verdict.STALE and "look-ahead" in mc.note


def test_undated_facts_are_dated_the_same_way_everywhere() -> None:
    # No as_of and no data_asof: both checks fall back to the call time.
    receipts = [_quote(None, None, wall="2026-07-24T21:00:00Z")]
    assert find_lookahead(receipts, AS_OF)
    [mc] = match_claims(extract_claims("NVDA closed at 181.52.", {"NVDA"}), receipts, as_of=AS_OF)
    assert mc.verdict is Verdict.STALE


@pytest.mark.parametrize(
    ("flag", "content"),
    [
        ("--tolerances", "price: [unclosed\n"),
        ("--tolerances", "- a\n- b\n"),
        ("--tolerances", "price: {abs: '0.01'}\n"),
        ("--tolerances", "price: {abs: -1}\n"),
        ("--vocabulary", "synonyms: [unclosed\n"),
    ],
)
def test_bad_config_files_exit_2(tmp_path: Path, flag: str, content: str) -> None:
    config = tmp_path / "c.yaml"
    config.write_text(content)
    answer = tmp_path / "a.txt"
    answer.write_text("NVDA closed at 181.52.\n")
    assert main(["--answer", str(answer), "--receipts", str(GOLDEN), flag, str(config)]) == 2


@pytest.mark.parametrize(
    "fact_edit",
    [{"entity": 7}, {"metric": None}, {"unit": {}}, {"json_ptr": ["/x"]}, {"as_of": 5}],
)
def test_facts_of_the_wrong_type_exit_2(tmp_path: Path, fact_edit: dict[str, object]) -> None:
    kind, body = bodies(golden_lines()[:1])[0]
    tree = json.loads(body)
    tree["facts"][0].update(fact_edit)
    log = write_log(tmp_path / "r.jsonl", signed_chain([(kind, tree)]))
    answer = tmp_path / "a.txt"
    answer.write_text("NVDA closed at 181.52.\n")
    assert main(["--answer", str(answer), "--receipts", str(log)]) == 2


@pytest.mark.parametrize(
    "doc", ["[NaN]", '{"x": Infinity}', "-Infinity", '{"e": "\\ud800"}', '{"\\udc00": 1}']
)
def test_canonical_parsing_refuses_what_go_refuses(doc: str) -> None:
    with pytest.raises(ValueError):
        parse_preserving(doc)
    with pytest.raises(ValueError):
        canonicalize(doc)


def test_reports_say_when_signatures_went_unchecked() -> None:
    extraction = extract_claims("NVDA closed at 181.52.", {"NVDA"})
    report = build_report(extraction, [], {}, signatures_verified=False)
    assert "Signatures: not checked" in to_markdown(report)
    assert json.loads(to_json(report))["summary"]["signatures_verified"] is False
    assert "Signatures not checked" in to_html(report)
    assert "not checked" not in to_markdown(build_report(extraction, [], {}))


def test_markdown_cells_do_not_render_links() -> None:
    cell = md_cell("![x](https://t.example/p.png) [y](https://t.example)")
    assert re.search(r"(?<!\\)[\[\]!]", cell) is None, cell


def test_signs_around_currency_and_direction_nouns() -> None:
    [neg] = tokenize("net income was -$1,200.")
    assert (neg.value, neg.unit) == (-1200.0, "USD")
    [paren] = tokenize("net income was ($1,200).")
    assert paren.parenthesized and paren.unit == "USD"
    [pct] = tokenize("down (1.35)% today")
    assert pct.unit == "pct" and pct.parenthesized
    [drop] = extract_claims("AMD's drop of 1.35% hurt.", {"AMD"}).claims
    assert drop.value == -1.35
    denied = extract_claims("AMD didn't fall 1.35% today.", {"AMD"}, FINANCE)
    assert not denied.claims and [u.text for u in denied.unresolved] == ["1.35%"]
