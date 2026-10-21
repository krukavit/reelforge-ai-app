from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

WARMUP_IDLE_SECONDS = 30
WARMUP_PAUSE_SECONDS = 3.0
WARMUP_TEXT = "Внимание"
RETRY_BASE_SECONDS = 3.0
RETRY_PER_CHAR_SECONDS = 0.07
RETRY_PAUSE_SECONDS = 3.0
MAX_ATTEMPTS = 2
_STAMP = Path.home() / ".config" / "reelforge-agent-next" / "tts_last_use"
PIPER_SCRIPT = Path.home() / "piper_voices" / "say.py"
PIPER_MODEL = Path.home() / "piper_voices" / "ru_RU-irina-medium.onnx"
PIPER_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class TTSResult:
    ok: bool
    error: str | None = None


def expected_min_seconds(text: str) -> float:
    return RETRY_BASE_SECONDS + RETRY_PER_CHAR_SECONDS * len(text)


def _warmup_needed() -> bool:
    try:
        return time.time() - _STAMP.stat().st_mtime > WARMUP_IDLE_SECONDS
    except OSError:
        return True


def _touch_stamp() -> None:
    try:
        _STAMP.parent.mkdir(parents=True, exist_ok=True)
        _STAMP.touch()
    except OSError:
        pass


def _log_attempt(text: str, attempt: int, elapsed: float, expected: float, lost: bool) -> None:
    try:
        path = _STAMP.parent / "tts_log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": round(time.time(), 1),
            "chars": len(text),
            "attempt": attempt,
            "elapsed": round(elapsed, 2),
            "expected_min": round(expected, 2),
            "lost": lost,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


class TextToSpeech:
    """Speech synthesis through the working Termux:API TTS command.

    ColorOS kills the short-lived Termux:API process and the Google TTS
    engine soon after start. A sacrificial phrase absorbs the loss after
    idle, and a call that returns much faster than the speech could last is
    treated as lost and repeated once.
    """

    def __init__(
        self,
        command: str = "termux-tts-speak",
        language: str = "ru",
        stream: str = "MUSIC",
        warmup: bool = True,
        retry: bool = True,
        region: str = "RU",
        backend: str = "auto",
    ):
        self.command = command
        self.language = language
        self.region = region
        self.backend = backend
        self.stream = stream
        self.warmup = warmup
        self.retry = retry

    def _argv(self, text: str) -> list[str]:
        return [self.command, "-l", self.language, "-n", self.region, "-s", self.stream, text]

    def _wake(self) -> None:
        try:
            subprocess.run(
                self._argv(WARMUP_TEXT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
            )
        except Exception:
            pass
        time.sleep(WARMUP_PAUSE_SECONDS)

    def _piper_available(self) -> bool:
        return (
            self.backend in ("auto", "piper")
            and PIPER_SCRIPT.is_file()
            and PIPER_MODEL.is_file()
        )

    def _speak_piper(self, text: str) -> TTSResult:
        try:
            completed = subprocess.run(
                [sys.executable, str(PIPER_SCRIPT), text],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=PIPER_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            return TTSResult(ok=False, error=f"PIPER_START_FAILED:{exc}")

        if completed.returncode != 0:
            error = (completed.stderr or "").strip()[-200:]
            return TTSResult(
                ok=False,
                error=f"PIPER_FAILED:{error or completed.returncode}",
            )
        return TTSResult(ok=True)

    def speak_result(self, text: str) -> TTSResult:
        text = str(text or "").strip()
        if not text:
            return TTSResult(ok=False, error="TTS_EMPTY")

        if self._piper_available():
            local = self._speak_piper(text)
            if local.ok:
                _touch_stamp()
                return local

        if self.warmup and _warmup_needed():
            self._wake()

        expected = expected_min_seconds(text)
        attempt = 0
        while True:
            attempt += 1
            start = time.monotonic()
            try:
                completed = subprocess.run(
                    self._argv(text),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
            except Exception as exc:
                return TTSResult(
                    ok=False,
                    error=f"TTS_START_FAILED:{exc}",
                )
            elapsed = time.monotonic() - start

            if completed.returncode != 0:
                error = (completed.stderr or "").strip()
                return TTSResult(
                    ok=False,
                    error=f"TTS_FAILED:{error or completed.returncode}",
                )

            lost = self.retry and elapsed < expected
            _log_attempt(text, attempt, elapsed, expected, lost)
            if lost and attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_PAUSE_SECONDS)
                continue
            break

        _touch_stamp()
        return TTSResult(ok=True)

    def speak(self, text: str) -> None:
        result = self.speak_result(text)
        if not result.ok:
            raise RuntimeError(result.error or "Text-to-speech failed")


__all__ = ["TTSResult", "TextToSpeech", "expected_min_seconds"]
