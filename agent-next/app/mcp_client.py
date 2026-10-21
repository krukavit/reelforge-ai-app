import json
import os
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class MCPUnknownOutcomeError(RuntimeError):
    """The request may have reached the server, but its outcome is unknown."""


class MCPRPCError(RuntimeError):
    """The server returned a valid JSON-RPC error; the outcome is known."""


class MCPClient:
    def __init__(self, config_path=None):
        if config_path is None:
            config_path = PROJECT_ROOT / "config" / "mcp.txt"
        self.config = self._load_config(config_path)
        self.session_id = None
        self.request_id = 0

    def _load_config(self, path):
        data = {}
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                data[key.strip()] = value.strip()
        return data

    def _token(self):
        token_file = os.environ.get("REELFORGE_MCP_TOKEN_FILE", self.config.get("MCP_TOKEN_FILE", ""))
        if not token_file:
            raise RuntimeError("MCP token file is not configured")
        path = os.path.expanduser(token_file)
        return Path(path).read_text(encoding="utf-8").strip()

    def _request(self, method, params=None, timeout=None):
        self.request_id += 1
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method,
            "params": params or {},
        }).encode()

        headers = {
            "Authorization": "Bearer " + self._token(),
            "Content-Type": "application/json",
            "Accept": self.config.get(
                "MCP_ACCEPT",
                "text/event-stream,application/json",
            ),
            "Origin": "http://127.0.0.1",
        }

        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
            headers["MCP-Protocol-Version"] = self.config.get(
                "MCP_PROTOCOL", "2025-06-18"
            )

        req = urllib.request.Request(
            self.config["MCP_URL"],
            data=payload,
            headers=headers,
            method="POST",
        )

        with urllib.request.urlopen(
            req,
            timeout=timeout or int(self.config.get("MCP_TIMEOUT", "120")),
        ) as response:
            sid = response.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid

            body = response.read().decode()

            if response.headers.get(
                "Content-Type", ""
            ).startswith("text/event-stream"):
                for line in body.splitlines():
                    if line.startswith("data:"):
                        data = line[5:].strip()
                        if data:
                            return self._validate_rpc_response(
                                json.loads(data), expected_id=self.request_id
                            )

            return self._validate_rpc_response(
                json.loads(body), expected_id=self.request_id
            )

    @staticmethod
    def _validate_rpc_response(data, expected_id=None):
        if not isinstance(data, dict):
            raise RuntimeError("Invalid MCP response: expected JSON object")
        if data.get("jsonrpc") != "2.0":
            raise RuntimeError(
                f"Invalid MCP response: jsonrpc mismatch (expected 2.0, got {data.get('jsonrpc')})"
            )
        if expected_id is not None and data.get("id") != expected_id:
            raise RuntimeError(
                f"Invalid MCP response: id mismatch (expected {expected_id}, got {data.get('id')})"
            )
        if "error" in data:
            error = data.get("error") or {}
            raise MCPRPCError(
                f"MCP JSON-RPC error {error.get('code')}: {error.get('message', 'unknown error')}"
            )
        if "result" not in data:
            raise RuntimeError("Invalid MCP response: missing result/error")
        return data

    def initialize(self):
        result = self._request("initialize", {
            "protocolVersion": self.config.get(
                "MCP_PROTOCOL", "2025-06-18"
            ),
            "capabilities": {},
            "clientInfo": {
                "name": "reelforge-agent-next",
                "version": "0.2.0",
            },
        })
        if not self.session_id:
            raise RuntimeError("MCP session missing after initialize")
        return result

    def call(self, method, params=None):
        if not self.session_id:
            self.initialize()

        try:
            return self._request(method, params)
        except MCPRPCError:
            # A valid JSON-RPC error is a known server-side outcome. Do not
            # relabel it as UNKNOWN_OUTCOME merely because it came from
            # tools/call; the tool call was explicitly rejected.
            raise
        except Exception as exc:
            self.session_id = None
            # Automatic replay is safe only for discovery/read-only protocol calls.
            # Never blindly replay tools/call: the server may have executed a mutation
            # while the response was lost, producing an UNKNOWN_OUTCOME.
            if method != "tools/list":
                raise MCPUnknownOutcomeError(
                    f"MCP request failed with unknown outcome; not retried: {method}: {exc}"
                ) from exc
            self.initialize()
            return self._request(method, params)

    def tools_list(self):
        all_tools = []
        cursor = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            page = self.call("tools/list", params)
            result = page.get("result", {})
            tools = result.get("tools", [])
            if not isinstance(tools, list):
                raise RuntimeError("Invalid tools/list response: result.tools must be a list")
            all_tools.extend(tools)
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return {
            "jsonrpc": "2.0",
            "id": page.get("id"),
            "result": {"tools": all_tools},
        }

    def tool_call(self, name, arguments=None):
        """Call an MCP tool with confirmation compatibility for the live Core.

        The documented MCP schema places ``confirmed`` inside ``arguments``.
        The currently deployed live Core also reads a top-level ``params.confirmed``
        field and overwrites its internal value from that location.  Therefore,
        when a confirmed call is requested, send the flag in both places.
        This is a client-side compatibility workaround; Core/Bridge are not modified.
        """
        call_arguments = dict(arguments or {})
        params = {
            "name": name,
            "arguments": call_arguments,
        }
        if call_arguments.get("confirmed") is True:
            params["confirmed"] = True
        return self.call("tools/call", params)


_client = MCPClient()


def call(method, params=None, timeout=None):
    if timeout is not None:
        return _client._request(method, params, timeout)
    return _client.call(method, params)


if __name__ == "__main__":
    print("initialize:", _client.initialize())
    tools = _client.tools_list()
    print("tools:", len(tools.get("result", {}).get("tools", [])))
    print("session:", bool(_client.session_id))
