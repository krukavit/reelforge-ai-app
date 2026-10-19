from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from app.gemini_stt import (
    GeminiSTTError,
    transcribe_gemini_audio,
)


class FakeResponse:
    def __init__(self, status_code=200, data=None, headers=None):
        self.status_code = status_code
        self._data = data if data is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._data


class GeminiSTTTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.audio = Path(self.temp.name) / "sample.wav"
        self.audio.write_bytes(b"fake-audio-bytes")

    def test_success_upload_transcribe_and_cleanup(self):
        session = Mock()
        session.post.side_effect = [
            FakeResponse(
                headers={"x-goog-upload-url": "https://upload.invalid/test"}
            ),
            FakeResponse(
                data={
                    "file": {
                        "name": "files/test123",
                        "uri": "https://files.invalid/audio123",
                    }
                }
            ),
            FakeResponse(data={"output_text": "Распознанная речь"}),
        ]

        with patch("app.gemini_stt.requests.Session", return_value=session):
            result = transcribe_gemini_audio(self.audio, "fake-test-key")

        self.assertEqual(result, "Распознанная речь")
        self.assertEqual(session.post.call_count, 3)
        self.assertEqual(session.delete.call_count, 1)
        self.assertTrue(session.close.called)

        interaction_call = session.post.call_args_list[2]
        self.assertIn("json", interaction_call.kwargs)
        self.assertEqual(
            interaction_call.kwargs["json"]["model"], "gemini-3.5-transcribe"
        )

    def test_missing_key_fails_before_network(self):
        with patch("app.gemini_stt.requests.Session") as session_factory:
            with self.assertRaisesRegex(
                GeminiSTTError, "GEMINI_STT_KEY_MISSING"
            ):
                transcribe_gemini_audio(self.audio, "")
            session_factory.assert_not_called()

    def test_missing_audio_fails_before_network(self):
        with patch("app.gemini_stt.requests.Session") as session_factory:
            with self.assertRaisesRegex(
                GeminiSTTError, "GEMINI_STT_AUDIO_MISSING"
            ):
                transcribe_gemini_audio(
                    Path(self.temp.name) / "missing.wav", "fake-test-key"
                )
            session_factory.assert_not_called()

    def test_empty_audio_fails_before_network(self):
        empty = Path(self.temp.name) / "empty.wav"
        empty.write_bytes(b"")

        with patch("app.gemini_stt.requests.Session") as session_factory:
            with self.assertRaisesRegex(
                GeminiSTTError, "GEMINI_STT_AUDIO_EMPTY"
            ):
                transcribe_gemini_audio(empty, "fake-test-key")
            session_factory.assert_not_called()

    def test_network_error_is_sanitized_and_session_closed(self):
        session = Mock()
        session.post.side_effect = requests.ConnectionError(
            "sensitive transport details"
        )

        with patch("app.gemini_stt.requests.Session", return_value=session):
            with self.assertRaisesRegex(
                GeminiSTTError, "GEMINI_STT_NETWORK_ERROR"
            ) as caught:
                transcribe_gemini_audio(self.audio, "fake-test-key")

        self.assertNotIn("sensitive transport details", str(caught.exception))
        self.assertNotIn("fake-test-key", str(caught.exception))
        self.assertTrue(session.close.called)

    def test_http_error_is_reported_without_response_body(self):
        session = Mock()
        session.post.return_value = FakeResponse(
            status_code=403,
            data={"error": {"message": "private response details"}},
        )

        with patch("app.gemini_stt.requests.Session", return_value=session):
            with self.assertRaisesRegex(
                GeminiSTTError, "GEMINI_STT_UPLOAD_START_HTTP_403"
            ) as caught:
                transcribe_gemini_audio(self.audio, "fake-test-key")

        self.assertNotIn("private response details", str(caught.exception))
        self.assertTrue(session.close.called)

    def test_missing_upload_url_fails_safely(self):
        session = Mock()
        session.post.return_value = FakeResponse()

        with patch("app.gemini_stt.requests.Session", return_value=session):
            with self.assertRaisesRegex(
                GeminiSTTError, "GEMINI_STT_UPLOAD_URL_MISSING"
            ):
                transcribe_gemini_audio(self.audio, "fake-test-key")

        self.assertTrue(session.close.called)

    def test_empty_transcription_is_rejected_and_upload_cleanup_attempted(self):
        session = Mock()
        session.post.side_effect = [
            FakeResponse(
                headers={"x-goog-upload-url": "https://upload.invalid/test"}
            ),
            FakeResponse(
                data={
                    "file": {
                        "name": "files/test123",
                        "uri": "https://files.invalid/audio123",
                    }
                }
            ),
            FakeResponse(data={"output_text": "   "}),
        ]

        with patch("app.gemini_stt.requests.Session", return_value=session):
            with self.assertRaisesRegex(
                GeminiSTTError, "GEMINI_STT_EMPTY"
            ):
                transcribe_gemini_audio(self.audio, "fake-test-key")

        self.assertEqual(session.delete.call_count, 1)
        self.assertTrue(session.close.called)


if __name__ == "__main__":
    unittest.main(verbosity=2)
