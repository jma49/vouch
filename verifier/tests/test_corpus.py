"""Adversarial regression corpus: hand-written agent prose, complete
expected verdict lists. See tests/corpus/claims.yaml for the spec."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from vouch_verifier.claims import extract_claims
from vouch_verifier.matcher import load_tolerances, match_claims
from vouch_verifier.receipts import Fact, Receipt

CORPUS = Path(__file__).parent / "corpus" / "claims.yaml"
TOLERANCES = load_tolerances(Path(__file__).resolve().parents[2] / "tolerance.yaml")

_TOL_CLASS = {
    "rsi_14": "indicator",
    "macd_hist": "indicator",
    "close_price": "price",
    "open_price": "price",
    "last_price": "price",
    "change_pct": "percentage",
    "volume": "count",
}
_UNIT = {"close_price": "USD", "open_price": "USD", "last_price": "USD", "change_pct": "pct"}
_EXPECT_RE = re.compile(
    r"^(?P<text>.+?) -> (?P<verdict>[A-Z]+)(?: \((?P<entity>\S+)(?: (?P<metric>\S+))?\))?$"
)


@dataclass(frozen=True)
class Expected:
    text: str
    verdict: str
    entity: str | None
    metric: str | None


def _receipts(raw: list[dict[str, Any]]) -> list[Receipt]:
    out = []
    for turn, r in enumerate(raw):
        facts = []
        for i, row in enumerate(r["facts"]):
            entity, metric, value = row[0], row[1], row[2]
            as_of = str(row[3]) if len(row) > 3 else str(r["as_of"])
            ptr = f"/bars/{i}/close" if len(row) > 3 else f"/{metric}"
            facts.append(
                Fact(
                    entity=entity,
                    metric=metric,
                    value=float(value),
                    unit=_UNIT.get(metric),
                    as_of=as_of,
                    timeframe="1d",
                    json_ptr=ptr,
                    tol_class=_TOL_CLASS[metric],
                )
            )
        out.append(
            Receipt(
                receipt_id=r["id"],
                session_id="corpus",
                turn_index=turn,
                tool_name="corpus",
                args_canonical="{}",
                result_canonical="{}",
                result_digest="",
                facts=tuple(facts),
                data_asof=None,
                wall_time="2026-07-24T20:00:00Z",
                logical_time=turn,
                upstream_latency_ms=0,
            )
        )
    return out


def _parse_expect(line: str) -> Expected:
    m = _EXPECT_RE.match(line)
    assert m, f"bad expectation syntax: {line!r}"
    return Expected(m["text"], m["verdict"], m["entity"], m["metric"])


def _load() -> tuple[list[Receipt], list[Any]]:
    data = yaml.safe_load(CORPUS.read_text(encoding="utf-8"))
    receipts = _receipts(data["receipts"])
    params = []
    for case in data["cases"]:
        marks = []
        if "xfail" in case:
            marks.append(pytest.mark.xfail(strict=True, reason=str(case["xfail"])))
        expected = [_parse_expect(e) for e in case["expect"]]
        params.append(pytest.param(case["text"], expected, id=case["text"][:60], marks=marks))
    return receipts, params


RECEIPTS, CASES = _load()
ENTITIES = {f.entity for r in RECEIPTS for f in r.facts}


def test_corpus_size() -> None:
    # Roadmap Phase 1 exit criterion.
    assert len(CASES) >= 100


@pytest.mark.parametrize(("text", "expected"), CASES)
def test_corpus_case(text: str, expected: list[Expected]) -> None:
    matched = match_claims(extract_claims(text, ENTITIES), RECEIPTS, TOLERANCES)
    got = [(mc.claim.text.strip(), mc.verdict.value) for mc in matched]
    assert got == [(e.text, e.verdict) for e in expected]
    for mc, e in zip(matched, expected, strict=True):
        if e.entity is not None:
            assert mc.claim.entity == e.entity, f"{e.text}: entity"
        if e.metric is not None:
            assert mc.claim.metric == e.metric, f"{e.text}: metric"
