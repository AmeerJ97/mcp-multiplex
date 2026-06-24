from __future__ import annotations

import json
import os
import sys
from typing import Any


def main() -> int:
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(request)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    return 0


def handle(request: Any) -> dict[str, Any] | None:
    if not isinstance(request, dict):
        return error(None, -32600, "invalid request")
    request_id = request.get("id")
    method = request.get("method")
    if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return error(request_id, -32600, "invalid request")
    if request_id is None and method.startswith("notifications/"):
        return None
    if method == "initialize":
        params = request.get("params")
        params_obj = params if isinstance(params, dict) else {}
        protocol_version = params_obj.get("protocolVersion")
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": protocol_version or "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-stdio", "version": "0.1.0"},
            },
        }
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": [
                    {
                        "name": "ping",
                        "description": "Return pong.",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            },
        }
    if method == "tools/call":
        params = request.get("params")
        params_obj = params if isinstance(params, dict) else {}
        if params_obj.get("name") == "backend_request_id":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": str(request_id)}],
                    "isError": False,
                },
            }
        if params_obj.get("name") == "env_present":
            arguments = params_obj.get("arguments")
            arguments_obj = arguments if isinstance(arguments, dict) else {}
            env_name = arguments_obj.get("name")
            if not isinstance(env_name, str):
                return error(request_id, -32602, "missing env name")
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": "present" if env_name in os.environ else "missing",
                        }
                    ],
                    "isError": False,
                },
            }
        if params_obj.get("name") != "ping":
            return error(request_id, -32602, "unknown tool")
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": "pong"}], "isError": False},
        }
    return error(request_id, -32601, "method not found")


def error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


if __name__ == "__main__":
    raise SystemExit(main())
