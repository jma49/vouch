"""The stdio JSON-RPC loop shared by vouch's synthetic MCP servers.

Newline-delimited JSON-RPC 2.0, as the proxy speaks it: initialize,
ping, tools/list, and tools/call, with bad tool input reported as a
tool error the model can read (the server's call_tool decides that).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, TextIO

PROTOCOL_VERSION = "2025-06-18"

Handler = Callable[[dict[str, Any]], dict[str, Any] | None]


def make_handler(
    name: str,
    tools: list[dict[str, Any]],
    call_tool: Callable[[str, object], dict[str, Any]],
) -> Handler:
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
                "serverInfo": {"name": name, "version": "0.1.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tools}
        elif method == "tools/call":
            result = call_tool(params.get("name", ""), params.get("arguments") or {})
        else:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"method {method!r} not found"},
            }
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    return handle


def serve(handle: Handler, stdin: TextIO, stdout: TextIO) -> None:
    for line in stdin:
        if not line.strip():
            continue
        reply = handle(json.loads(line))
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


def tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "structuredContent": payload,
    }
