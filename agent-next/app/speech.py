from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from .voice import VoiceCapture


@dataclass(frozen=True)
class SpeechResult:
    text: str
    ok: bool
    error: str | None = None


class SpeechToText:
    """Russian STT using Groq Whisper with Termux STT fallback."""

    DEFAULT_COMMAND = "termux-speech-to-text"
    GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
    DEFAULT_MODEL = "whisper-large-v3-turbo"

    def __init__(
        self,
        command: str = DEFAULT_COMMAND,
        provider: str | None = None,
        api_key: str | None = None,
        voice_capture=None,
        timeout: float = 30.0,
        model: str | None = None,
    ):
        self.command = command
        self.provider = (
            provider or os.environ.get("REELFORGE_STT_PROVIDER", "auto")
        ).strip().lower()
        self.api_key = api_key
        self.voice_capture = voice_capture or VoiceCapture()
        self.timeout = timeout
        self.model = model or os.environ.get(
            "REELFORGE_STT_MODEL", self.DEFAULT_MODEL
        )

    def _get_api_key(self) -> str:
        if self.api_key:
            return self.api_key.strip()

        key = os.environ.get("GROQ_API_KEY", "").strip()
        if key:
            return key

        key_file = Path(
            os.environ.get(
                "REELFORGE_GROQ_KEY_FILE",
                str(Path.home() / ".config/reelforge-agent-next/groq_api_key"),
            )
        )
        try:
            return key_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _get_gemini_api_key(self) -> str:
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if key:
            return key

        key_file = Path(
            os.environ.get(
                "REELFORGE_GEMINI_KEY_FILE",
                str(Path.home() / ".config/reelforge-agent-next/gemini_api_key"),
            )
        )
        try:
            return key_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _transcribe_groq(self, api_key: str) -> SpeechResult:
        seconds = int(os.environ.get("REELFORGE_STT_SECONDS", "5"))
        if not 1 <= seconds <= 30:
            return SpeechResult("", False, "STT_INVALID_DURATION")

        audio_path = None
        try:
            audio_path = self.voice_capture.record(seconds=seconds)
            with Path(audio_path).open("rb") as audio:
                response = requests.post(
                    self.GROQ_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    data={
                        "model": self.model,
                        "language": "ru",
                        "response_format": "json",
                    },
                    files={
                        "file": (
                            Path(audio_path).name,
                            audio,
                            "audio/mp4",
                        )
                    },
                    timeout=self.timeout,
                )

            if response.status_code != 200:
                return SpeechResult(
                    "",
                    False,
                    f"GROQ_STT_HTTP_{response.status_code}",
                )

            try:
                text = response.json().get("text", "").strip()
            except (AttributeError, ValueError):
                return SpeechResult("", False, "GROQ_STT_INVALID_RESPONSE")

            if not text:
                return SpeechResult("", False, "GROQ_STT_EMPTY")

            return SpeechResult(text=text, ok=True)

        except Exception:
            return SpeechResult("", False, "GROQ_STT_REQUEST_FAILED")

        finally:
            if audio_path is not None:
                try:
                    Path(audio_path).unlink(missing_ok=True)
                except OSError:
                    pass

    def transcribe_audio_file(self, audio_path: str | Path) -> SpeechResult:
        """Transcribe an existing file; auto mode tries Gemini, then Groq."""
        if self.provider == "termux":
            return SpeechResult("", False, "STT_FILE_UNSUPPORTED_BY_TERMUX")

        if self.provider not in {"auto", "gemini", "groq"}:
            return SpeechResult("", False, "STT_PROVIDER_INVALID")

        path = Path(audio_path)
        if not path.is_file():
            return SpeechResult("", False, "STT_AUDIO_MISSING")
        try:
            if path.stat().st_size == 0:
                return SpeechResult("", False, "STT_AUDIO_EMPTY")
        except OSError:
            return SpeechResult("", False, "STT_AUDIO_UNREADABLE")

        errors = []

        if self.provider in {"auto", "gemini"}:
            gemini_key = self._get_gemini_api_key()
            if gemini_key:
                try:
                    from .gemini_stt import GeminiSTTError, transcribe_gemini_audio
                    text = transcribe_gemini_audio(
                        path, gemini_key, timeout=max(self.timeout, 60.0)
                    )
                    if text and text.strip():
                        return SpeechResult(text.strip(), True)
                    errors.append("GEMINI_STT_EMPTY")
                except GeminiSTTError as exc:
                    errors.append(str(exc))
                except Exception:
                    errors.append("GEMINI_STT_FAILED")
            else:
                errors.append("GEMINI_STT_KEY_MISSING")

            if self.provider == "gemini":
                return SpeechResult("", False, ";".join(errors))

        groq_key = self._get_api_key()
        if not groq_key:
            errors.append("GROQ_STT_KEY_MISSING")
            return SpeechResult("", False, ";".join(errors))

        content_type = {
            ".ogg": "audio/ogg",
            ".opus": "audio/ogg",
            ".m4a": "audio/mp4",
            ".mp4": "audio/mp4",
            ".wav": "audio/wav",
            ".mp3": "audio/mpeg",
        }.get(path.suffix.lower(), "application/octet-stream")

        try:
            with path.open("rb") as audio:
                response = requests.post(
                    self.GROQ_URL,
                    headers={"Authorization": f"Bearer {groq_key}"},
                    data={
                        "model": self.model,
                        "language": "ru",
                        "response_format": "json",
                    },
                    files={"file": (path.name, audio, content_type)},
                    timeout=self.timeout,
                )

            if response.status_code != 200:
                errors.append(f"GROQ_STT_HTTP_{response.status_code}")
            else:
                try:
                    text = response.json().get("text", "").strip()
                except (AttributeError, ValueError):
                    text = ""
                    errors.append("GROQ_STT_INVALID_RESPONSE")

                if text:
                    return SpeechResult(text, True)
                if not errors or errors[-1] != "GROQ_STT_INVALID_RESPONSE":
                    errors.append("GROQ_STT_EMPTY")
        except Exception:
            errors.append("GROQ_STT_REQUEST_FAILED")

        return SpeechResult("", False, ";".join(errors) or "STT_FAILED")

    def _transcribe_termux(self) -> SpeechResult:
        if self.command == self.DEFAULT_COMMAND:
            command = (
                "/data/data/com.termux/files/usr/bin/"
                "termux-speech-to-text -p -l ru-RU"
            )
        else:
            command = self.command

        last_error = "STT_EMPTY"

        for attempt in range(3):
            try:
                completed = subprocess.run(
                    command,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=15,
                )
                lines = [
                    line.strip()
                    for line in completed.stdout.splitlines()
                    if line.strip()
                ]

                if completed.returncode == 0 and lines:
                    return SpeechResult(lines[-1], True)

                if completed.returncode != 0:
                    last_error = f"STT_COMMAND_FAILED:{completed.returncode}"
                else:
                    last_error = "STT_EMPTY"

            except subprocess.TimeoutExpired:
                last_error = "STT_TIMEOUT"
            except Exception:
                last_error = "STT_READ_FAILED"

            if attempt < 2:
                time.sleep(1)

        return SpeechResult("", False, last_error)

    def transcribe_result(self) -> SpeechResult:
        # Preserve explicitly supplied command behavior for existing callers.
        if self.command != self.DEFAULT_COMMAND:
            return self._transcribe_termux()

        if self.provider not in {"auto", "groq", "termux"}:
            return SpeechResult("", False, "STT_PROVIDER_INVALID")

        if self.provider == "termux":
            return self._transcribe_termux()

        api_key = self._get_api_key()
        cloud_error = "GROQ_STT_KEY_MISSING"

        if api_key:
            cloud_result = self._transcribe_groq(api_key)
            if cloud_result.ok:
                return cloud_result
            cloud_error = cloud_result.error or "GROQ_STT_FAILED"

        local_result = self._transcribe_termux()
        if local_result.ok:
            return local_result

        return SpeechResult(
            "",
            False,
            f"{cloud_error};{local_result.error or 'STT_EMPTY'}",
        )

    def transcribe(self) -> str:
        result = self.transcribe_result()
        if not result.ok:
            raise RuntimeError(result.error or "Speech recognition failed")
        return result.text


__all__ = ["SpeechResult", "SpeechToText"]
