import json
from pathlib import Path
from typing import Any

import pytest

from vouch_verifier import canonicalize

VECTORS = Path(__file__).resolve().parents[2] / "testdata" / "canonical_vectors.json"


def load_vectors() -> list[dict[str, Any]]:
    vectors: list[dict[str, Any]] = json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"]
    assert vectors, "no vectors loaded"
    return vectors


@pytest.mark.parametrize("vec", load_vectors(), ids=lambda v: v["name"])
def test_vectors(vec: dict[str, Any]) -> None:
    if vec.get("rejected"):
        with pytest.raises(ValueError):
            canonicalize(vec["input"])
    else:
        assert canonicalize(vec["input"]) == vec["canonical"]


def test_idempotent() -> None:
    once = canonicalize('{"b": {"y": 2, "x": 1}, "a": [1, 2.5, "s"]}')
    assert canonicalize(once) == once


@pytest.mark.parametrize("bad", ["", "{", '{"a":1}garbage'])
def test_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        canonicalize(bad)
