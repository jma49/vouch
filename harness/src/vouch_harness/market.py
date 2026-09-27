"""Synthetic market-data MCP server for the real-agent evaluation.

The evaluation (docs/roadmap.md Phase 2) needs an upstream the agent
can call through the vouch proxy. A live market-data provider would
make runs irreproducible and its data possibly not redistributable
(docs/design.md section 12), so this server serves deterministic,
**synthetic** data: every value is generated from a seeded random walk
and is not a real market quote.

Real ticker symbols are used on purpose. A model that "knows" NVDA's
price from pretraining will sometimes prefer that memory over the tool
result, and that is exactly the failure the evaluation must be able to
observe.

    python -m vouch_harness.market      # MCP over stdio

Speaks newline-delimited JSON-RPC 2.0, like the proxy.
"""

from __future__ import annotations

import json
import math
import random
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from functools import cache
from itertools import pairwise
from typing import Any, TextIO

SERVER_NAME = "vouch-synthetic-market"
PROTOCOL_VERSION = "2025-06-18"

# Last trading day in the dataset; the data "as of" the evaluation.
AS_OF_DAY = date(2026, 7, 24)
_HISTORY_DAYS = 60  # enough for RSI(14) and MACD(12, 26, 9) to settle
MAX_BARS = 10

# Starting price and typical daily volume per symbol. Plausible, not real.
_UNIVERSE: dict[str, tuple[float, int]] = {
    "NVDA": (171.0, 190_000_000),
    "AMD": (163.0, 55_000_000),
    "AAPL": (208.0, 50_000_000),
    "MSFT": (488.0, 21_000_000),
    "GOOGL": (182.0, 30_000_000),
    "AMZN": (219.0, 38_000_000),
    "META": (690.0, 12_000_000),
    "TSLA": (305.0, 95_000_000),
}


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: int

    @property
    def t(self) -> str:
        return f"{self.day.isoformat()}T20:00:00Z"


def _trading_days(end: date, n: int) -> list[date]:
    days: list[date] = []
    d = end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return days[::-1]


@cache
def bars(symbol: str) -> tuple[Bar, ...]:
    """Full synthetic daily history for a symbol, oldest first."""
    price, base_volume = _UNIVERSE[symbol]
    rng = random.Random(f"vouch-market:{symbol}")
    out: list[Bar] = []
    for day in _trading_days(AS_OF_DAY, _HISTORY_DAYS):
        open_ = price * (1 + rng.gauss(0, 0.005))
        close = open_ * (1 + rng.gauss(0.0008, 0.018))
        high = max(open_, close) * (1 + abs(rng.gauss(0, 0.006)))
        low = min(open_, close) * (1 - abs(rng.gauss(0, 0.006)))
        volume = int(base_volume * math.exp(rng.gauss(0, 0.25)))
        out.append(
            Bar(day, round(open_, 2), round(high, 2), round(low, 2), round(close, 2), volume)
        )
        price = close
    return tuple(out)


def _ema(values: list[float], span: int) -> list[float]:
    k = 2 / (span + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi_14(closes: list[float]) -> float:
    """Wilder's RSI over the last 14 periods."""
    gains = [max(b - a, 0.0) for a, b in pairwise(closes)]
    losses = [max(a - b, 0.0) for a, b in pairwise(closes)]
    avg_gain = sum(gains[:14]) / 14
    avg_loss = sum(losses[:14]) / 14
    for g, lo in zip(gains[14:], losses[14:], strict=True):
        avg_gain = (avg_gain * 13 + g) / 14
        avg_loss = (avg_loss * 13 + lo) / 14
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def macd_histogram(closes: list[float]) -> float:
    macd = [a - b for a, b in zip(_ema(closes, 12), _ema(closes, 26), strict=True)]
    return macd[-1] - _ema(macd, 9)[-1]


def get_quote(symbol: str) -> dict[str, Any]:
    history = bars(symbol)
    last, prev = history[-1], history[-2]
    return {
        "symbol": symbol,
        "as_of": last.t,
        "last": last.close,
        "change_pct": round((last.close / prev.close - 1) * 100, 2),
        "volume": last.volume,
        "data_notice": "synthetic evaluation data, not a real market quote",
    }


def get_indicators(symbol: str) -> dict[str, Any]:
    history = bars(symbol)
    closes = [b.close for b in history]
    return {
        "symbol": symbol,
        "as_of": history[-1].t,
        "timeframe": "1d",
        "rsi_14": round(rsi_14(closes), 1),
        "macd": {"histogram": round(macd_histogram(closes), 2)},
        "close": history[-1].close,
        "data_notice": "synthetic evaluation data, not a real market quote",
    }


def get_ohlcv(symbol: str, limit: int = 5) -> dict[str, Any]:
    limit = max(1, min(int(limit), MAX_BARS))
    return {
        "symbol": symbol,
        "timeframe": "1d",
        "bars": [
            {
                "t": b.t,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in bars(symbol)[-limit:]
        ],
        "data_notice": "synthetic evaluation data, not a real market quote",
    }


_SYMBOL = {"type": "string", "description": "Ticker symbol, e.g. NVDA", "enum": sorted(_UNIVERSE)}
TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_quote",
        "description": "Latest daily quote: last price (USD), day change in percent, volume.",
        "inputSchema": {
            "type": "object",
            "properties": {"symbol": _SYMBOL},
            "required": ["symbol"],
        },
    },
    {
        "name": "get_indicators",
        "description": "Daily technical indicators: RSI(14), MACD histogram, and close.",
        "inputSchema": {
            "type": "object",
            "properties": {"symbol": _SYMBOL},
            "required": ["symbol"],
        },
    },
    {
        "name": "get_ohlcv",
        "description": f"Daily OHLCV bars, most recent last (up to {MAX_BARS}).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": _SYMBOL,
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_BARS, "default": 5},
            },
            "required": ["symbol"],
        },
    },
]
_HANDLERS = {"get_quote": get_quote, "get_indicators": get_indicators, "get_ohlcv": get_ohlcv}


_JSON_TYPES: dict[str, tuple[type, ...]] = {"string": (str,), "integer": (int,)}


def _argument_error(tool: dict[str, Any], arguments: object) -> str | None:
    """What is wrong with the arguments against the tool's inputSchema,
    or None. Checked before the handler runs: real models send extra
    keys, wrong types, and non-objects, and any of those reaching the
    handler would raise and take the whole server down."""
    if not isinstance(arguments, dict):
        return f"arguments must be an object, got {type(arguments).__name__}"
    schema = tool["inputSchema"]
    props: dict[str, Any] = schema["properties"]
    for key in arguments:
        if key not in props:
            return f"unexpected argument {key!r}; accepted: {', '.join(sorted(props))}"
    for key in schema.get("required", []):
        if key not in arguments:
            return f"missing required argument {key!r}"
    for key, value in arguments.items():
        kind = props[key]["type"]
        # bool is an int subclass in Python but not a JSON integer.
        if isinstance(value, bool) or not isinstance(value, _JSON_TYPES[kind]):
            article = "an" if kind[0] in "aeiou" else "a"
            return f"argument {key!r} must be {article} {kind}, got {json.dumps(value)}"
    return None


def call_tool(name: str, arguments: object) -> dict[str, Any]:
    """An MCP tools/call result. Bad input is a tool error the model can
    read and recover from, not a protocol error."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return _tool_error(f"unknown tool {name!r}; available: {', '.join(sorted(_HANDLERS))}")
    tool = next(t for t in TOOLS if t["name"] == name)
    problem = _argument_error(tool, arguments)
    if problem is not None:
        return _tool_error(problem)
    assert isinstance(arguments, dict)
    symbol = arguments["symbol"]
    if symbol not in _UNIVERSE:
        return _tool_error(f"unknown symbol {symbol!r}; supported: {', '.join(sorted(_UNIVERSE))}")
    payload = handler(**arguments)
    return {
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "structuredContent": payload,
    }


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def handle(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Respond to one JSON-RPC message; None for notifications."""
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None
    params = msg.get("params") or {}
    if method == "initialize":
        result: dict[str, Any] = {
            "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": "0.1.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = call_tool(params.get("name", ""), params.get("arguments") or {})
    else:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"method {method!r} not found"},
        }
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def serve(stdin: TextIO, stdout: TextIO) -> None:
    for line in stdin:
        if not line.strip():
            continue
        reply = handle(json.loads(line))
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


if __name__ == "__main__":
    serve(sys.stdin, sys.stdout)
