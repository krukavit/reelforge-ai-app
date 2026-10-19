

from app.mcp_client import MCPClient, MCPRPCError


class FakeClient(MCPClient):
    def __init__(self):
        self.config = {"MCP_PROTOCOL": "2025-06-18"}
        self.session_id = "sid"
        self.request_id = 0
        self.calls = []
        self.pages = [
            {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "a"}], "nextCursor": "c2"}},
            {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "b"}]}}
        ]

    def initialize(self):
        self.session_id = "sid-new"
        return {"result": {"protocolVersion": "2025-06-18"}}

    def _request(self, method, params=None, timeout=None):
        self.calls.append((method, params))
        if method == "tools/list":
            return self.pages.pop(0)
        raise AssertionError(method)


def test_tools_list_paginates():
    c = FakeClient()
    out = c.tools_list()
    assert [x["name"] for x in out["result"]["tools"]] == ["a", "b"]
    assert c.calls == [("tools/list", {}), ("tools/list", {"cursor": "c2"})]


def test_rpc_response_id_must_match_request():
    try:
        MCPClient._validate_rpc_response(
            {"jsonrpc": "2.0", "id": 99, "result": {}}, expected_id=1
        )
    except RuntimeError as exc:
        assert "id mismatch" in str(exc)
    else:
        raise AssertionError("mismatched JSON-RPC response id accepted")


def test_mutation_call_is_not_replayed_after_failure():
    c = FakeClient()
    c.session_id = "sid"
    attempts = []
    def fail(method, params=None, timeout=None):
        attempts.append(method)
        raise OSError("connection lost after send")
    c._request = fail
    try:
        c.call("tools/call", {"name": "delete", "arguments": {"path": "A"}})
    except RuntimeError as exc:
        assert "UNKNOWN_OUTCOME" in str(exc) or "unknown outcome" in str(exc)
    else:
        raise AssertionError("mutation was not rejected as unknown outcome")
    assert attempts == ["tools/call"]


class ToolCallCaptureClient(MCPClient):
    def __init__(self):
        self.config = {"MCP_PROTOCOL": "2025-06-18"}
        self.session_id = "sid"
        self.request_id = 0
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params))
        return {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}



def test_json_rpc_error_is_known_and_not_unknown_outcome():
    class RPCErrorClient(MCPClient):
        def __init__(self):
            self.config = {"MCP_PROTOCOL": "2025-06-18"}
            self.session_id = "sid"
            self.request_id = 0

        def _request(self, method, params=None, timeout=None):
            raise MCPRPCError("MCP JSON-RPC error -32000: rejected")

    c = RPCErrorClient()
    try:
        c.call("tools/call", {"name": "delete", "arguments": {"path": "A"}})
    except MCPRPCError as exc:
        assert "rejected" in str(exc)
    else:
        raise AssertionError("known JSON-RPC error was relabeled as unknown outcome")

def test_confirmation_wiring_three_cases():
    c = ToolCallCaptureClient()

    c.tool_call("health", {})
    assert c.calls[-1] == ("tools/call", {"name": "health", "arguments": {}})

    c.tool_call("delete", {"path": "A", "confirmed": True})
    method, params = c.calls[-1]
    assert method == "tools/call"
    assert params["arguments"]["confirmed"] is True
    assert params["confirmed"] is True

    c.tool_call("delete", {"path": "A", "confirmed": False})
    method, params = c.calls[-1]
    assert method == "tools/call"
    assert params["arguments"]["confirmed"] is False
    assert "confirmed" not in params
