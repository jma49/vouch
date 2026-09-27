"""Extraction scales linearly with answer length (issue #19).

The test compares two lengths rather than asserting absolute times, so
it holds on slow CI machines: a 4x longer answer must take well under
16x as long (quadratic), with generous room above linear's 4x.
"""

from __future__ import annotations

import time

from vouch_verifier.claims import extract_claims

ENTITIES = {"NVDA", "AMD", "AAPL", "MSFT"}


def _answer(sentences: int) -> str:
    tickers = sorted(ENTITIES)
    return " ".join(
        f"{tickers[i % 4]} closed at ${100 + i % 50}.{i % 97:02d} and RSI is {40 + i % 30}."
        for i in range(sentences)
    )


def _best_of(runs: int, text: str) -> float:
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        extract_claims(text, ENTITIES)
        best = min(best, time.perf_counter() - start)
    return best


def test_extraction_scales_linearly() -> None:
    short, long = _answer(300), _answer(1200)
    ratio = _best_of(3, long) / _best_of(3, short)
    assert ratio < 9, f"4x longer answer took {ratio:.1f}x as long"
