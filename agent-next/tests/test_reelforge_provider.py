import os

from app.llm_provider import LLMProviderUnavailable
from app.reelforge_provider import PROVIDER_CONFIG, ReelForgeProvider
from app.reelforge_provider_factory import create_reelforge_provider


def test_provider_matrix():
    expected = {
        "openai",
        "groq",
        "gemini",
        "cerebras",
        "mistral",
        "openrouter",
    }
    assert set(PROVIDER_CONFIG) == expected

    for provider, cfg in PROVIDER_CONFIG.items():
        obj = ReelForgeProvider(provider)
        assert obj.provider == provider
        assert obj.model in cfg["models"]


def test_invalid_provider_and_model_are_rejected():
    try:
        ReelForgeProvider("invalid-provider")
    except LLMProviderUnavailable:
        pass
    else:
        raise AssertionError("invalid provider was accepted")

    try:
        ReelForgeProvider("groq", "invalid-model")
    except LLMProviderUnavailable:
        pass
    else:
        raise AssertionError("invalid model was accepted")


def test_missing_key_is_safe():
    old = os.environ.pop("GROQ_API_KEY", None)
    old_file = os.environ.get("REELFORGE_GROQ_KEY_FILE")
    os.environ["REELFORGE_GROQ_KEY_FILE"] = str(
        __import__("pathlib").Path.home() / "__missing_groq_test_key__"
    )
    try:
        try:
            ReelForgeProvider("groq").interpret("проверь состояние агента")
        except LLMProviderUnavailable as exc:
            assert "GROQ_API_KEY" in str(exc)
        else:
            raise AssertionError("provider ran without API key")
    finally:
        if old is not None:
            os.environ["GROQ_API_KEY"] = old
        if old_file is None:
            os.environ.pop("REELFORGE_GROQ_KEY_FILE", None)
        else:
            os.environ["REELFORGE_GROQ_KEY_FILE"] = old_file


def test_factory_selection():
    old_provider = os.environ.get("REELFORGE_LLM_PROVIDER")
    old_model = os.environ.get("REELFORGE_LLM_MODEL")

    try:
        os.environ["REELFORGE_LLM_PROVIDER"] = "gemini"
        os.environ["REELFORGE_LLM_MODEL"] = "gemini-3.6-flash"

        provider = create_reelforge_provider()

        assert provider.provider == "gemini"
        assert provider.model == "gemini-3.6-flash"
    finally:
        if old_provider is None:
            os.environ.pop("REELFORGE_LLM_PROVIDER", None)
        else:
            os.environ["REELFORGE_LLM_PROVIDER"] = old_provider

        if old_model is None:
            os.environ.pop("REELFORGE_LLM_MODEL", None)
        else:
            os.environ["REELFORGE_LLM_MODEL"] = old_model


if __name__ == "__main__":
    test_provider_matrix()
    test_invalid_provider_and_model_are_rejected()
    test_missing_key_is_safe()
    test_factory_selection()
    print("REELFORGE_PROVIDER_TEST=GREEN")
    print("PRODUCTION_MUTATION=NONE")


def test_provider_parses_mocked_json_response():
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            import json
            return json.dumps({
                "choices": [{
                    "message": {
                        "content": (
                            '{"capability":"health","arguments":{},'
                            '"operation":null,"profile":"ADMIN",'
                            '"confidence":"high"}'
                        )
                    }
                }]
            }).encode("utf-8")

    def fake_urlopen(request, timeout):
        assert request.full_url.endswith("/chat/completions")
        assert timeout == 30.0
        return FakeResponse()

    import urllib.request
    import os
    old_urlopen = urllib.request.urlopen
    old = os.environ.get("GROQ_API_KEY")
    urllib.request.urlopen = fake_urlopen
    os.environ["GROQ_API_KEY"] = "test-only-not-a-real-secret"

    try:
        intent = ReelForgeProvider("groq").interpret("проверь состояние агента")
    finally:
        urllib.request.urlopen = old_urlopen
        if old is None:
            os.environ.pop("GROQ_API_KEY", None)
        else:
            os.environ["GROQ_API_KEY"] = old

    assert intent.capability == "health"
    assert intent.arguments == {}
    assert intent.operation is None
    assert intent.profile == "ADMIN"
    assert intent.confidence == "high"

    print("R5_PROVIDER_MOCK_RESPONSE=GREEN")
    print("PRODUCTION_MUTATION=NONE")




def test_provider_parses_clarification_response():
    import json
    import os
    import urllib.request

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            payload = {
                "choices": [{
                    "message": {
                        "content": json.dumps({
                            "capability": "",
                            "arguments": {},
                            "operation": None,
                            "profile": "READ_ONLY",
                            "confidence": "high",
                            "needs_clarification": True,
                            "clarification_question": (
                                "Какой именно файл вы имеете в виду?"
                            ),
                        }, ensure_ascii=False),
                    }
                }]
            }
            return json.dumps(payload).encode("utf-8")

    def fake_urlopen(request, timeout):
        assert request.full_url.endswith("/chat/completions")
        return FakeResponse()

    old_urlopen = urllib.request.urlopen
    old_key = os.environ.get("GROQ_API_KEY")
    urllib.request.urlopen = fake_urlopen
    os.environ["GROQ_API_KEY"] = "test-only-not-a-real-secret"

    try:
        intent = ReelForgeProvider("groq").interpret(
            "Обработай тот файл"
        )
    finally:
        urllib.request.urlopen = old_urlopen
        if old_key is None:
            os.environ.pop("GROQ_API_KEY", None)
        else:
            os.environ["GROQ_API_KEY"] = old_key

    assert intent.needs_clarification is True
    assert intent.clarification_question == (
        "Какой именно файл вы имеете в виду?"
    )
    assert intent.capability == ""
    assert intent.arguments == {}

def test_factory_defaults_to_primary_only():
    old_values = {
        key: os.environ.get(key)
        for key in (
            "REELFORGE_LLM_PROVIDER",
            "REELFORGE_LLM_MODEL",
            "REELFORGE_LLM_FALLBACK_PROVIDERS",
        )
    }
    try:
        os.environ["REELFORGE_LLM_PROVIDER"] = "gemini"
        os.environ["REELFORGE_LLM_MODEL"] = "gemini-3.6-flash"
        os.environ.pop("REELFORGE_LLM_FALLBACK_PROVIDERS", None)

        provider = create_reelforge_provider()
        assert provider.provider == "gemini"
        assert provider.model == "gemini-3.6-flash"
        assert [item.provider for item in provider.providers] == ["gemini"]
    finally:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_factory_explicit_fallback_order():
    old_values = {
        key: os.environ.get(key)
        for key in (
            "REELFORGE_LLM_PROVIDER",
            "REELFORGE_LLM_MODEL",
            "REELFORGE_LLM_FALLBACK_PROVIDERS",
        )
    }
    try:
        os.environ["REELFORGE_LLM_PROVIDER"] = "groq"
        os.environ.pop("REELFORGE_LLM_MODEL", None)
        os.environ["REELFORGE_LLM_FALLBACK_PROVIDERS"] = " gemini, openrouter "

        provider = create_reelforge_provider()
        assert [item.provider for item in provider.providers] == [
            "groq", "gemini", "openrouter"
        ]
    finally:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_factory_rejects_invalid_fallback_configuration():
    old_values = {
        key: os.environ.get(key)
        for key in (
            "REELFORGE_LLM_PROVIDER",
            "REELFORGE_LLM_FALLBACK_PROVIDERS",
        )
    }
    try:
        os.environ["REELFORGE_LLM_PROVIDER"] = "groq"

        for value in ("unknown-provider", "gemini,gemini", "groq,gemini"):
            os.environ["REELFORGE_LLM_FALLBACK_PROVIDERS"] = value
            try:
                create_reelforge_provider()
            except LLMProviderUnavailable:
                pass
            else:
                raise AssertionError(
                    f"Invalid fallback configuration accepted: {value}"
                )
    finally:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_fallback_stops_after_first_success():
    from app.reelforge_provider_factory import FallbackLLMProvider
    from app.llm_provider import LLMIntent

    calls = []

    class FakeProvider:
        def __init__(self, name, result=None, failure=False):
            self.provider = name
            self.model = "offline-test-model"
            self.result = result
            self.failure = failure

        def interpret(self, text):
            calls.append(self.provider)
            if self.failure:
                raise LLMProviderUnavailable("offline simulated failure")
            return self.result

    expected = LLMIntent(
        capability="health",
        arguments={},
        operation=None,
        profile="READ_ONLY",
        confidence="high",
    )
    wrapper = FallbackLLMProvider([
        FakeProvider("primary", failure=True),
        FakeProvider("secondary", result=expected),
        FakeProvider("never-called", result=expected),
    ])

    result = wrapper.interpret("offline test")
    assert result is expected
    assert calls == ["primary", "secondary"]


def test_fallback_all_failures_are_sanitized():
    from app.reelforge_provider_factory import FallbackLLMProvider

    class FakeProvider:
        def __init__(self, name):
            self.provider = name
            self.model = "offline-test-model"

        def interpret(self, text):
            raise LLMProviderUnavailable("simulated failure with secret-marker")

    wrapper = FallbackLLMProvider([
        FakeProvider("primary"),
        FakeProvider("secondary"),
    ])

    try:
        wrapper.interpret("offline test")
    except LLMProviderUnavailable as exc:
        message = str(exc)
        assert "primary" in message
        assert "secondary" in message
        assert "secret-marker" not in message
    else:
        raise AssertionError("Expected all-provider failure")
