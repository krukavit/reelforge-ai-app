from __future__ import annotations

from .conversation_response import format_conversation_result
from .conversation_service import ConversationService
from .speech import SpeechToText
from .tts import TextToSpeech


_SPOKEN_RU = (
    ("path does not exist", "указанного пути не существует"),
    ("no such file", "файл не найден"),
    ("permission denied", "нет доступа"),
)


def _speakable(text: str) -> str:
    """Translate known English tool errors for speech only."""
    result = str(text)
    for needle, replacement in _SPOKEN_RU:
        index = result.lower().find(needle)
        if index != -1:
            result = result[:index] + replacement + result[index + len(needle):]
    return result


class VoiceService:
    """Bridge voice input through conversation and back to spoken output."""

    def __init__(self, speech=None, conversation=None, tts=None):
        self.speech = speech or SpeechToText()
        self.conversation = conversation or ConversationService()
        self.tts = tts or TextToSpeech()

    def handle_text(self, text: str, target_root=None) -> dict:
        """Handle recognized text and speak the conversation response."""
        result = self.conversation.handle(
            text,
            target_root=target_root,
        )

        response_text = format_conversation_result(result)
        tts_result = self.tts.speak_result(_speakable(response_text))

        return {
            "text": text,
            "state": "COMPLETED",
            "response": response_text,
            "conversation": result,
            "tts": {
                "state": "COMPLETED" if tts_result.ok else "FAILED",
                "error": tts_result.error,
            },
        }

    def listen_and_handle(self, target_root=None) -> dict:
        speech_result = self.speech.transcribe_result()

        if not speech_result.ok:
            return {
                "text": "",
                "state": "STT_EMPTY",
                "error": speech_result.error,
            }

        return self.handle_text(
            speech_result.text,
            target_root=target_root,
        )


__all__ = ["VoiceService"]
