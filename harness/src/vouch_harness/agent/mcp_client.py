"""Minimal MCP client over stdio, for driving the vouch proxy.

Covers what the agent loop needs: initialize, tools/list, tools/call.
Newline-delimited JSON-RPC 2.0, one request in flight, matching the
proxy's transport (proxy/internal/mcp).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any, Protocol

PROTOCOL_VERSION = "2025-06-18"


class ToolHost(Protocol):
    """What the agent loop needs from an MCP server."""

    def list_tools(self) -> list[dict[str, Any]]: ...

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class MCPError(RuntimeError):
    """The server returned a JSON-RPC error or broke the protocol."""


class RPCError(MCPError):
    """The server answered with a JSON-RPC error. The session is intact,
    unlike the other MCPErrors (a closed pipe, a mismatched id), so the
    agent loop can report it to the model and carry on."""


class StdioMCPClient:
    def __init__(
        self, argv: Sequence[str], *, env: dict[str, str] | None = None, stderr: Path | None = None
    ) -> None:
        self._stderr: IO[bytes] | None = stderr.open("wb") if stderr else None
        self._proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr or subprocess.DEVNULL,
            env=env,
        )
        self._next_id = 0
        self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "vouch-agent", "version": "0.1.0"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg: dict[str, Any]) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(msg).encode("utf-8") + b"\n")
        self._proc.stdin.flush()

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        req: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            req["params"] = params
        self._send(req)
        assert self._proc.stdout is not None
        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise MCPError(f"{method}: server closed the connection (exit {self._proc.poll()})")
            msg = json.loads(line)
            if "id" not in msg:
                continue  # notification
            if msg["id"] != self._next_id:
                raise MCPError(f"{method}: response id {msg['id']} != request id {self._next_id}")
            if "error" in msg:
                raise RPCError(f"{method}: {msg['error'].get('message', msg['error'])}")
            result: dict[str, Any] = msg["result"]
            return result

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = self._request("tools/list").get("tools", [])
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._request("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> int:
        """Close stdin (the server's cue to exit), reap it, release pipes."""
        if self._proc.stdin:
            self._proc.stdin.close()
        code = self._proc.wait(timeout=30)
        if self._proc.stdout:
            self._proc.stdout.close()
        if self._stderr:
            self._stderr.close()
        return code

    def __enter__(self) -> StdioMCPClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
