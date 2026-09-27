"""Synthetic market-data MCP server: determinism, indicator math, protocol."""

import io
import json

import pytest

from vouch_harness import market


def test_data_is_deterministic_and_ends_on_as_of_day() -> None:
    market.bars.cache_clear()
    first = market.get_quote("NVDA")
    market.bars.cache_clear()
    assert market.get_quote("NVDA") == first
    assert first["as_of"] == "2026-07-24T20:00:00Z"
    assert all(b.day.weekday() < 5 for b in market.bars("NVDA"))


def test_quote_is_consistent_with_bars() -> None:
    history = market.bars("AMD")
    quote = market.get_quote("AMD")
    assert quote["last"] == history[-1].close
    assert quote["change_pct"] == round((history[-1].close / history[-2].close - 1) * 100, 2)
    assert market.get_indicators("AMD")["close"] == quote["last"]


@pytest.mark.parametrize("symbol", sorted(market._UNIVERSE))
def test_indicators_are_in_range(symbol: str) -> None:
    ind = market.get_indicators(symbol)
    assert 0 <= ind["rsi_14"] <= 100
    for b in market.bars(symbol):
        assert b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high
        assert b.volume > 0


def test_rsi_extremes() -> None:
    assert market.rsi_14([float(i) for i in range(30)]) == 100.0
    assert market.rsi_14([float(30 - i) for i in range(30)]) == pytest.approx(0.0)


def test_ohlcv_limit_is_clamped() -> None:
    assert len(market.get_ohlcv("MSFT", limit=3)["bars"]) == 3
    assert len(market.get_ohlcv("MSFT", limit=999)["bars"]) == market.MAX_BARS
    assert len(market.get_ohlcv("MSFT", limit=0)["bars"]) == 1


def test_bad_input_is_a_tool_error_not_a_protocol_error() -> None:
    result = market.call_tool("get_quote", {"symbol": "XYZ"})
    assert result["isError"] is True
    assert "unknown symbol" in result["content"][0]["text"]
    assert market.call_tool("nope", {"symbol": "NVDA"})["isError"] is True


def test_tool_result_carries_structured_content() -> None:
    result = market.call_tool("get_quote", {"symbol": "TSLA"})
    assert result["structuredContent"] == json.loads(result["content"][0]["text"])
    assert "synthetic" in result["structuredContent"]["data_notice"]


def test_serve_speaks_newline_delimited_jsonrpc() -> None:
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "x"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
    ]
    out = io.StringIO()
    market.serve(io.StringIO("".join(json.dumps(r) + "\n" for r in requests)), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[0]["result"]["protocolVersion"] == "x"
    assert {t["name"] for t in replies[1]["result"]["tools"]} == set(market._HANDLERS)
    assert replies[2]["error"]["code"] == -32601


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        ("get_quote", {"symbol": "NVDA", "timeframe": "1d"}, "unexpected argument"),
        ("get_ohlcv", {"symbol": "NVDA", "limit": "five"}, "must be an integer"),
        ("get_ohlcv", {"symbol": "NVDA", "limit": True}, "must be an integer"),
        ("get_quote", {"symbol": ["NVDA"]}, "must be a string"),
        ("get_quote", {}, "missing required argument"),
        ("get_quote", ["NVDA"], "must be an object"),
        ("get_quote", None, "must be an object"),
    ],
)
def test_malformed_arguments_are_tool_errors(name: str, arguments: object, message: str) -> None:
    result = market.call_tool(name, arguments)
    assert result["isError"] is True
    assert message in result["content"][0]["text"]


def test_serve_survives_malformed_arguments() -> None:
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_quote", "arguments": {"symbol": ["NVDA"]}},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_quote", "arguments": {"symbol": "NVDA"}},
        },
    ]
    out = io.StringIO()
    market.serve(io.StringIO("".join(json.dumps(r) + "\n" for r in requests)), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert replies[0]["result"]["isError"] is True
    assert "isError" not in replies[1]["result"]
