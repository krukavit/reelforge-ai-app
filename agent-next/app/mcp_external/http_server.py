from __future__ import annotations

import asyncio
import json
from typing import Any

from aiohttp import web

from .server import (
    PROTOCOL_VERSION,
    handle_initialize,
    handle_tools_call,
    handle_tools_list,
)


def _jsonrpc_result(request_id: Any, result: dict[str, Any]) -> web.Response:
    return web.json_response(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        }
    )


def _jsonrpc_error(
    request_id: Any,
    code: int,
    message: str,
) -> web.Response:
    return web.json_response(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": code,
                "message": message,
            },
        }
    )


async def health(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "status": "ok",
            "service": "reelforge-agent-next-mcp",
            "protocol": PROTOCOL_VERSION,
        }
    )


async def mcp(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        return _jsonrpc_error(None, -32700, "Parse error")

    if not isinstance(payload, dict):
        return _jsonrpc_error(None, -32600, "Invalid Request")

    if payload.get("jsonrpc") != "2.0":
        return _jsonrpc_error(
            payload.get("id"),
            -32600,
            "jsonrpc must be 2.0",
        )

    request_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}

    if not isinstance(method, str):
        return _jsonrpc_error(request_id, -32600, "method must be a string")

    if not isinstance(params, dict):
        return _jsonrpc_error(request_id, -32600, "params must be an object")

    if method == "initialize":
        return _jsonrpc_result(
            request_id,
            handle_initialize(params),
        )

    if method == "notifications/initialized":
        return web.Response(status=202)

    if method == "tools/list":
        return _jsonrpc_result(
            request_id,
            handle_tools_list(),
        )

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}

        if not isinstance(name, str):
            return _jsonrpc_error(
                request_id,
                -32602,
                "tools/call requires string parameter 'name'",
            )

        if not isinstance(arguments, dict):
            return _jsonrpc_error(
                request_id,
                -32602,
                "tools/call 'arguments' must be an object",
            )

        return _jsonrpc_result(
            request_id,
            handle_tools_call(name, arguments),
        )

    return _jsonrpc_error(
        request_id,
        -32601,
        f"Method not found: {method}",
    )


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/healthz", health)
    app.router.add_post("/mcp", mcp)
    return app


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    web.run_app(
        create_app(),
        host=host,
        port=port,
        print=None,
    )


if __name__ == "__main__":
    run()
