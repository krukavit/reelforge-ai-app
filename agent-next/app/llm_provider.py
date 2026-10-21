from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class LLMIntent:
    """Untrusted interpretation produced by an LLM.

    This object is descriptive only. It never grants policy permission,
    confirmation, or execution authority.
    """

    capability: str
    arguments: Mapping[str, Any]
    operation: str | None = None
    profile: str = "READ_ONLY"
    confidence: str = "unknown"
    needs_clarification: bool = False
    clarification_question: str | None = None


class LLMProvider(Protocol):
    """Provider contract for conversational intent interpretation."""

    def interpret(self, text: str) -> LLMIntent:
        ...


class LLMProviderUnavailable(RuntimeError):
    """Raised when no LLM provider is configured."""


class UnconfiguredLLMProvider:
    """Safe default: conversational LLM fallback is disabled."""

    def interpret(self, text: str) -> LLMIntent:
        raise LLMProviderUnavailable(
            "LLM provider is not configured"
        )


__all__ = [
    "LLMIntent",
    "LLMProvider",
    "LLMProviderUnavailable",
    "UnconfiguredLLMProvider",
]
