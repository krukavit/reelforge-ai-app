from app.conversation_service import ConversationService
from app.llm_provider import LLMIntent


class FakeLLM:
    def __init__(self, intent):
        self.intent = intent

    def interpret(self, text):
        return self.intent


def test_all_ambiguous_modifying_commands_stop_before_llm():
    cases = [
        (
            "Удали какой-нибудь файл",
            "Не удалось однозначно определить файл для удаления.",
        ),
        (
            "Запиши hello в какой-нибудь файл",
            "Не удалось однозначно определить файл для записи.",
        ),
        (
            "Создай какую-нибудь папку",
            "Не удалось однозначно определить папку для создания.",
        ),
    ]

    for text, expected in cases:
        calls = []

        class SpyLLM:
            def interpret(self, value):
                calls.append(value)
                return LLMIntent(
                    capability="system_info",
                    arguments={},
                )

        service = ConversationService(llm=SpyLLM())
        result = service.handle(text)

        assert expected in result["error"]
        assert calls == []


def test_r5_concrete_modify_reaches_confirmation():
    class SpyExecutor:
        def __init__(self):
            self.calls = []

        def execute(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {
                "state": "CONFIRM_REQUIRED",
                "reason": "CONFIRM_REQUIRED",
                "confirmation_token": "test-token",
            }

    executor = SpyExecutor()
    service = ConversationService(executor=executor)

    result = service.handle("Удали файл test.txt")

    assert result["capability"] == "delete"
    assert result["result"]["state"] == "CONFIRM_REQUIRED"
    assert result["result"]["reason"] == "CONFIRM_REQUIRED"
    assert len(executor.calls) == 1
    args, kwargs = executor.calls[0]
    assert args[1] == "delete"
    assert kwargs["arguments"]["path"] == "test.txt"
    assert kwargs["validated"] is False
    assert kwargs["profile"] == "EDIT"


def test_r5_conversation_routing_matrix():
    parser_calls = []

    class SpyLLM:
        def interpret(self, text):
            parser_calls.append(text)
            return LLMIntent(
                capability="system_info",
                arguments={},
            )

    service = ConversationService(llm=SpyLLM())

    # 1. Known READ_ONLY command: deterministic parser must win.
    result = service.handle("проверь здоровье")
    assert result["capability"] == "health"
    assert parser_calls == []

    # 2. Unknown command: parser misses, LLM fallback is allowed.
    result = service.handle("Сделай что-нибудь необычное")
    assert result["capability"] == "system_info"
    assert parser_calls == ["Сделай что-нибудь необычное"]

    # 3. Ambiguous modifying command: hard stop, no LLM fallback.
    parser_calls.clear()
    result = service.handle("Удали какой-нибудь файл")
    assert "Не удалось однозначно определить файл для удаления." in result["error"]
    assert parser_calls == []


def test_parser_ambiguity_does_not_fallback_to_llm():
    calls = []

    class SpyLLM:
        def interpret(self, text):
            calls.append(text)
            return LLMIntent(
                capability="system_info",
                arguments={},
            )

    service = ConversationService(llm=SpyLLM())
    result = service.handle("Удали какой-нибудь файл")

    assert result["error"] == (
        "Не удалось однозначно определить файл для удаления. "
        "Укажите конкретный путь."
    )
    assert calls == []



def test_llm_clarification_stops_before_executor():
    class ClarifyingLLM:
        def interpret(self, text):
            return LLMIntent(
                capability="",
                arguments={},
                needs_clarification=True,
                clarification_question="Уточните, какой именно файл имеется в виду.",
            )

    class SpyExecutor:
        def __init__(self):
            self.calls = []

        def execute(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {"state": "UNEXPECTED_EXECUTION"}

    executor = SpyExecutor()
    service = ConversationService(
        executor=executor,
        llm=ClarifyingLLM(),
    )

    result = service.handle("Обработай этот файл необычным способом")

    assert result["needs_clarification"] is True
    assert result["error"] == "Уточните, какой именно файл имеется в виду."
    assert executor.calls == []


def test_llm_clarification_uses_fallback_question_when_empty():
    class ClarifyingLLM:
        def interpret(self, text):
            return LLMIntent(
                capability="",
                arguments={},
                needs_clarification=True,
                clarification_question="  ",
            )

    class SpyExecutor:
        def __init__(self):
            self.calls = []

        def execute(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {"state": "UNEXPECTED_EXECUTION"}

    executor = SpyExecutor()
    service = ConversationService(
        executor=executor,
        llm=ClarifyingLLM(),
    )

    result = service.handle("Сделай что-нибудь")

    assert result["needs_clarification"] is True
    assert result["error"] == "Уточните, пожалуйста, что именно нужно сделать."
    assert executor.calls == []

def test_llm_validated_intent_reaches_executor():
    class FakeExecutor:
        def __init__(self):
            self.calls = []

        def execute(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {"state": "COMPLETED", "result": "test-ok"}

    executor = FakeExecutor()
    service = ConversationService(
        executor=executor,
        llm=FakeLLM(
            LLMIntent(
                capability="system_info",
                arguments={},
            )
        )
    )

    result = service.handle("Сделай что-нибудь необычное")

    assert result["capability"] == "system_info"
    assert result["result"]["state"] == "COMPLETED"
    assert len(executor.calls) == 1
    args, kwargs = executor.calls[0]
    assert args[1] == "system_info"
    assert kwargs["arguments"] == {}
    assert kwargs["validated"] is False
    assert kwargs["profile"] == "READ_ONLY"


def test_llm_confirmed_argument_is_rejected():
    service = ConversationService(
        llm=FakeLLM(
            LLMIntent(
                capability="delete",
                arguments={
                    "path": "__llm_probe__",
                    "confirmed": True,
                },
            )
        )
    )

    result = service.handle("Сделай что-нибудь необычное")

    assert "LLM intent rejected" in result["error"]
    assert "confirmed" in result["error"]


def test_llm_unknown_capability_is_rejected():
    service = ConversationService(
        llm=FakeLLM(
            LLMIntent(
                capability="execute_anything",
                arguments={},
            )
        )
    )

    result = service.handle("Сделай что-нибудь необычное")

    assert "LLM intent rejected" in result["error"]
    assert "unknown capability" in result["error"]


def test_unconfigured_llm_preserves_parser_error():
    service = ConversationService()

    result = service.handle("Совсем непонятная фраза")

    assert "Не удалось однозначно" in result["error"]


print("CONVERSATION_LLM_TEST_FILE=GREEN")
print("PRODUCTION_MUTATION=NONE")


def test_llm_profile_cannot_escalate():
    from app.llm_intent_validator import LLMIntentValidator

    result = LLMIntentValidator().validate(
        LLMIntent(
            capability="system_info",
            arguments={},
            profile="EDIT",
        )
    )

    assert result.profile == "READ_ONLY"


def test_llm_operation_cannot_be_injected_for_non_composite():
    from app.llm_intent_validator import (
        LLMIntentValidationError,
        LLMIntentValidator,
    )

    try:
        LLMIntentValidator().validate(
            LLMIntent(
                capability="system_info",
                arguments={},
                operation="anything",
            )
        )
    except LLMIntentValidationError:
        return

    raise AssertionError("operation injection was accepted")


def test_llm_composite_operation_uses_policy():
    from app.llm_intent_validator import LLMIntentValidator

    result = LLMIntentValidator().validate(
        LLMIntent(
            capability="cloud_sync",
            arguments={},
            operation="list",
            profile="EDIT",
        )
    )

    assert result.operation == "list"
    assert result.profile == "READ_ONLY"


print("CONVERSATION_LLM_TEST_FILE=GREEN")
print("PRODUCTION_MUTATION=NONE")


def test_provider_validator_executor_e2e_boundary():
    from app.conversation_service import ConversationService
    from app.llm_provider import LLMIntent

    class FakeLLM:
        def interpret(self, text):
            return LLMIntent(
                capability="health",
                arguments={},
                operation=None,
                profile="ADMIN",
                confidence="high",
            )

    class FakeExecutor:
        def __init__(self):
            self.calls = []

        def execute(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {"state": "COMPLETED", "result": "health-ok"}

    executor = FakeExecutor()
    service = ConversationService(
        executor=executor,
        llm=FakeLLM(),
    )

    result = service.handle("Сделай мне что-нибудь полезное")

    assert result["result"]["state"] == "COMPLETED"
    assert len(executor.calls) == 1

    args, kwargs = executor.calls[0]

    assert args[1] == "health"
    assert kwargs["arguments"] == {}
    assert kwargs["profile"] == "READ_ONLY"
    assert kwargs["validated"] is False
    assert "confirmed" not in kwargs.get("arguments", {})

    print("R5_CONVERSATION_LLM_EXECUTOR_E2E=GREEN")
    print("R5_LLM_ADMIN_DOWNGRADE_E2E=GREEN")
    print("PRODUCTION_MUTATION=NONE")

def test_llm_provider_error_keeps_user_facing_parser_error():
    from app.llm_provider import LLMProviderUnavailable

    class FailingLLM:
        def interpret(self, text):
            raise LLMProviderUnavailable("simulated provider outage")

    service = ConversationService(llm=FailingLLM())
    result = service.handle("Сделай что-нибудь непонятное")

    assert result["error"] == "Не удалось однозначно определить команду из русской фразы"


def test_llm_validation_error_is_recorded_separately():
    service = ConversationService(
        llm=FakeLLM(
            LLMIntent(
                capability="execute_anything",
                arguments={},
            )
        )
    )

    result = service.handle("Сделай что-нибудь непонятное")

    assert result["error"].startswith("LLM intent rejected:")
    assert "unknown capability" in result["error"]


print("R5_LLM_ERROR_CONTRACT_TESTS=GREEN")
print("PRODUCTION_MUTATION=NONE")


def test_combined_phone_and_agent_status_asks_before_execution():
    question = "Уточните: проверить состояние телефона, агента или оба состояния?"

    class MustNotRunLLM:
        def interpret(self, text):
            raise AssertionError("LLM must not run before clarification")

    class SpyExecutor:
        def __init__(self):
            self.calls = []

        def execute(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            raise AssertionError("Executor must not run before clarification")

    executor = SpyExecutor()
    service = ConversationService(executor=executor, llm=MustNotRunLLM())
    result = service.handle(
        "Надо проверить не только телефона состояние, но и состояние агента."
    )

    assert result["needs_clarification"] is True
    assert result["error"] == question
    assert executor.calls == []

def test_combined_status_clarification_bypasses_parser_llm_and_executor():
    from app.conversation_service import ConversationService

    class MustNotRunParser:
        def parse(self, text):
            raise AssertionError("Parser ran before clarification")

    class MustNotRunLLM:
        def interpret(self, text):
            raise AssertionError("LLM ran before clarification")

    class SpyExecutor:
        def __init__(self):
            self.calls = []

        def execute(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            raise AssertionError("Executor ran before clarification")

    executor = SpyExecutor()
    service = ConversationService(
        parser=MustNotRunParser(),
        executor=executor,
        llm=MustNotRunLLM(),
    )
    result = service.handle(
        "Надо проверить не только телефона состояние, но и состояние агента."
    )

    assert result.get("needs_clarification") is True
    assert result.get("error") == (
        "Уточните: проверить состояние телефона, агента или оба состояния?"
    )
    assert executor.calls == []
