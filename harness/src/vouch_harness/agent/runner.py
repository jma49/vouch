"""Agent loop for the real-model evaluation (docs/roadmap.md Phase 2).

For each (model, task, sample), the runner starts a fresh vouch proxy
in front of the synthetic market server, lets the model call tools
through it until it answers, and saves the run: the final answer, the
signed receipt log the proxy wrote, and the full transcript. Those run
directories are the evaluation's raw data; labeling and scoring read
them and never call a model.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from vouch_harness.agent.llm import ChatClient, Message, check_path_segment
from vouch_harness.agent.mcp_client import RPCError, StdioMCPClient, ToolHost
from vouch_harness.signing import signing_key_id

# The date the synthetic data ends on (vouch_harness.market.AS_OF_DAY).
# Stating it lets the model read "latest" the same way the verifier does.
SYSTEM_PROMPT = """You are a financial research assistant. Today is Friday, July 24, 2026, \
after the US market close. Use the available tools to look up market data before stating any \
figure, and answer the user's question concisely in plain prose."""

MAX_TURNS = 12

# JSON Schema keywords every OpenAI-compatible provider accepts in tool
# parameters. Others ("default", "$schema", ...) are rejected by some,
# notably Gemini, so they are dropped when translating MCP tools.
_SCHEMA_KEYS = {
    "type",
    "properties",
    "required",
    "description",
    "enum",
    "items",
    "minimum",
    "maximum",
}


@dataclass(frozen=True)
class Task:
    id: str
    prompt: str
    tags: tuple[str, ...] = ()


def load_tasks(path: str | Path) -> list[Task]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    tasks = [
        Task(
            id=check_path_segment("task id", t["id"]),
            prompt=t["prompt"],
            tags=tuple(t.get("tags", ())),
        )
        for t in raw
    ]
    ids = [t.id for t in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate task ids")
    return tasks


def _clean_schema(schema: Any) -> Any:
    if isinstance(schema, dict):
        out = {k: _clean_schema(v) for k, v in schema.items() if k in _SCHEMA_KEYS}
        if "properties" in schema:
            out["properties"] = {k: _clean_schema(v) for k, v in schema["properties"].items()}
        return out
    return schema


def to_openai_tools(mcp_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate MCP tool definitions into chat-completions function tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": _clean_schema(t.get("inputSchema", {"type": "object"})),
            },
        }
        for t in mcp_tools
    ]


def _tool_text(result: dict[str, Any]) -> str:
    text = "\n".join(
        c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"
    )
    return f"ERROR: {text}" if result.get("isError") else text


def _content_text(content: Any) -> str:
    """The answer text of an assistant message. Some OpenAI-compatible
    providers send content as a list of parts; only the text parts are
    the answer, and str() of the list would be labeled and scored."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(p.get("text", ""))
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


# Finish reasons that mean the answer was cut off, not completed.
_TRUNCATED = frozenset({"length", "content_filter"})


@dataclass(frozen=True)
class RunResult:
    answer: str
    turns: int
    tool_calls: int
    # False when MAX_TURNS ran out, or the provider cut the answer off.
    finished: bool
    finish_reason: str | None = None  # the provider's, for the last turn


def run_agent(
    client: ChatClient, host: ToolHost, prompt: str, sample: int
) -> tuple[RunResult, list[Message]]:
    """Drive one conversation to a final answer. Returns the result and
    the transcript. Assistant messages are kept exactly as the provider
    returned them: some (Gemini 3) attach fields to tool calls that must
    be sent back unchanged on the next turn."""
    tools = to_openai_tools(host.list_tools())
    messages: list[Message] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    calls = 0
    for turn in range(1, MAX_TURNS + 1):
        reply = client.complete(messages, tools, sample)
        message = reply.message
        messages.append(message)
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            reason = reply.finish_reason
            result = RunResult(
                _content_text(message.get("content")), turn, calls, reason not in _TRUNCATED, reason
            )
            return result, messages
        for tc in tool_calls:
            calls += 1
            fn = tc.get("function", {})
            messages.append(
                {"role": "tool", "tool_call_id": tc.get("id", ""), "content": _call(host, fn)}
            )
    return RunResult("", MAX_TURNS, calls, False), messages


def _call(host: ToolHost, fn: dict[str, Any]) -> str:
    """One tool call's result text. Everything the model can get wrong
    (bad JSON, a non-object, an unknown tool) becomes an ERROR message
    it reads and can recover from; real models do all of these, and
    none of them should end the run. A broken session (MCPError other
    than RPCError) still raises: the run cannot continue meaningfully."""
    raw = fn.get("arguments")
    if isinstance(raw, dict):
        args: Any = raw  # some providers send the object instead of a JSON string
    elif raw is None or isinstance(raw, str):
        try:
            args = json.loads(raw or "{}")
        except json.JSONDecodeError as e:
            return f"ERROR: arguments are not valid JSON: {e}"
    else:
        args = raw
    if not isinstance(args, dict):
        return f"ERROR: arguments must be a JSON object, got {type(args).__name__}"
    try:
        return _tool_text(host.call_tool(str(fn.get("name", "")), args))
    except RPCError as e:
        return f"ERROR: {e}"


@dataclass(frozen=True)
class RunSpec:
    model: str
    task: Task
    sample: int

    @property
    def session(self) -> str:
        return f"{self.model}.{self.task.id}.s{self.sample}"


def run_dir(out: Path, spec: RunSpec) -> Path:
    return out / spec.model / spec.task.id / f"s{spec.sample}"


def proxy_argv(proxy: Path, schemas: Path, receipts: Path, session: str) -> list[str]:
    # The proxy splits --upstream on whitespace (docs/pitfalls.md P-023).
    upstream = f"{sys.executable} -m vouch_harness.market"
    return [
        str(proxy),
        "proxy",
        "--upstream",
        upstream,
        "--receipts",
        str(receipts),
        "--schemas",
        str(schemas),
        "--session",
        session,
    ]


def execute(
    spec: RunSpec,
    client: ChatClient,
    out: Path,
    proxy: Path,
    schemas: Path,
    env: dict[str, str],
    host_factory: Callable[[Sequence[str], dict[str, str], Path], ToolHost] | None = None,
) -> Path:
    """Run one (model, task, sample) and write its directory. A run that
    already has a meta.json is complete and is skipped, so an
    interrupted evaluation resumes."""
    d = run_dir(out, spec)
    if (d / "meta.json").exists():
        return d
    d.mkdir(parents=True, exist_ok=True)
    (d / "receipts.jsonl").unlink(missing_ok=True)  # a partial previous attempt
    (d / "error.txt").unlink(missing_ok=True)  # its recorded failure
    argv = proxy_argv(proxy, schemas, d, spec.session)
    if host_factory is not None:
        host = host_factory(argv, env, d)
        result, transcript = run_agent(client, host, spec.task.prompt, spec.sample)
    else:
        with StdioMCPClient(argv, env=env, stderr=d / "proxy.log") as mcp:
            result, transcript = run_agent(client, mcp, spec.task.prompt, spec.sample)
    (d / "answer.txt").write_text(result.answer + "\n", encoding="utf-8")
    (d / "transcript.json").write_text(
        json.dumps(transcript, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    meta = {
        "model": spec.model,
        "task": spec.task.id,
        "prompt": spec.task.prompt,
        "sample": spec.sample,
        "session": spec.session,
        # Which key signed receipts.jsonl, so scoring can tell a wrong
        # key from a tampered log (vouch_harness.signing).
        "key_id": signing_key_id(Path(env["VOUCH_SIGNING_KEY"]))
        if "VOUCH_SIGNING_KEY" in env
        else None,
        **asdict(result),
    }
    (d / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return d
