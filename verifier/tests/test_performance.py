"""Extraction scales linearly with answer length (issue #19).

The test compares two lengths rather than asserting absolute times, so
it holds on slow CI machines: a 4x longer answer must take well under
16x as long (quadratic), with generous room above linear's 4x.
"""

from __future__ import annotations

import time

from vouch_verifier.claims import clear_caches, extract_claims

ENTITIES = {"NVDA", "AMD", "AAPL", "MSFT"}


def _answer(sentences: int) -> str:
    tickers = sorted(ENTITIES)
    return " ".join(
        f"{tickers[i % 4]} closed at ${100 + i % 50}.{i % 97:02d} and RSI is {40 + i % 30}."
        for i in range(sentences)
    )


def _best_of(runs: int, text: str, entities: set[str] = ENTITIES) -> float:
    best = float("inf")
    for _ in range(runs):
        clear_caches()  # a warm cache would hide the work
        start = time.perf_counter()
        extract_claims(text, entities)
        best = min(best, time.perf_counter() - start)
    return best


def test_extraction_scales_linearly() -> None:
    short, long = _answer(300), _answer(1200)
    ratio = _best_of(3, long) / _best_of(3, short)
    assert ratio < 9, f"4x longer answer took {ratio:.1f}x as long"


def test_extraction_scales_linearly_in_entities() -> None:
    # #97: one regex per entity per number made 800 entities take 10 s.
    def answer(n: int) -> tuple[str, set[str]]:
        names = {f"T{i:04d}" for i in range(n)}
        return " ".join(f"T{i:04d} closed at {100 + i}.5." for i in range(0, n, 4)), names

    short, long = answer(200), answer(800)
    ratio = _best_of(3, *long) / _best_of(3, *short)
    assert ratio < 9, f"4x the entities took {ratio:.1f}x as long"


def test_one_long_sentence_and_one_long_table_scale_linearly() -> None:
    # #97: every number rescanned its whole sentence; every table cell
    # walked up to its header.
    def sentence(n: int) -> str:
        return "NVDA closed at " + ", ".join(f"{100 + i}.25" for i in range(n)) + "."

    def table(n: int) -> str:
        rows = "\n".join(f"| NVDA | {100 + i}.5 |" for i in range(n))
        return "| Ticker | Close |\n|---|---|\n" + rows

    for build in (sentence, table):
        ratio = _best_of(3, build(2000)) / _best_of(3, build(500))
        assert ratio < 9, f"{build.__name__}: 4x longer took {ratio:.1f}x as long"
