"""The words a domain uses for its metrics (issue #88).

Tier 2 extraction reads prose, so it needs to know which words name
which metric, what unit each metric is in, which metrics can be
negative, and which metric a bare percentage or a "to <number>" move
means when no keyword names one. That is all a domain contributes to
the verifier; everything else is domain-free. FINANCE is the default,
matching the synthetic market server; other domains load theirs from
YAML (`vouch-verify --vocabulary`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

# Units the tokenizer can attach to a number (vouch_verifier.tokens).
UNITS = frozenset({"USD", "pct"})


@dataclass(frozen=True)
class Vocabulary:
    # keyword -> metric; longer keywords win over shorter ones
    synonyms: Mapping[str, str]
    # metric -> unit; a metric not listed has none
    units: Mapping[str, str | None] = field(default_factory=dict)
    # metrics that can be negative, so "(1.35%)" reads as -1.35
    signed: frozenset[str] = frozenset()
    # the metric a percentage means when no keyword names one, and the
    # sentence is about a USD metric or names none ("AMD is down 1.35%")
    pct_fallback: str | None = None
    # the metric of a bare number after a move and "to"/"at"
    # ("fell 1.35% to 172.04")
    move_target: str | None = None
    # the receipted series a multi-day change or a high/low is computed
    # over when the sentence names no metric of its own ("up 6.2% since
    # July 17"); None means the domain has no derived claims (design 6.2)
    series: str | None = None


FINANCE = Vocabulary(
    synonyms=MappingProxyType(
        {
            "macd histogram": "macd_hist",
            "macd hist": "macd_hist",
            "last price": "last_price",
            "trading at": "last_price",
            "rsi(14)": "rsi_14",
            "closed at": "close_price",
            "closing": "close_price",
            "closed": "close_price",
            "close": "close_price",
            "opened at": "open_price",
            "opened": "open_price",
            "open": "open_price",
            "volume": "volume",
            "change": "change_pct",
            "macd": "macd_hist",
            "rsi": "rsi_14",
            "last": "last_price",
        }
    ),
    units=MappingProxyType(
        {"close_price": "USD", "open_price": "USD", "last_price": "USD", "change_pct": "pct"}
    ),
    signed=frozenset({"change_pct", "macd_hist"}),
    pct_fallback="change_pct",
    move_target="last_price",
    series="close_price",
)


def load_vocabulary(path: str | Path) -> Vocabulary:
    """Read a vocabulary YAML file. Unknown keys and units are errors: a
    typo must tighten verification (fewer resolved claims), never loosen
    it silently (AGENTS.md invariant 3)."""
    try:
        raw: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"vocabulary {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ValueError(f"vocabulary {path}: expected a mapping")
    unknown = set(raw) - {"synonyms", "units", "signed", "pct_fallback", "move_target", "series"}
    if unknown:
        raise ValueError(f"vocabulary {path}: unknown keys {sorted(unknown)}")
    synonyms = raw.get("synonyms") or {}
    if not isinstance(synonyms, dict) or not synonyms:
        raise ValueError(f"vocabulary {path}: synonyms must be a non-empty mapping")
    synonyms = {str(k).lower(): str(v) for k, v in synonyms.items()}
    metrics = set(synonyms.values())
    units = raw.get("units") or {}
    if not isinstance(units, dict):
        raise ValueError(f"vocabulary {path}: units must be a mapping")
    for metric, unit in units.items():
        if unit is not None and unit not in UNITS:
            raise ValueError(f"vocabulary {path}: {metric}: unit must be one of {sorted(UNITS)}")
    signed = raw.get("signed") or []
    if not isinstance(signed, list):
        raise ValueError(f"vocabulary {path}: signed must be a list")
    for name in ("pct_fallback", "move_target", "series"):
        value = raw.get(name)
        if value is not None and value not in metrics:
            raise ValueError(
                f"vocabulary {path}: {name} {value!r} is not a metric any synonym names"
            )
    return Vocabulary(
        synonyms=MappingProxyType(synonyms),
        units=MappingProxyType({str(k): v for k, v in units.items()}),
        signed=frozenset(str(s) for s in signed),
        pct_fallback=raw.get("pct_fallback"),
        move_target=raw.get("move_target"),
        series=raw.get("series"),
    )
