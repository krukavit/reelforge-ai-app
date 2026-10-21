from __future__ import annotations

import os

from .llm_provider import LLMIntent, LLMProvider, LLMProviderUnavailable
from .reelforge_provider import PROVIDER_CONFIG, ReelForgeProvider


class FallbackLLMProvider:
    """Try configured LLM adapters in order; never validates or executes intents."""

    def __init__(self, providers: list[LLMProvider]):
        self.providers = providers
        primary = providers[0] if providers else None
        self.provider = getattr(primary, "provider", "unconfigured")
        self.model = getattr(primary, "model", None)

    def interpret(self, text: str) -> LLMIntent:
        failures = []
        for provider in self.providers:
            name = getattr(provider, "provider", type(provider).__name__)
            try:
                return provider.interpret(text)
            except LLMProviderUnavailable:
                failures.append(str(name))

        attempted = ", ".join(failures) if failures else "none"
        raise LLMProviderUnavailable(
            f"No LLM provider succeeded; attempted: {attempted}"
        )


def create_reelforge_provider() -> LLMProvider:
    preferred = os.environ.get("REELFORGE_LLM_PROVIDER", "groq").strip()
    preferred_model = os.environ.get("REELFORGE_LLM_MODEL") or None

    if preferred not in PROVIDER_CONFIG:
        raise LLMProviderUnavailable(
            f"Unknown ReelForge provider: {preferred}"
        )

    fallback_value = os.environ.get(
        "REELFORGE_LLM_FALLBACK_PROVIDERS", ""
    )
    fallback_names = [
        item.strip()
        for item in fallback_value.split(",")
        if item.strip()
    ]

    if len(fallback_names) != len(set(fallback_names)):
        raise LLMProviderUnavailable(
            "Duplicate provider in REELFORGE_LLM_FALLBACK_PROVIDERS"
        )

    if preferred in fallback_names:
        raise LLMProviderUnavailable(
            "Primary provider must not appear in REELFORGE_LLM_FALLBACK_PROVIDERS"
        )

    unknown = [name for name in fallback_names if name not in PROVIDER_CONFIG]
    if unknown:
        raise LLMProviderUnavailable(
            "Unknown provider in REELFORGE_LLM_FALLBACK_PROVIDERS"
        )

    providers = [
        ReelForgeProvider(provider=preferred, model=preferred_model)
    ]
    providers.extend(
        ReelForgeProvider(provider=name)
        for name in fallback_names
    )
    return FallbackLLMProvider(providers)


__all__ = ["FallbackLLMProvider", "create_reelforge_provider"]
