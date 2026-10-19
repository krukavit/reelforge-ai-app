from .capability_registry import CapabilityRegistry
from .mcp_client import MCPClient, MCPUnknownOutcomeError
from .policy import authorize


class MCPWorker:
    def __init__(self, registry=None, client=None, state=None):
        self.registry = registry or CapabilityRegistry()
        self.client = client or MCPClient()
        self.state = state

    def call_tool(
        self,
        capability,
        arguments=None,
        operation=None,
        *,
        validated=False,
        confirmed=False,
        task_id=None,
    ):
        if capability in {"run", "session_run"}:
            raise PermissionError(
                f"MCP capability blocked by policy: {capability}"
            )

        if not self.registry.has(capability):
            raise PermissionError(
                f"MCP capability unavailable: {capability}"
            )

        allowed, reason = authorize(
            capability,
            operation,
            validated=validated,
            confirmed=confirmed,
        )

        if not allowed:
            raise PermissionError(
                f"MCP capability blocked: {capability} ({reason})"
            )

        call_arguments = dict(arguments or {})
        if confirmed:
            call_arguments["confirmed"] = True

        def audit(status, result=None, error=None):
            if self.state is None:
                return
            config = getattr(self.client, "config", {})
            self.state.add_mcp_call(
                capability, status=status, result=result, error=error,
                task_id=task_id, operation=operation, arguments=call_arguments,
                request_id=getattr(self.client, "request_id", None),
                session_id=getattr(self.client, "session_id", None),
                protocol_version=config.get("MCP_PROTOCOL") if hasattr(config, "get") else None,
            )

        try:
            result = self.client.tool_call(capability, call_arguments)
        except MCPUnknownOutcomeError as exc:
            audit("UNKNOWN_OUTCOME", error=str(exc))
            raise
        except Exception as exc:
            audit("FAILED", error=str(exc))
            raise
        audit("SUCCESS", result=result)
        return result
