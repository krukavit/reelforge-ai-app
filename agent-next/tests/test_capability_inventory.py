import hashlib
import json
import tempfile
from pathlib import Path

from app.capability_inventory import CapabilityInventory


def _tools(*names):
    return [
        {
            "name": name,
            "description": f"tool {name}",
            "inputSchema": {"type": "object"},
        }
        for name in names
    ]


def test_inventory_normalizes_live_tools():
    inv = CapabilityInventory()

    result = inv.build_snapshot(_tools("write", "read"))

    assert result["version"] == 1
    assert result["source"] == "live_tools_list"
    assert result["tools_count"] == 2
    assert result["fingerprint"]
    assert result["tools"]["read"]["status"] == "AVAILABLE"
    assert result["tools"]["write"]["status"] == "AVAILABLE"


def test_inventory_fingerprint_is_deterministic():
    inv = CapabilityInventory()

    a = inv.build_snapshot(_tools("b", "a"))
    b = inv.build_snapshot(_tools("a", "b"))

    assert a["fingerprint"] == b["fingerprint"]


def test_inventory_does_not_mark_available_as_proven():
    inv = CapabilityInventory()

    result = inv.build_snapshot(_tools("read"))

    assert result["tools"]["read"]["status"] == "AVAILABLE"
    assert result["tools"]["read"]["proven_correct"] is False


def test_inventory_can_mark_explicitly_proven_capability():
    inv = CapabilityInventory()

    result = inv.build_snapshot(
        _tools("read"),
        proven_correct={"read"},
    )

    assert result["tools"]["read"]["status"] == "PROVEN_CORRECT"
    assert result["tools"]["read"]["proven_correct"] is True


def test_inventory_rejects_duplicate_live_tools():
    inv = CapabilityInventory()

    try:
        inv.build_snapshot(_tools("read", "read"))
    except ValueError as exc:
        assert "duplicate" in str(exc).lower()
    else:
        raise AssertionError("duplicate live tools accepted")


def test_inventory_atomic_persistence():
    inv = CapabilityInventory()

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "runtime_capability_inventory.json"
        snapshot = inv.build_snapshot(_tools("read"))

        inv.save_atomic(path, snapshot)

        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded == snapshot
        assert path.is_file()


if __name__ == "__main__":
    tests = [
        test_inventory_normalizes_live_tools,
        test_inventory_fingerprint_is_deterministic,
        test_inventory_does_not_mark_available_as_proven,
        test_inventory_can_mark_explicitly_proven_capability,
        test_inventory_rejects_duplicate_live_tools,
        test_inventory_atomic_persistence,
    ]

    for test in tests:
        test()

    print("CAPABILITY_INVENTORY_TESTS=6/6")
    print("CAPABILITY_INVENTORY_GREEN")


def test_reconcile_uses_live_tools_list_only():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def tools_list(self):
            self.calls.append("tools/list")
            return {
                "jsonrpc": "2.0",
                "id": 7,
                "result": {
                    "tools": _tools("read", "write"),
                },
            }

    inv = CapabilityInventory()
    client = FakeClient()

    result = inv.reconcile(client)

    assert client.calls == ["tools/list"]
    assert result["source"] == "live_tools_list"
    assert result["tools_count"] == 2
    assert set(result["tools"]) == {"read", "write"}


def test_reconcile_does_not_use_bootstrap_registry_for_runtime_status():
    class FakeClient:
        def tools_list(self):
            return {
                "result": {
                    "tools": _tools("read"),
                }
            }

    inv = CapabilityInventory()

    result = inv.reconcile(FakeClient())

    assert set(result["tools"]) == {"read"}
    assert "write" not in result["tools"]
    assert result["tools"]["read"]["status"] == "AVAILABLE"


def test_reconcile_changes_fingerprint_when_live_inventory_changes():
    class FakeClient:
        def __init__(self, tools):
            self._tools = tools

        def tools_list(self):
            return {"result": {"tools": self._tools}}

    inv = CapabilityInventory()

    first = inv.reconcile(FakeClient(_tools("read")))
    second = inv.reconcile(FakeClient(_tools("read", "write")))

    assert first["fingerprint"] != second["fingerprint"]


def test_reconcile_live_error_does_not_create_inventory():
    class FailingClient:
        def tools_list(self):
            raise RuntimeError("live discovery failed")

    inv = CapabilityInventory()

    try:
        inv.reconcile(FailingClient())
    except RuntimeError as exc:
        assert "live discovery failed" in str(exc)
    else:
        raise AssertionError("live discovery failure was swallowed")


if __name__ == "__main__":
    tests = [
        test_inventory_normalizes_live_tools,
        test_inventory_fingerprint_is_deterministic,
        test_inventory_does_not_mark_available_as_proven,
        test_inventory_can_mark_explicitly_proven_capability,
        test_inventory_rejects_duplicate_live_tools,
        test_inventory_atomic_persistence,
        test_reconcile_uses_live_tools_list_only,
        test_reconcile_does_not_use_bootstrap_registry_for_runtime_status,
        test_reconcile_changes_fingerprint_when_live_inventory_changes,
        test_reconcile_live_error_does_not_create_inventory,
    ]

    for test in tests:
        test()

    print("CAPABILITY_INVENTORY_TESTS=10/10")
    print("CAPABILITY_INVENTORY_GREEN")
