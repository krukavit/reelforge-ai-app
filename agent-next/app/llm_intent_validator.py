from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .capability_registry import CapabilityRegistry
from .llm_provider import LLMIntent


class LLMIntentValidationError(ValueError):
    """LLM returned an intent outside the allowed capability contract."""


@dataclass(frozen=True)
class ValidatedLLMIntent:
    capability: str
    arguments: dict[str, Any]
    operation: str | None
    profile: str
    confidence: str


class LLMIntentValidator:
    """Validate untrusted LLM intent against the live capability registry.

    The LLM has no confirmation authority. The `confirmed` argument is
    forbidden even when an MCP tool schema exposes it.
    """

    _FORBIDDEN_ARGUMENTS = frozenset({"confirmed"})

    def __init__(self, registry: CapabilityRegistry | None = None):
        self.registry = registry or CapabilityRegistry()

    def validate(self, intent: LLMIntent) -> ValidatedLLMIntent:
        if not isinstance(intent, LLMIntent):
            raise LLMIntentValidationError("intent must be LLMIntent")

        capability = intent.capability
        if not isinstance(capability, str) or not capability:
            raise LLMIntentValidationError("invalid capability")

        if capability in {"run", "session_run"}:
            raise LLMIntentValidationError(
                f"capability is globally blocked: {capability}"
            )

        schema_tool = self.registry.get(capability)
        if not isinstance(schema_tool, dict):
            raise LLMIntentValidationError(
                f"unknown capability: {capability}"
            )

        schema = schema_tool.get("inputSchema") or {}
        if not isinstance(schema, dict):
            raise LLMIntentValidationError(
                f"invalid input schema: {capability}"
            )

        properties = schema.get("properties") or {}
        required = schema.get("required") or []

        if not isinstance(properties, dict):
            raise LLMIntentValidationError(
                f"invalid input properties: {capability}"
            )

        arguments = dict(intent.arguments or {})

        forbidden = sorted(set(arguments) & self._FORBIDDEN_ARGUMENTS)
        if forbidden:
            raise LLMIntentValidationError(
                f"forbidden LLM arguments: {', '.join(forbidden)}"
            )

        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise LLMIntentValidationError(
                f"unknown arguments for {capability}: {', '.join(unknown)}"
            )

        missing = [
            name for name in required
            if name not in arguments
        ]
        if missing:
            raise LLMIntentValidationError(
                f"missing required arguments for {capability}: "
                + ", ".join(missing)
            )

        for name, value in arguments.items():
            self._validate_value(
                capability,
                name,
                value,
                properties[name],
            )

        from .policy import COMPOSITE_POLICIES

        operation = intent.operation
        if capability in COMPOSITE_POLICIES:
            allowed_operations = COMPOSITE_POLICIES[capability]
            if "*" not in allowed_operations:
                if operation not in allowed_operations:
                    raise LLMIntentValidationError(
                        f"invalid operation for {capability}: {operation!r}"
                    )
        elif operation is not None:
            raise LLMIntentValidationError(
                f"operation is not allowed for {capability}"
            )

        return ValidatedLLMIntent(
            capability=capability,
            arguments=arguments,
            operation=operation,
            profile="READ_ONLY",
            confidence=intent.confidence,
        )

    @staticmethod
    def _validate_value(
        capability: str,
        name: str,
        value: Any,
        schema: dict[str, Any],
    ) -> None:
        if "enum" in schema and value not in schema["enum"]:
            raise LLMIntentValidationError(
                f"invalid value for {capability}.{name}: {value!r}"
            )

        expected = schema.get("type")

        if expected == "string" and not isinstance(value, str):
            raise LLMIntentValidationError(
                f"{capability}.{name} must be string"
            )

        if expected == "integer" and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            raise LLMIntentValidationError(
                f"{capability}.{name} must be integer"
            )

        if expected == "boolean" and not isinstance(value, bool):
            raise LLMIntentValidationError(
                f"{capability}.{name} must be boolean"
            )

        if expected == "array" and not isinstance(value, list):
            raise LLMIntentValidationError(
                f"{capability}.{name} must be array"
            )

        if isinstance(value, int) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                raise LLMIntentValidationError(
                    f"{capability}.{name} below minimum"
                )
            if "maximum" in schema and value > schema["maximum"]:
                raise LLMIntentValidationError(
                    f"{capability}.{name} above maximum"
                )


__all__ = [
    "LLMIntentValidationError",
    "ValidatedLLMIntent",
    "LLMIntentValidator",
]
