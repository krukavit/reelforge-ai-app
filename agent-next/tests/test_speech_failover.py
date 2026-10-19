from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import patch

from app.speech import SpeechResult, SpeechToText
from app.gemini_stt import GeminiSTTError


class FakeResponse:
    status_code = 200

    def json(self):
        return {"text": "Текст от Groq"}


class SpeechFailoverTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audio = Path(self.tmp.name) / "sample.m4a"
        self.audio.write_bytes(b"mock audio only")

        self.stt = SpeechToText(provider="auto", timeout=1)
        self.stt._get_gemini_api_key = lambda: "mock-gemini-key"
        self.stt._get_api_key = lambda: "mock-groq-key"

    def test_gemini_success_skips_groq(self):
        with patch(
            "app.gemini_stt.transcribe_gemini_audio",
            return_value="Текст от Gemini",
        ) as gemini, patch("app.speech.requests.post") as groq:
            result = self.stt.transcribe_audio_file(self.audio)

        self.assertEqual(result, SpeechResult("Текст от Gemini", True))
        gemini.assert_called_once()
        groq.assert_not_called()

    def test_gemini_failure_falls_back_to_groq(self):
        with patch(
            "app.gemini_stt.transcribe_gemini_audio",
            side_effect=GeminiSTTError("GEMINI_STT_NETWORK_ERROR"),
        ), patch(
            "app.speech.requests.post", return_value=FakeResponse()
        ) as groq:
            result = self.stt.transcribe_audio_file(self.audio)

        self.assertEqual(result, SpeechResult("Текст от Groq", True))
        groq.assert_called_once()

    def test_missing_gemini_key_falls_back_to_groq(self):
        self.stt._get_gemini_api_key = lambda: ""

        with patch(
            "app.gemini_stt.transcribe_gemini_audio"
        ) as gemini, patch(
            "app.speech.requests.post", return_value=FakeResponse()
        ) as groq:
            result = self.stt.transcribe_audio_file(self.audio)

        self.assertEqual(result, SpeechResult("Текст от Groq", True))
        gemini.assert_not_called()
        groq.assert_called_once()

    def test_both_providers_fail_without_exposing_secrets(self):
        self.stt._get_gemini_api_key = lambda: ""
        self.stt._get_api_key = lambda: ""

        with patch("app.speech.requests.post") as groq:
            result = self.stt.transcribe_audio_file(self.audio)

        self.assertFalse(result.ok)
        self.assertIn("GEMINI_STT_KEY_MISSING", result.error)
        self.assertIn("GROQ_STT_KEY_MISSING", result.error)
        self.assertNotIn("mock-", result.error)
        groq.assert_not_called()

    def test_missing_audio_does_not_call_providers(self):
        with patch("app.gemini_stt.transcribe_gemini_audio") as gemini, patch(
            "app.speech.requests.post"
        ) as groq:
            result = self.stt.transcribe_audio_file(
                Path(self.tmp.name) / "missing.m4a"
            )

        self.assertEqual(result.error, "STT_AUDIO_MISSING")
        gemini.assert_not_called()
        groq.assert_not_called()


if __name__ == "__main__":
    main()
