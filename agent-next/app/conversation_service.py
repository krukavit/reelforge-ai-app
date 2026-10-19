from __future__ import annotations

from pathlib import Path
import re

from .conversation import ConversationAmbiguityError, RussianConversationParser
from .executor import TaskExecutor
from .llm_provider import LLMProviderUnavailable, UnconfiguredLLMProvider
from .llm_intent_validator import LLMIntentValidator, LLMIntentValidationError
from .reelforge_provider_factory import create_reelforge_provider


class ConversationService:
    """Thin text-to-Agent-Next execution adapter.

    The conversation layer may parse and dispatch an intent, but it never
    grants confirmation authority. Destructive actions therefore flow through
    the existing TaskExecutor confirmation boundary.
    """

    def __init__(self, parser=None, executor=None, llm=None, llm_validator=None):
        self.parser = parser or RussianConversationParser()
        self.executor = executor or TaskExecutor()
        self.llm = llm or create_reelforge_provider()
        self.llm_validator = llm_validator or LLMIntentValidator()

    @staticmethod
    def _task_id(capability: str) -> str:
        from uuid import uuid4
        return f"chat-{capability}-{uuid4().hex[:12]}"

    @staticmethod
    def _combined_status_clarification(text: str) -> str | None:
        normalized = str(text or "").strip().lower()
        mentions_phone = re.search(r"\bтелефон\w*\b", normalized)
        mentions_agent = re.search(r"\bагент\w*\b", normalized)
        mentions_check = re.search(
            r"\b(?:проверь\w*|провер\w*|состояни\w*)\b",
            normalized,
        )
        if mentions_phone and mentions_agent and mentions_check:
            return "Уточните: проверить состояние телефона, агента или оба состояния?"
        return None

    def handle(self, text: str, target_root=None) -> dict:
        question = self._combined_status_clarification(text)
        if question:
            return {
                "text": str(text or "").strip(),
                "needs_clarification": True,
                "error": question,
            }
        try:
            intent = self.parser.parse(text)
        except ConversationAmbiguityError as ambiguity_error:
            return {
                "text": str(text or "").strip(),
                "error": str(ambiguity_error),
            }
        except ValueError as parser_error:
            try:
                llm_intent = self.llm.interpret(text)
                if getattr(llm_intent, "needs_clarification", False):
                    question = str(
                        getattr(llm_intent, "clarification_question", "") or ""
                    ).strip()
                    if not question:
                        question = "Уточните, пожалуйста, что именно нужно сделать."
                    return {
                        "text": str(text or "").strip(),
                        "needs_clarification": True,
                        "error": question,
                    }
                validated = self.llm_validator.validate(llm_intent)
            except LLMProviderUnavailable:
                return {
                    "text": str(text or "").strip(),
                    "error": str(parser_error),
                }
            except LLMIntentValidationError as exc:
                return {
                    "text": str(text or "").strip(),
                    "error": f"LLM intent rejected: {exc}",
                }

            from types import SimpleNamespace

            intent = SimpleNamespace(
                text=str(text or "").strip(),
                capability=validated.capability,
                arguments=dict(validated.arguments),
                profile=validated.profile,
                operation=validated.operation,
                allowed_by_policy=False,
                policy_reason="",
                confidence=validated.confidence,
            )

        # Conversational commands operate on the Agent Next project by default.
        # Explicit target_root remains authoritative when supplied by the caller.
        if target_root is None:
            target_root = str(Path(__file__).resolve().parent.parent)

        task_id = self._task_id(intent.capability)

        try:
            result = self.executor.execute(
                task_id,
                intent.capability,
                arguments=intent.arguments,
                operation=intent.operation,
                profile=intent.profile,
                validated=False,
                target_root=target_root,
            )
        except (ValueError, FileNotFoundError) as exc:
            return {
                "text": intent.text,
                "task_id": task_id,
                "capability": intent.capability,
                "arguments": intent.arguments,
                "profile": intent.profile,
                "operation": intent.operation,
                "policy_allowed": intent.allowed_by_policy,
                "policy_reason": intent.policy_reason,
                "error": str(exc),
            }

        return {
            "text": intent.text,
            "task_id": task_id,
            "capability": intent.capability,
            "arguments": intent.arguments,
            "profile": intent.profile,
            "operation": intent.operation,
            "policy_allowed": intent.allowed_by_policy,
            "policy_reason": intent.policy_reason,
            "result": result,
        }

    def confirm(self, task_id: str, confirmation_token: str, arguments=None) -> dict:
        """Resume one specific pending task through the existing confirmation boundary."""
        return self.executor.confirm_and_resume(
            task_id,
            confirmation_token,
            arguments=arguments,
        )


__all__ = ["ConversationService"]
