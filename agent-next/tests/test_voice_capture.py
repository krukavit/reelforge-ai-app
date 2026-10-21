from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile

from app.voice import VoiceCapture


def test_voice_capture_rejects_non_positive_duration():
    with tempfile.TemporaryDirectory() as tmp:
        capture = VoiceCapture(directory=tmp)

        try:
            capture.record(seconds=0)
        except ValueError as exc:
            assert str(exc) == "seconds must be positive"
        else:
            raise AssertionError("seconds=0 was not rejected")


def test_voice_capture_cleans_output_after_recording_failure():
    with tempfile.TemporaryDirectory() as tmp:
        original_run = subprocess.run

        def fake_run(*args, **kwargs):
            command = args[0]
            if command[0] == "termux-microphone-record" and "-q" not in command:
                output = Path(command[command.index("-f") + 1])
                output.write_bytes(b"partial-audio")
                raise subprocess.CalledProcessError(1, command)
            raise AssertionError("unexpected command")

        subprocess.run = fake_run
        try:
            capture = VoiceCapture(directory=tmp)

            try:
                capture.record(seconds=1)
            except subprocess.CalledProcessError:
                pass
            else:
                raise AssertionError("recording failure was not propagated")

            assert list(Path(tmp).iterdir()) == []
        finally:
            subprocess.run = original_run
