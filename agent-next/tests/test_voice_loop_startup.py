from __future__ import annotations

import contextlib
import io
import tempfile
from pathlib import Path

from app.voice_loop import VoiceLoop


class FakeSpeech:
    def __init__(self, events, error=None):
        self.events = events
        self.error = error

    def validate_configuration(self):
        self.events.append("stt_ready")
        if self.error:
            raise RuntimeError(self.error)
        return "fake-stt"


class FakeLLMProvider:
    provider = "fake-llm"
    model = "fake-model"

    def __init__(self, events, error=None):
        self.events = events
        self.error = error

    def validate_configuration(self):
        self.events.append("llm_ready")
        if self.error:
            raise RuntimeError(self.error)


class FakeLLM:
    def __init__(self, provider):
        self.providers = [provider]


class FakeService:
    def __init__(self, provider):
        self.conversation = type(
            "FakeConversation", (), {"llm": FakeLLM(provider)}
        )()


def make_loop(events, stt_error=None, llm_error=None):
    speech = FakeSpeech(events, stt_error)
    llm_provider = FakeLLMProvider(events, llm_error)
    service = FakeService(llm_provider)
    tempdir = tempfile.TemporaryDirectory()
    loop = VoiceLoop(
        speech=speech,
        service=service,
        capture_dir=Path(tempdir.name),
    )
    return loop, tempdir


def test_voice_loop_checks_providers_before_microphone():
    events = []
    loop, tempdir = make_loop(events)
    loop._start_recording = lambda path: (
        events.append("microphone"),
        (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    output = io.StringIO()

    try:
        with contextlib.redirect_stdout(output):
            loop.run()
    finally:
        tempdir.cleanup()

    lines = output.getvalue().splitlines()
    assert events == ["stt_ready", "llm_ready", "microphone"], events
    assert any(line.startswith("PROVIDERS_READY ") for line in lines), lines
    assert lines.index(next(x for x in lines if x.startswith("PROVIDERS_READY "))) < (
        lines.index("VOICE_LOOP=LISTENING")
    )


def test_voice_loop_stt_preflight_failure_never_starts_microphone():
    events = []
    loop, tempdir = make_loop(events, stt_error="STT_KEY_MISSING")
    loop._start_recording = lambda path: (_ for _ in ()).throw(
        AssertionError("microphone must not start")
    )
    output = io.StringIO()

    try:
        try:
            with contextlib.redirect_stdout(output):
                loop.run()
        except SystemExit as exc:
            assert exc.code == 2, exc.code
        else:
            raise AssertionError("expected SystemExit(2)")
    finally:
        tempdir.cleanup()

    assert events == ["stt_ready"], events
    assert "VOICE_LOOP=PROVIDER_NOT_READY reason=STT_KEY_MISSING" in output.getvalue()
    assert "PROVIDERS_READY" not in output.getvalue()
    assert "VOICE_LOOP=LISTENING" not in output.getvalue()


def test_voice_loop_llm_preflight_failure_never_starts_microphone():
    events = []
    loop, tempdir = make_loop(events, llm_error="LLM_KEY_MISSING")
    loop._start_recording = lambda path: (_ for _ in ()).throw(
        AssertionError("microphone must not start")
    )
    output = io.StringIO()

    try:
        try:
            with contextlib.redirect_stdout(output):
                loop.run()
        except SystemExit as exc:
            assert exc.code == 2, exc.code
        else:
            raise AssertionError("expected SystemExit(2)")
    finally:
        tempdir.cleanup()

    assert events == ["stt_ready", "llm_ready"], events
    assert "VOICE_LOOP=PROVIDER_NOT_READY reason=LLM_KEY_MISSING" in output.getvalue()
    assert "PROVIDERS_READY" not in output.getvalue()
    assert "VOICE_LOOP=LISTENING" not in output.getvalue()

def test_voice_loop_preflight_uses_ready_fallback_provider():
    events = []
    primary = FakeLLMProvider(events, error="PRIMARY_UNAVAILABLE")
    primary.provider = "primary"
    secondary = FakeLLMProvider(events)
    secondary.provider = "secondary"
    secondary.model = "secondary-model"

    loop = VoiceLoop(
        speech=FakeSpeech(events),
        service=FakeService(primary),
        capture_dir=Path(tempfile.mkdtemp()),
    )
    loop.service.conversation.llm.providers.append(secondary)

    result = loop._prepare_providers()

    assert result == ("fake-stt", "secondary", "secondary-model"), result
    assert events == ["stt_ready", "llm_ready", "llm_ready"], events


def test_voice_loop_all_llm_providers_unavailable():
    events = []
    primary = FakeLLMProvider(events, error="PRIMARY_UNAVAILABLE")
    secondary = FakeLLMProvider(events, error="SECONDARY_UNAVAILABLE")
    secondary.provider = "secondary"

    loop = VoiceLoop(
        speech=FakeSpeech(events),
        service=FakeService(primary),
        capture_dir=Path(tempfile.mkdtemp()),
    )
    loop.service.conversation.llm.providers.append(secondary)

    try:
        loop._prepare_providers()
    except RuntimeError as exc:
        assert str(exc) == "PRIMARY_UNAVAILABLE", str(exc)
    else:
        raise AssertionError("expected preflight failure")

    assert events == ["stt_ready", "llm_ready", "llm_ready"], events
