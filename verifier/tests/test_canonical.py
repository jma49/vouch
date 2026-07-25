import json
from pathlib import Path

import pytest

from vouch_verifier import canonicalize

VECTORS = Path(__file__).resolve().parents[2] / "testdata" / "canonical_vectors.json"


def load_vectors():
    data = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert data["vectors"], "no vectors loaded"
    return data["vectors"]


@pytest.mark.parametrize("vec", load_vectors(), ids=lambda v: v["name"])
def test_vectors(vec):
    assert canonicalize(vec["input"]) == vec["canonical"]


def test_idempotent():
    once = canonicalize('{"b": {"y": 2, "x": 1}, "a": [1, 2.5, "s"]}')
    assert canonicalize(once) == once


@pytest.mark.parametrize("bad", ["", "{", '{"a":1}garbage'])
def test_rejects_invalid(bad):
    with pytest.raises(ValueError):
        canonicalize(bad)
