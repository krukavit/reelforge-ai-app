import os
import sys

import json
import tempfile
from pathlib import Path

from app.capability_registry import CapabilityRegistry
from app.mcp_client import MCPClient


def test_registry_rejects_duplicate_names():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "registry.json"
        p.write_text(json.dumps({"result": {"tools": [{"name": "a"}, {"name": "a"}]}}), encoding="utf-8")
        try:
            CapabilityRegistry(p).load()
        except ValueError as exc:
            assert "duplicate" in str(exc)
        else:
            raise AssertionError("duplicate registry names accepted")


def test_registry_rejects_missing_name():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "registry.json"
        p.write_text(json.dumps({"result": {"tools": [{"description": "x"}]}}), encoding="utf-8")
        try:
            CapabilityRegistry(p).load()
        except ValueError as exc:
            assert "name" in str(exc)
        else:
            raise AssertionError("nameless registry tool accepted")


def test_rpc_error_is_not_treated_as_success():
    data = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "nope"}}
    try:
        MCPClient._validate_rpc_response(data)
    except RuntimeError as exc:
        assert "nope" in str(exc)
    else:
        raise AssertionError("JSON-RPC error accepted as success")


def test_rpc_result_is_accepted():
    data = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    assert MCPClient._validate_rpc_response(data)["result"]["ok"] is True


if __name__ == "__main__":
    test_registry_rejects_duplicate_names()
    test_registry_rejects_missing_name()
    test_rpc_error_is_not_treated_as_success()
    test_rpc_result_is_accepted()
    print("REGISTRY_RPC_OK")
