from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .speech import SpeechToText
from .voice_service import VoiceService


class VoiceLoop:
    """Continuous microphone capture with silence-based Groq transcription."""

    def __init__(
        self,
        pause_seconds: float = 0.75,
        poll_interval: float = 0.45,
        silence_threshold: str = "-35dB",
        capture_dir: str | Path | None = None,
        speech=None,
        service=None,
    ):
        self.pause_seconds = max(0.5, float(pause_seconds))
        self.poll_interval = max(0.25, float(poll_interval))
        self.silence_threshold = silence_threshold
        self.capture_dir = (
            Path(capture_dir)
            if capture_dir
            else Path(tempfile.gettempdir())
        )
        self.speech = speech or SpeechToText(provider="auto")
        self.service = service or VoiceService(speech=self.speech)
        self._recording = False

    @staticmethod
    def _run(command, timeout=5):
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )

    def _start_recording(self, path: Path) -> None:
        result = self._run(
            [
                "termux-microphone-record",
                "-f", str(path),
                "-e", "opus",
                "-r", "16000",
                "-c", "1",
                "-l", "0",
            ],
            timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError("MICROPHONE_START_FAILED")
        self._recording = True

    def _stop_recording(self) -> None:
        if not self._recording:
            return
        try:
            result = self._run(
                ["termux-microphone-record", "-q"],
                timeout=10,
            )
            if result.returncode != 0:
                print("VOICE_LOOP_WARNING=MICROPHONE_STOP_FAILED")
        finally:
            self._recording = False

    @staticmethod
    def _duration(path: Path) -> float | None:
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
                check=False,
            )
            if result.returncode == 0:
                return float(result.stdout.strip())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        return None

    def _silence_events(self, path: Path) -> tuple[list[float], list[float]]:
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-nostats",
                    "-i", str(path),
                    "-af",
                    f"silencedetect=noise={self.silence_threshold}:d=0.20",
                    "-f", "null", "-",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return [], []

        starts = [
            float(value)
            for value in re.findall(
                r"silence_start:\s*([0-9]+(?:\.[0-9]+)?)",
                result.stderr,
            )
        ]
        ends = [
            float(value)
            for value in re.findall(
                r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)",
                result.stderr,
            )
        ]
        return starts, ends

    def _wait_for_phrase(self, path: Path) -> bool:
        """Wait for speech followed by silence, analyzing closed snapshots."""
        snapshot = path.with_name(path.stem + "-snapshot.ogg")

        try:
            while True:
                time.sleep(self.poll_interval)

                if not path.is_file() or path.stat().st_size < 1500:
                    continue

                try:
                    shutil.copyfile(path, snapshot)
                except OSError:
                    continue

                duration = self._duration(snapshot)
                if duration is None or duration < 0.4:
                    continue

                starts, ends = self._silence_events(snapshot)
                if not starts:
                    continue

                last_start = starts[-1]

                # Ignore leading silence before the first utterance.
                if last_start < 0.25:
                    continue

                # Only count silence after the last detected speech.
                # An end event at the snapshot boundary is not evidence
                # that the microphone heard speech again.
                previous_ends = [value for value in ends if value < last_start]
                previous_end = previous_ends[-1] if previous_ends else 0.0
                if last_start - previous_end < 0.25:
                    continue

                if duration - last_start >= self.pause_seconds:
                    return True
        finally:
            snapshot.unlink(missing_ok=True)

    def _prepare_providers(self) -> tuple[str, str, str]:
        """Validate local provider configuration before microphone capture."""
        stt_provider = self.speech.validate_configuration()

        conversation = getattr(self.service, "conversation", None)
        llm = getattr(conversation, "llm", None)
        if llm is None:
            raise RuntimeError("LLM_PROVIDER_PRECHECK_UNAVAILABLE")

        configured = getattr(llm, "providers", None)
        candidates = list(configured) if configured else [llm]
        failures = []
        selected = None

        for candidate in candidates:
            validate = getattr(candidate, "validate_configuration", None)
            if not callable(validate):
                failures.append(
                    RuntimeError("LLM_PROVIDER_PRECHECK_UNAVAILABLE")
                )
                continue
            try:
                validate()
            except Exception as exc:
                failures.append(exc)
                continue
            selected = candidate
            break

        if selected is None:
            if failures:
                raise failures[0]
            raise RuntimeError("LLM_PROVIDER_PRECHECK_UNAVAILABLE")

        return (
            stt_provider,
            str(getattr(selected, "provider", "unknown")),
            str(getattr(selected, "model", "unknown")),
        )

    def run(self, target_root=None) -> None:
        print("VOICE_LOOP=STARTING", flush=True)
        try:
            stt_provider, llm_provider, llm_model = self._prepare_providers()
        except Exception as exc:
            reason = str(exc).replace("\\n", " ").replace("\\r", " ")
            print(f"VOICE_LOOP=PROVIDER_NOT_READY reason={reason}", flush=True)
            raise SystemExit(2) from None

        print(
            f"PROVIDERS_READY stt={stt_provider} "
            f"llm={llm_provider} model={llm_model}",
            flush=True,
        )
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        print(
            f"VOICE_LOOP=STARTED pause_seconds={self.pause_seconds}; "
            "exit=Ctrl+C",
            flush=True,
        )

        try:
            while True:
                path = self.capture_dir / (
                    f"reelforge-loop-{uuid.uuid4().hex[:12]}.ogg"
                )

                try:
                    print("VOICE_LOOP=LISTENING", flush=True)
                    self._start_recording(path)
                    self._wait_for_phrase(path)
                    self._stop_recording()

                    if not path.is_file() or path.stat().st_size == 0:
                        print("VOICE_LOOP=EMPTY_AUDIO", flush=True)
                        continue

                    print("VOICE_LOOP=TRANSCRIBING", flush=True)
                    result = self.speech.transcribe_audio_file(path)

                    if not result.ok or not result.text.strip():
                        print(
                            "VOICE_LOOP=STT_FAILED "
                            f"error={result.error or 'STT_EMPTY'}",
                            flush=True,
                        )
                        continue

                    print(f"VOICE_LOOP=HEARD {result.text}", flush=True)
                    response = self.service.handle_text(
                        result.text,
                        target_root=target_root,
                    )

                    if response.get("tts", {}).get("state") != "COMPLETED":
                        print("VOICE_LOOP=TTS_WARNING", flush=True)

                    if (
                        response.get("conversation", {})
                        .get("result", {})
                        .get("state") == "FAILED"
                    ):
                        print("VOICE_LOOP=CONVERSATION_FAILED", flush=True)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    # Avoid dumping request details or credentials.
                    print(
                        "VOICE_LOOP=ERROR "
                        f"type={type(exc).__name__}",
                        flush=True,
                    )
                    time.sleep(1)
                finally:
                    self._stop_recording()
                    path.unlink(missing_ok=True)
                    path.with_name(path.stem + "-snapshot.ogg").unlink(
                        missing_ok=True
                    )

        except KeyboardInterrupt:
            print("\nVOICE_LOOP=STOPPED", flush=True)
        finally:
            self._stop_recording()


__all__ = ["VoiceLoop"]
