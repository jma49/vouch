"""Agent runner: tool translation, the loop, caching, HTTP retries, run
directories, and an end-to-end run through the real Go proxy."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from vouch_harness import market
from vouch_harness.agent import cli as agent_cli
from vouch_harness.agent import runner
from vouch_harness.agent.llm import (
    CachedClient,
    LLMError,
    Message,
    ModelConfig,
    OpenAICompatClient,
    load_models,
)
from vouch_harness.agent.mcp_client import RPCError
from vouch_verifier.receipts import load_log

ROOT = Path(__file__).resolve().parents[2]
PROXY = ROOT / "proxy" / "bin" / "vouch"


class InProcessHost:
    """The synthetic market server without a subprocess."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tools(self) -> list[dict[str, Any]]:
        return market.TOOLS

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return market.call_tool(name, arguments)


class ScriptedClient:
    """Replays a fixed sequence of assistant messages."""

    def __init__(self, replies: list[Message]) -> None:
        self.replies = list(replies)
        self.seen: list[list[Message]] = []

    def complete(
        self, messages: list[Message], tools: list[dict[str, Any]], sample: int
    ) -> Message:
        self.seen.append([dict(m) for m in messages])
        return self.replies.pop(0)


def tool_call(name: str, args: str, call_id: str = "c1") -> Message:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}
        ],
        # Provider-specific fields must survive the round trip.
        "extra_content": {"google": {"thought_signature": "sig"}},
    }


def answer(text: str) -> Message:
    return {"role": "assistant", "content": text}


def test_tool_translation_drops_schema_keys_providers_reject() -> None:
    (quote, _, ohlcv) = runner.to_openai_tools(market.TOOLS)
    assert quote["function"]["name"] == "get_quote"
    limit = ohlcv["function"]["parameters"]["properties"]["limit"]
    assert "default" not in limit and limit["maximum"] == market.MAX_BARS
    assert ohlcv["function"]["parameters"]["required"] == ["symbol"]


def test_loop_calls_tools_then_answers() -> None:
    client = ScriptedClient([tool_call("get_quote", '{"symbol": "NVDA"}'), answer("NVDA is up.")])
    host = InProcessHost()
    result, transcript = runner.run_agent(client, host, "How is NVDA?", sample=0)
    assert result == runner.RunResult("NVDA is up.", turns=2, tool_calls=1, finished=True)
    assert host.calls == [("get_quote", {"symbol": "NVDA"})]
    assert [m["role"] for m in transcript] == ["system", "user", "assistant", "tool", "assistant"]
    assert transcript[2]["extra_content"] == {"google": {"thought_signature": "sig"}}
    assert json.loads(transcript[3]["content"])["symbol"] == "NVDA"


def test_loop_reports_tool_errors_and_bad_arguments_to_the_model() -> None:
    client = ScriptedClient(
        [
            tool_call("get_quote", '{"symbol": "XYZ"}'),
            tool_call("get_quote", "{not json", call_id="c2"),
            answer("done"),
        ]
    )
    _, transcript = runner.run_agent(client, InProcessHost(), "q", sample=0)
    tool_msgs = [m for m in transcript if m["role"] == "tool"]
    assert tool_msgs[0]["content"].startswith("ERROR: unknown symbol")
    assert tool_msgs[1]["content"].startswith("ERROR: arguments are not valid JSON")
    assert tool_msgs[1]["tool_call_id"] == "c2"


class RejectingHost(InProcessHost):
    """Answers unknown tool names the way the proxy does: a JSON-RPC error."""

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in market._HANDLERS:
            raise RPCError(f"tools/call: unknown tool {name!r}")
        return super().call_tool(name, arguments)


def test_loop_survives_malformed_tool_calls() -> None:
    dict_args = tool_call("get_quote", "", call_id="c3")
    dict_args["tool_calls"][0]["function"]["arguments"] = {"symbol": "NVDA"}
    client = ScriptedClient(
        [
            tool_call("get_price", '{"symbol": "NVDA"}'),
            tool_call("get_quote", '["NVDA"]', call_id="c2"),
            dict_args,
            tool_call("get_quote", '{"symbol": "NVDA", "timeframe": "1d"}', call_id="c4"),
            answer("done"),
        ]
    )
    host = RejectingHost()
    result, transcript = runner.run_agent(client, host, "q", sample=0)
    assert result.finished and result.answer == "done"
    tool_msgs = [m["content"] for m in transcript if m["role"] == "tool"]
    assert tool_msgs[0].startswith("ERROR: tools/call: unknown tool 'get_price'")
    assert tool_msgs[1].startswith("ERROR: arguments must be a JSON object")
    assert json.loads(tool_msgs[2])["symbol"] == "NVDA"  # a dict is taken as is
    assert tool_msgs[3].startswith("ERROR: unexpected argument 'timeframe'")


def test_batch_records_an_unexpected_failure_and_continues(tmp_path: Path) -> None:
    specs = [runner.RunSpec("m", runner.Task(f"t{i}", "q"), 0) for i in range(3)]
    done: list[str] = []

    def run_one(spec: runner.RunSpec) -> Path:
        if spec.task.id == "t1":
            raise KeyError("boom")
        done.append(spec.task.id)
        return runner.run_dir(tmp_path, spec)

    assert agent_cli.run_batch(specs, run_one, tmp_path) == 1
    assert done == ["t0", "t2"]
    error = (runner.run_dir(tmp_path, specs[1]) / "error.txt").read_text()
    assert "KeyError: 'boom'" in error
    assert not (runner.run_dir(tmp_path, specs[1]) / "meta.json").exists()  # retried on rerun


def test_execute_clears_a_previous_error(tmp_path: Path) -> None:
    spec = runner.RunSpec("fake", runner.Task("t01", "q"), sample=0)
    d = runner.run_dir(tmp_path, spec)
    d.mkdir(parents=True)
    (d / "error.txt").write_text("old failure\n")
    runner.execute(spec, ScriptedClient([answer("ok")]), tmp_path, PROXY, ROOT, {}, _in_process)
    assert not (d / "error.txt").exists()


def test_loop_gives_up_after_max_turns() -> None:
    client = ScriptedClient([tool_call("get_quote", '{"symbol": "AMD"}')] * runner.MAX_TURNS)
    result, _ = runner.run_agent(client, InProcessHost(), "q", sample=0)
    assert not result.finished and result.answer == ""


def test_cache_keys_on_request_and_sample(tmp_path: Path) -> None:
    inner = ScriptedClient([answer("a"), answer("b")])
    client = CachedClient(inner, tmp_path, identity="m")
    msgs: list[Message] = [{"role": "user", "content": "hi"}]
    assert client.complete(msgs, [], 0)["content"] == "a"
    assert client.complete(msgs, [], 0)["content"] == "a"  # cached
    assert client.complete(msgs, [], 1)["content"] == "b"  # new sample
    assert (client.hits, client.misses) == (1, 2)
    assert CachedClient(inner, tmp_path, identity="other").key(msgs, [], 0) != client.key(
        msgs, [], 0
    )


@dataclass
class FakeProvider:
    """A local OpenAI-compatible endpoint that answers with scripted statuses."""

    url: str = ""
    statuses: list[int] = field(default_factory=list)
    bodies: list[dict[str, Any]] = field(default_factory=list)
    auth: list[str] = field(default_factory=list)


@pytest.fixture
def provider() -> Iterator[FakeProvider]:
    state = FakeProvider()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            state.bodies.append(json.loads(self.rfile.read(length)))
            state.auth.append(self.headers["Authorization"])
            status = state.statuses.pop(0)
            self.send_response(status)
            if status == 429:
                self.send_header("Retry-After", "0")
            self.end_headers()
            if status == 200:
                reply = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
                self.wfile.write(json.dumps(reply).encode())

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}/v1/"
    yield state
    server.shutdown()
    server.server_close()


def test_http_client_retries_rate_limits_and_sends_tools(
    provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_KEY", "secret")
    provider.statuses = [429, 503, 200]
    config = ModelConfig("t", provider.url, "m-1", "TEST_KEY", rpm=6000, params={"temperature": 0})
    sleeps: list[float] = []
    client = OpenAICompatClient(config, sleep=sleeps.append)
    tools = runner.to_openai_tools(market.TOOLS)
    assert client.complete([{"role": "user", "content": "q"}], tools, 0)["content"] == "ok"
    assert len(provider.bodies) == 3
    body = provider.bodies[-1]
    assert (body["model"], body["temperature"], body["tool_choice"]) == ("m-1", 0, "auto")
    assert provider.auth[-1] == "Bearer secret"
    assert 0.0 in sleeps  # Retry-After: 0 was honored


def test_http_client_fails_fast_on_client_errors(
    provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_KEY", "secret")
    provider.statuses = [400]
    client = OpenAICompatClient(
        ModelConfig("t", provider.url, "m", "TEST_KEY"), sleep=lambda s: None
    )
    with pytest.raises(LLMError, match="HTTP 400"):
        client.complete([], [], 0)
    assert len(provider.bodies) == 1


def test_missing_key_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_SUCH_KEY", raising=False)
    with pytest.raises(LLMError, match="NO_SUCH_KEY"):
        OpenAICompatClient(ModelConfig("t", "http://x/", "m", "NO_SUCH_KEY"))


def test_repo_configs_load() -> None:
    models = load_models(ROOT / "eval" / "models.yaml")
    assert "gemini-flash" in models
    tasks = runner.load_tasks(ROOT / "eval" / "tasks.yaml")
    assert len(tasks) >= 30
    assert all(t.tags for t in tasks)


def _in_process(argv: Sequence[str], env: dict[str, str], d: Path) -> InProcessHost:
    return InProcessHost()


def test_execute_writes_a_run_directory_and_resumes(tmp_path: Path) -> None:
    spec = runner.RunSpec("fake", runner.Task("t01", "How is NVDA?"), sample=0)
    client = ScriptedClient([tool_call("get_quote", '{"symbol": "NVDA"}'), answer("Fine.")])
    d = runner.execute(spec, client, tmp_path, PROXY, ROOT / "schemas", {}, _in_process)
    assert (d / "answer.txt").read_text() == "Fine.\n"
    meta = json.loads((d / "meta.json").read_text())
    assert meta["session"] == "fake.t01.s0" and meta["tool_calls"] == 1
    # A completed run is not repeated: the client has no replies left.
    assert runner.execute(spec, client, tmp_path, PROXY, ROOT / "schemas", {}, _in_process) == d


@pytest.mark.skipif(not PROXY.exists(), reason="proxy binary not built (make build)")
def test_end_to_end_through_the_go_proxy(tmp_path: Path) -> None:
    spec = runner.RunSpec("fake", runner.Task("t08", "Compare NVDA and AMD."), sample=0)
    client = ScriptedClient(
        [
            tool_call("get_quote", '{"symbol": "NVDA"}'),
            tool_call("get_quote", '{"symbol": "AMD"}', call_id="c2"),
            answer("Both moved."),
        ]
    )
    env = {"VOUCH_HMAC_KEY": "vouch-eval-key", "PATH": "/usr/bin:/bin"}
    d = runner.execute(spec, client, tmp_path, PROXY, ROOT / "schemas", env)
    receipts = load_log(d / "receipts.jsonl", key=b"vouch-eval-key")
    assert [r.session_id for r in receipts] == ["fake.t08.s0"] * 2
    assert {f.entity for r in receipts for f in r.facts} == {"NVDA", "AMD"}
    nvda_last = next(f for f in receipts[0].facts if f.metric == "last_price")
    assert nvda_last.value == market.get_quote("NVDA")["last"]


@pytest.mark.skipif(not PROXY.exists(), reason="proxy binary not built (make build)")
def test_end_to_end_malformed_calls_do_not_end_the_run(tmp_path: Path) -> None:
    spec = runner.RunSpec("fake", runner.Task("t01", "How is NVDA?"), sample=0)
    client = ScriptedClient(
        [
            tool_call("get_price", '{"symbol": "NVDA"}'),
            tool_call("get_quote", '{"symbol": ["NVDA"], "timeframe": "1d"}', call_id="c2"),
            tool_call("get_quote", '{"symbol": "NVDA"}', call_id="c3"),
            answer("NVDA moved."),
        ]
    )
    env = {"VOUCH_HMAC_KEY": "vouch-eval-key", "PATH": "/usr/bin:/bin"}
    d = runner.execute(spec, client, tmp_path, PROXY, ROOT / "schemas", env)
    meta = json.loads((d / "meta.json").read_text())
    assert meta["finished"] and meta["tool_calls"] == 3
    tool_msgs = [m["content"] for m in json.loads((d / "transcript.json").read_text())[3::2]]
    assert tool_msgs[0].startswith("ERROR:") and "get_price" in tool_msgs[0]
    assert tool_msgs[1].startswith("ERROR:")
    assert json.loads(tool_msgs[2])["symbol"] == "NVDA"
    receipts = load_log(d / "receipts.jsonl", key=b"vouch-eval-key")
    assert "NVDA" in {f.entity for r in receipts for f in r.facts}
