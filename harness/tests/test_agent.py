"""Agent runner: tool translation, the loop, caching, HTTP retries, run
directories, and an end-to-end run through the real Go proxy."""

from __future__ import annotations

import gc
import json
import os
import signal
import socket
import struct
import sys
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from keys import EVAL_KEY_ID, EVAL_KEYS, EVAL_PRIV, proxy_env

from vouch_harness import market, signing
from vouch_harness.agent import cli as agent_cli
from vouch_harness.agent import llm, runner
from vouch_harness.agent.llm import (
    CachedClient,
    LLMError,
    Message,
    ModelConfig,
    OpenAICompatClient,
    Reply,
    load_models,
)
from vouch_harness.agent.mcp_client import MCPError, RPCError, StdioMCPClient
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

    def __init__(self, replies: list[Message | Reply]) -> None:
        self.replies = list(replies)
        self.seen: list[list[Message]] = []

    def complete(self, messages: list[Message], tools: list[dict[str, Any]], sample: int) -> Reply:
        self.seen.append([dict(m) for m in messages])
        reply = self.replies.pop(0)
        return reply if isinstance(reply, Reply) else Reply(reply, "stop")


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
    assert result == runner.RunResult("NVDA is up.", 2, 1, finished=True, finish_reason="stop")
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


def test_truncated_answer_marks_the_run_unfinished() -> None:
    client = ScriptedClient([Reply(answer("NVDA closed at 18"), "length")])
    result, _ = runner.run_agent(client, InProcessHost(), "q", sample=0)
    assert result == runner.RunResult(
        "NVDA closed at 18", turns=1, tool_calls=0, finished=False, finish_reason="length"
    )


def test_list_content_is_joined_from_its_text_parts() -> None:
    parts = [
        {"type": "text", "text": "NVDA closed "},
        {"type": "image_url", "image_url": {"url": "x"}},
        {"type": "text", "text": "at 181.52."},
    ]
    client = ScriptedClient([{"role": "assistant", "content": parts}])
    result, transcript = runner.run_agent(client, InProcessHost(), "q", sample=0)
    assert result.answer == "NVDA closed at 181.52."
    assert result.finished and result.finish_reason == "stop"
    assert transcript[-1]["content"] == parts  # the transcript keeps what the provider sent


def test_cache_identity_covers_request_params() -> None:
    def identity(params: dict[str, Any]) -> str:
        return llm.cache_identity(ModelConfig("t", "https://x/v1/", "m-1", "K", params=params))

    assert identity({"temperature": 0.0}) != identity({"temperature": 1.0})
    assert identity({"temperature": 0.0}) != identity({})
    assert identity({"a": 1, "b": {"c": 2, "d": 3}}) == identity({"b": {"d": 3, "c": 2}, "a": 1})
    # Default settings keep the identity (and so the cache) of earlier versions.
    assert identity({}) == "https://x/v1/chat/completions|m-1"


def test_cache_reads_entries_written_before_finish_reason(tmp_path: Path) -> None:
    client = CachedClient(ScriptedClient([]), tmp_path, identity="m")
    msgs: list[Message] = [{"role": "user", "content": "hi"}]
    key = client.key(msgs, [], 0)
    (tmp_path / key[:2]).mkdir()
    (tmp_path / key[:2] / f"{key}.json").write_text(json.dumps(answer("old")))
    assert client.complete(msgs, [], 0) == Reply(answer("old"), None)


def test_loop_gives_up_after_max_turns() -> None:
    client = ScriptedClient([tool_call("get_quote", '{"symbol": "AMD"}')] * runner.MAX_TURNS)
    result, _ = runner.run_agent(client, InProcessHost(), "q", sample=0)
    assert not result.finished and result.answer == ""


def test_cache_keys_on_request_and_sample(tmp_path: Path) -> None:
    inner = ScriptedClient([answer("a"), answer("b")])
    client = CachedClient(inner, tmp_path, identity="m")
    msgs: list[Message] = [{"role": "user", "content": "hi"}]
    assert client.complete(msgs, [], 0).message["content"] == "a"
    cached = client.complete(msgs, [], 0)
    assert cached == Reply(answer("a"), "stop")  # finish_reason survives the cache
    assert client.complete(msgs, [], 1).message["content"] == "b"  # new sample
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
                reply = {
                    "choices": [
                        {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                    ]
                }
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
    reply = client.complete([{"role": "user", "content": "q"}], tools, 0)
    assert reply == Reply({"role": "assistant", "content": "ok"}, "stop")
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


@dataclass
class FlakyProvider:
    """A local endpoint that fails in scripted transport-level ways."""

    url: str = ""
    actions: list[str] = field(default_factory=list)
    requests: int = 0
    release: threading.Event = field(default_factory=threading.Event)


@pytest.fixture
def flaky() -> Iterator[FlakyProvider]:
    state = FlakyProvider()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            state.requests += 1
            action = state.actions.pop(0)
            if action == "reset":
                # SO_LINGER 0 makes close() send a TCP RST.
                self.connection.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                )
                self.connection.close()
                return
            if action == "hang":
                state.release.wait(5)  # past the client's timeout
                return
            self.send_response(429 if action.startswith("429") else 200)
            if action == "429-date":
                self.send_header("Retry-After", "Wed, 21 Oct 2026 07:28:00 GMT")
            if action == "429-garbage":
                self.send_header("Retry-After", "soon")
            self.end_headers()
            if action == "html":
                self.wfile.write(b"<html>Bad gateway</html>")
            elif action == "ok":
                reply = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
                self.wfile.write(json.dumps(reply).encode())

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}/v1/"
    yield state
    state.release.set()
    server.shutdown()
    server.server_close()


def _flaky_client(
    flaky: FlakyProvider, monkeypatch: pytest.MonkeyPatch, sleeps: list[float], retries: int = 6
) -> OpenAICompatClient:
    monkeypatch.setenv("TEST_KEY", "secret")
    return OpenAICompatClient(
        ModelConfig("t", flaky.url, "m", "TEST_KEY", rpm=60000),
        max_retries=retries,
        timeout=0.3,
        sleep=sleeps.append,
        # 07:27:30 GMT on the Retry-After date: 30 seconds before it.
        wallclock=lambda: 1792567650.0,
    )


def test_http_client_retries_transport_failures(
    flaky: FlakyProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky.actions = ["429-date", "429-garbage", "reset", "hang", "html", "ok"]
    sleeps: list[float] = []
    client = _flaky_client(flaky, monkeypatch, sleeps)
    assert client.complete([], [], 0).message["content"] == "ok"
    assert flaky.requests == 6
    retry_sleeps = [s for s in sleeps if s >= 1.0]  # throttle sleeps are tiny
    assert retry_sleeps[0] == pytest.approx(30.0)  # the HTTP-date form of Retry-After
    assert len(retry_sleeps) == 5


def test_http_client_clamps_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1792567650.0
    assert llm.retry_after_seconds("5", now) == 5.0
    assert llm.retry_after_seconds("100000", now) == llm.MAX_RETRY_AFTER
    assert llm.retry_after_seconds("Wed, 21 Oct 2026 07:28:00 GMT", now) == pytest.approx(30.0)
    assert llm.retry_after_seconds("Wed, 21 Oct 2020 07:28:00 GMT", now) == 0.0
    assert llm.retry_after_seconds("-3", now) == 0.0
    assert llm.retry_after_seconds("soon", now) is None


def test_http_client_raises_llm_error_after_last_transport_failure(
    flaky: FlakyProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky.actions = ["html", "reset"]
    client = _flaky_client(flaky, monkeypatch, [], retries=1)
    with pytest.raises(LLMError, match="t: "):
        client.complete([], [], 0)
    assert flaky.requests == 2


def test_missing_key_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_SUCH_KEY", raising=False)
    with pytest.raises(LLMError, match="NO_SUCH_KEY"):
        OpenAICompatClient(ModelConfig("t", "http://x/", "m", "NO_SUCH_KEY"))


@pytest.mark.parametrize("name", ["openrouter/llama-3", "a b", "..", "", "x\\y", "gpt\n"])
def test_model_names_must_be_one_path_segment(tmp_path: Path, name: str) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(
        json.dumps({name: {"base_url": "http://x/", "model": "v/m", "api_key_env": "K"}})
    )
    with pytest.raises(ValueError, match="model name"):
        load_models(path)


def test_model_ids_may_contain_slashes(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    spec = {"base_url": "http://x/", "model": "meta/llama-3", "api_key_env": "K"}
    path.write_text(json.dumps({"openrouter-llama-3.1_8b": spec}))
    assert load_models(path)["openrouter-llama-3.1_8b"].model == "meta/llama-3"


@pytest.mark.parametrize("task_id", ["a/b", "..", ""])
def test_task_ids_must_be_one_path_segment(tmp_path: Path, task_id: str) -> None:
    path = tmp_path / "tasks.yaml"
    path.write_text(json.dumps([{"id": task_id, "prompt": "q"}]))
    with pytest.raises(ValueError, match="task id"):
        runner.load_tasks(path)


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
    assert meta["finish_reason"] == "stop"
    assert meta["key_id"] is None  # no signing key in this environment
    # A completed run is not repeated: the client has no replies left.
    assert runner.execute(spec, client, tmp_path, PROXY, ROOT / "schemas", {}, _in_process) == d


def test_execute_records_the_signing_key_id(tmp_path: Path) -> None:
    spec = runner.RunSpec("fake", runner.Task("t01", "q"), sample=0)
    env = {"VOUCH_SIGNING_KEY": str(EVAL_PRIV)}
    d = runner.execute(
        spec, ScriptedClient([answer("ok")]), tmp_path, PROXY, ROOT, env, _in_process
    )
    assert json.loads((d / "meta.json").read_text())["key_id"] == EVAL_KEY_ID
    assert signing.signing_key_id(EVAL_PRIV) == EVAL_KEY_ID


# A stand-in MCP server: answers initialize per its first argument, then
# ignores stdin EOF (the cue to exit) and stays alive until killed.
_STUBBORN_SERVER = """
import json, sys, time
open(sys.argv[2], "w").write(str(__import__("os").getpid()))
req = json.loads(sys.stdin.readline())
if sys.argv[1] == "fail":
    reply = {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -1, "message": "no"}}
else:
    reply = {"jsonrpc": "2.0", "id": req["id"], "result": {}}
sys.stdout.write(json.dumps(reply) + "\\n")
sys.stdout.flush()
while True:
    time.sleep(1)
"""


def _reaped(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # an unreaped zombie still accepts signal 0
    except ProcessLookupError:
        return True
    return False


def test_failed_handshake_kills_and_reaps_the_server(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    argv = [sys.executable, "-c", _STUBBORN_SERVER, "fail", str(pidfile)]
    with pytest.raises(MCPError, match="initialize: no"):
        StdioMCPClient(argv, stderr=tmp_path / "server.log")
    assert _reaped(int(pidfile.read_text()))
    gc.collect()  # unclosed pipes or log file would warn here, and warnings are errors


def test_close_kills_a_server_that_does_not_exit(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    argv = [sys.executable, "-c", _STUBBORN_SERVER, "ok", str(pidfile)]
    client = StdioMCPClient(argv, stderr=tmp_path / "server.log")
    assert client.close(timeout=0.5) == -signal.SIGKILL
    assert _reaped(int(pidfile.read_text()))


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
    env = proxy_env(tmp_path)
    d = runner.execute(spec, client, tmp_path, PROXY, ROOT / "schemas", env)
    receipts = load_log(d / "receipts.jsonl", EVAL_KEYS)
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
    env = proxy_env(tmp_path)
    d = runner.execute(spec, client, tmp_path, PROXY, ROOT / "schemas", env)
    meta = json.loads((d / "meta.json").read_text())
    assert meta["finished"] and meta["tool_calls"] == 3
    tool_msgs = [m["content"] for m in json.loads((d / "transcript.json").read_text())[3::2]]
    assert tool_msgs[0].startswith("ERROR:") and "get_price" in tool_msgs[0]
    assert tool_msgs[1].startswith("ERROR:")
    assert json.loads(tool_msgs[2])["symbol"] == "NVDA"
    receipts = load_log(d / "receipts.jsonl", EVAL_KEYS)
    assert "NVDA" in {f.entity for r in receipts for f in r.facts}
