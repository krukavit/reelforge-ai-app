import os
import sys
import tempfile
from pathlib import Path

from pathlib import Path

from app.mcp_client import MCPClient
from app.mcp_worker import MCPWorker
from app.state_store import StateStore


def test_rpc_version_is_required():
    try:
        MCPClient._validate_rpc_response({"jsonrpc": "1.0", "id": 1, "result": {}} , expected_id=1)
    except RuntimeError as exc:
        assert "jsonrpc mismatch" in str(exc)
    else:
        raise AssertionError("wrong JSON-RPC version was accepted")


class FakeRegistry:
    def has(self, name):
        return name == "health"


class FakeClient:
    def tool_call(self, name, arguments):
        return {"ok": True, "name": name}


def test_worker_records_successful_mcp_call():
    with tempfile.TemporaryDirectory() as td:
        store = StateStore(Path(td) / "audit.db")
        worker = MCPWorker(registry=FakeRegistry(), client=FakeClient(), state=store)
        result = worker.call_tool("health", arguments={"probe": "ok"}, operation="check", task_id="audit-task")
        assert result["ok"] is True
        rows = store.list_mcp_calls(task_id="audit-task")
        assert len(rows) == 1
        assert rows[0]["tool_name"] == "health"
        assert rows[0]["status"] == "SUCCESS"
        assert rows[0]["operation"] == "check"
        assert rows[0]["arguments_hash"]



def test_worker_records_unknown_outcome():
    from app.mcp_client import MCPUnknownOutcomeError

    class UnknownClient(FakeClient):
        def __init__(self):
            self.request_id = 17
            self.session_id = "sid"
            self.config = {"MCP_PROTOCOL": "2025-06-18"}

        def tool_call(self, name, arguments):
            raise MCPUnknownOutcomeError("unknown outcome")

    with tempfile.TemporaryDirectory() as td:
        store = StateStore(Path(td) / "audit.db")
        worker = MCPWorker(registry=FakeRegistry(), client=UnknownClient(), state=store)
        try:
            worker.call_tool("health", arguments={"x": 1}, task_id="unknown-task")
        except MCPUnknownOutcomeError:
            pass
        else:
            raise AssertionError("unknown outcome was swallowed")
        rows = store.list_mcp_calls(task_id="unknown-task")
        assert len(rows) == 1
        assert rows[0]["status"] == "UNKNOWN_OUTCOME"
        assert rows[0]["request_id"] == 17
        assert rows[0]["session_id"] == "sid"


if __name__ == "__main__":
    test_rpc_version_is_required()
    test_worker_records_successful_mcp_call()
    test_worker_records_unknown_outcome()
    print("MCP_AUDIT_RPC_OK")
