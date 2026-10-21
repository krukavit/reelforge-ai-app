from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.speech import SpeechResult, SpeechToText


class FakeCapture:
    def __init__(self, directory):
        self.path = Path(directory) / "test-audio.m4a"
        self.calls = []

    def record(self, seconds=5):
        self.calls.append(seconds)
        self.path.write_bytes(b"fake audio")
        return self.path


class FakeResponse:
    status_code = 200

    def json(self):
        return {"text": "Проверка русского распознавания речи"}


def test_groq_transcription_uses_russian_and_deletes_audio():
    with TemporaryDirectory() as tmp:
        capture = FakeCapture(tmp)
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse()

        with patch("app.speech.requests.post", side_effect=fake_post):
            stt = SpeechToText(
                provider="groq",
                api_key="test-key",
                voice_capture=capture,
            )
            result = stt._transcribe_groq("test-key")

        assert result == SpeechResult(
            "Проверка русского распознавания речи", True
        )
        assert capture.calls == [5]
        assert not capture.path.exists()
        assert calls[0][0] == SpeechToText.GROQ_URL
        assert calls[0][1]["data"]["language"] == "ru"
        assert calls[0][1]["data"]["model"] == "whisper-large-v3-turbo"
        assert calls[0][1]["headers"]["Authorization"] == "Bearer test-key"


def test_auto_provider_falls_back_to_termux_when_groq_fails():
    stt = SpeechToText(provider="auto", api_key="test-key")
    stt._transcribe_groq = lambda key: SpeechResult(
        "", False, "GROQ_STT_REQUEST_FAILED"
    )
    stt._transcribe_termux = lambda: SpeechResult("локальный текст", True)

    result = stt.transcribe_result()
    assert result.ok
    assert result.text == "локальный текст"


def test_auto_provider_uses_termux_when_key_is_missing():
    stt = SpeechToText(provider="auto", api_key="")
    stt._get_api_key = lambda: ""
    stt._transcribe_termux = lambda: SpeechResult("резервный текст", True)

    result = stt.transcribe_result()
    assert result.ok
    assert result.text == "резервный текст"


def test_termux_provider_skips_groq():
    stt = SpeechToText(provider="termux")
    stt._transcribe_termux = lambda: SpeechResult("локально", True)

    def unexpected_call(key):
        raise AssertionError("Groq should not be called")

    stt._transcribe_groq = unexpected_call
    result = stt.transcribe_result()

    assert result.ok
    assert result.text == "локально"
