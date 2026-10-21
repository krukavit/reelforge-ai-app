from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


class VoiceCapture:
    """Capture microphone audio through the existing Termux:API command."""

    def __init__(
        self,
        directory: str | Path | None = None,
        sample_rate: int = 16000,
        channels: int = 1,
        encoder: str = "aac",
    ):
        self.directory = Path(directory) if directory else Path(tempfile.gettempdir())
        self.sample_rate = sample_rate
        self.channels = channels
        self.encoder = encoder

    def record(self, seconds: int = 5) -> Path:
        if seconds <= 0:
            raise ValueError("seconds must be positive")

        self.directory.mkdir(parents=True, exist_ok=True)

        output = self.directory / f"reelforge-voice-{__import__('uuid').uuid4().hex[:12]}.m4a"

        try:
            subprocess.run(
                [
                    "termux-microphone-record",
                    "-f",
                    str(output),
                    "-e",
                    self.encoder,
                    "-r",
                    str(self.sample_rate),
                    "-c",
                    str(self.channels),
                    "-l",
                    str(seconds),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )

            import time
            time.sleep(seconds + 0.5)

            subprocess.run(
                ["termux-microphone-record", "-q"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )

            if not output.exists() or output.stat().st_size == 0:
                raise RuntimeError("Microphone recording produced no audio")

            return output

        except Exception:
            output.unlink(missing_ok=True)
            raise


__all__ = ["VoiceCapture"]
