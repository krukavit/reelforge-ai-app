from __future__ import annotations

from app.voice_service import VoiceService
from app.speech import SpeechResult
from app.tts import TTSResult


class FakeSpeech:
    def __init__(self, result):
        self.result = result

    def transcribe_result(self):
        return self.result


class FakeConversation:
    def __init__(self):
        self.calls = []

    def handle(self, text, target_root=None):
        self.calls.append((text, target_root))
        return {
            "text": text,
            "capability": "system_info",
            "result": {"state": "COMPLETED"},
        }


class FakeTTS:
    def __init__(self, result=None):
        self.result = result or TTSResult(ok=True)
        self.calls = []

    def speak_result(self, text):
        self.calls.append(text)
        return self.result


def test_voice_service_forwards_transcribed_text_and_speaks_response():
    conversation = FakeConversation()
    tts = FakeTTS()

    service = VoiceService(
        speech=FakeSpeech(
            SpeechResult(
                text="Проверь состояние агента",
                ok=True,
            )
        ),
        conversation=conversation,
        tts=tts,
    )

    result = service.listen_and_handle(target_root="/tmp/project")

    assert result["state"] == "COMPLETED"
    assert result["text"] == "Проверь состояние агента"
    assert result["conversation"]["capability"] == "system_info"
    assert result["response"] == "Готово: Проверь состояние агента."
    assert result["tts"]["state"] == "COMPLETED"
    assert result["tts"]["error"] is None

    assert conversation.calls == [
        ("Проверь состояние агента", "/tmp/project")
    ]
    assert tts.calls == [
        "Готово: Проверь состояние агента."
    ]


def test_voice_service_preserves_conversation_success_when_tts_fails():
    conversation = FakeConversation()
    tts = FakeTTS(
        TTSResult(
            ok=False,
            error="TTS_FAILED:engine unavailable",
        )
    )

    service = VoiceService(
        speech=FakeSpeech(
            SpeechResult(
                text="Проверь состояние агента",
                ok=True,
            )
        ),
        conversation=conversation,
        tts=tts,
    )

    result = service.listen_and_handle()

    assert result["state"] == "COMPLETED"
    assert result["conversation"]["result"]["state"] == "COMPLETED"
    assert result["response"] == "Готово: Проверь состояние агента."
    assert result["tts"]["state"] == "FAILED"
    assert result["tts"]["error"] == "TTS_FAILED:engine unavailable"
    assert len(tts.calls) == 1


def test_voice_service_empty_stt_does_not_call_conversation_or_tts():
    conversation = FakeConversation()
    tts = FakeTTS()

    service = VoiceService(
        speech=FakeSpeech(
            SpeechResult(
                text="",
                ok=False,
                error="STT_EMPTY",
            )
        ),
        conversation=conversation,
        tts=tts,
    )

    result = service.listen_and_handle()

    assert result["state"] == "STT_EMPTY"
    assert result["text"] == ""
    assert result["error"] == "STT_EMPTY"
    assert conversation.calls == []
    assert tts.calls == []


def test_speakable_translates_known_errors():
    from app.voice_service import _speakable

    assert _speakable("Не удалось выполнить команду: path does not exist") == (
        "Не удалось выполнить команду: указанного пути не существует"
    )
    assert _speakable("Готово") == "Готово"

def test_voice_service_speaks_system_info_metrics():
    import json

    class SystemInfoConversation(FakeConversation):
        def handle(self, text, target_root=None):
            self.calls.append((text, target_root))
            stats = {
                "ram_mb_used": 2528,
                "ram_mb_total": 3413,
                "disk_mb_used": 73284,
                "disk_mb_total": 108335,
                "temp_celsius": 43,
                "cpu_percent": "800",
                "uptime_seconds": 0,
            }
            return {
                "text": text,
                "capability": "system_info",
                "result": {
                    "state": "COMPLETED",
                    "result": {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(stats),
                                }
                            ]
                        },
                    },
                },
            }

    conversation = SystemInfoConversation()
    tts = FakeTTS()
    service = VoiceService(
        speech=FakeSpeech(
            SpeechResult(text="Проверь состояние агента", ok=True)
        ),
        conversation=conversation,
        tts=tts,
    )

    result = service.listen_and_handle()

    expected = (
        "Состояние системы: память занята 2528 из 3413 мегабайт; "
        "диск занят 73284 из 108335 мегабайт; температура 43 градуса."
    )
    assert result["state"] == "COMPLETED"
    assert result["response"] == expected
    assert tts.calls == [expected]
    assert "загрузка процессора" not in tts.calls[0]
    assert "время работы" not in tts.calls[0]
