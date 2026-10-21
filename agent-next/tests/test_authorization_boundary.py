from app.capability_registry import CapabilityRegistry
from app.mcp_worker import MCPWorker
from app.policy import authorize


def test_tool_metadata_cannot_grant_permission():
    # Metadata описывает capability, но не является полномочием.
    # Даже если metadata содержит "validated"/"confirmed", Policy
    # получает эти значения только из явных trusted arguments.
    metadata = {
        "name": "write",
        "description": "write files",
        "validated": True,
        "confirmed": True,
    }

    assert metadata["name"] == "write"

    ok, reason = authorize("write")
    assert ok is False
    assert reason == "CONFIRM_REQUIRED"


def test_tool_output_cannot_grant_permission():
    # Результат MCP-инструмента не должен становиться authorization input.
    tool_output = {
        "ok": True,
        "validated": True,
        "confirmed": True,
        "authorize": True,
    }

    assert tool_output["ok"] is True

    ok, reason = authorize("write")
    assert ok is False
    assert reason == "CONFIRM_REQUIRED"


def test_llm_claim_cannot_grant_permission():
    # Текст/утверждение LLM не является trusted authorization state.
    llm_claim = {
        "validated": True,
        "confirmed": True,
        "reason": "LLM says this action is safe",
    }

    assert llm_claim["validated"] is True

    ok, reason = authorize("write")
    assert ok is False
    assert reason == "CONFIRM_REQUIRED"

    ok, reason = authorize("write", validated=True)
    assert ok is False
    assert reason == "CONFIRM_REQUIRED"


def test_worker_requires_explicit_confirmation_for_mutation():
    class Registry:
        def has(self, name):
            return name == "write"

    class Client:
        def tool_call(self, name, arguments):
            raise AssertionError("blocked call reached MCP client")

    worker = MCPWorker(registry=Registry(), client=Client())

    try:
        worker.call_tool(
            "write",
            arguments={"path": "/tmp/example"},
            operation=None,
            validated=True,
        )
    except PermissionError as exc:
        assert "CONFIRM_REQUIRED" in str(exc)
    else:
        raise AssertionError("write reached MCP without explicit confirmation")


def test_unknown_capability_remains_denied():
    ok, reason = authorize(
        "llm_granted_capability",
        validated=True,
        confirmed=True,
    )
    assert ok is False
    assert reason == "DENY_BY_DEFAULT"


def test_always_blocked_capability_ignores_trusted_flags():
    for capability in ("run", "session_run"):
        ok, reason = authorize(
            capability,
            validated=True,
            confirmed=True,
        )
        assert ok is False
        assert reason == "ALWAYS_BLOCKED"


if __name__ == "__main__":
    test_tool_metadata_cannot_grant_permission()
    test_tool_output_cannot_grant_permission()
    test_llm_claim_cannot_grant_permission()
    test_worker_requires_explicit_confirmation_for_mutation()
    test_unknown_capability_remains_denied()
    test_always_blocked_capability_ignores_trusted_flags()
    print("AUTHORIZATION_BOUNDARY_OK")
