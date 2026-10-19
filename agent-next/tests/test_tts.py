from __future__ import annotations
import subprocess
from app.tts import TTSResult, TextToSpeech


def test_tts_builds_ru_ru_music_command():
    original_run = subprocess.run
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="",
            stderr="",
        )

    subprocess.run = fake_run
    try:
        result = TextToSpeech(warmup=False, retry=False, backend="termux").speak_result("Привет, агент.")
    finally:
        subprocess.run = original_run

    assert result.ok is True
    assert len(calls) == 1
    expected = [
        "termux-tts-speak",
        "-l",
        "ru",
        "-n",
        "RU",
        "-s",
        "MUSIC",
        "Привет, агент.",
    ]
    assert calls[0][0][0] == expected, (ascii(calls[0][0][0]), ascii(expected))


def test_tts_empty_text_does_not_execute():
    original_run = subprocess.run

    def fail_run(*args, **kwargs):
        raise AssertionError("subprocess must not run for empty TTS text")

    subprocess.run = fail_run
    try:
        result = TextToSpeech(warmup=False, retry=False, backend="termux").speak_result("   ")
    finally:
        subprocess.run = original_run

    assert result.ok is False
    assert result.error == "TTS_EMPTY"


def test_tts_nonzero_command_returns_error():
    original_run = subprocess.run

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=7,
            stdout="",
            stderr="TTS unavailable",
        )

    subprocess.run = fake_run
    try:
        result = TextToSpeech(warmup=False, retry=False, backend="termux").speak_result("Проверка")
    finally:
        subprocess.run = original_run

    assert result.ok is False
    assert result.error == "TTS_FAILED:TTS unavailable"


def _speak_with_fake_clock(durations, text):
    import tempfile
    from pathlib import Path

    import app.tts as tts_module

    original_run = subprocess.run
    original_sleep = tts_module.time.sleep
    original_monotonic = tts_module.time.monotonic
    original_stamp = tts_module._STAMP
    calls = []
    clock = [0.0]

    def fake_run(*args, **kwargs):
        index = len(calls)
        calls.append(args[0])
        clock[0] += durations[min(index, len(durations) - 1)]
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="",
            stderr="",
        )

    with tempfile.TemporaryDirectory() as tmp:
        tts_module._STAMP = Path(tmp) / "tts_last_use"
        subprocess.run = fake_run
        tts_module.time.sleep = lambda seconds: None
        tts_module.time.monotonic = lambda: clock[0]
        try:
            result = TextToSpeech(warmup=False, backend="termux").speak_result(text)
        finally:
            subprocess.run = original_run
            tts_module.time.sleep = original_sleep
            tts_module.time.monotonic = original_monotonic
            tts_module._STAMP = original_stamp
    return result, calls


def test_tts_retries_once_when_call_returns_too_fast():
    text = "Это достаточно длинная фраза для проверки повтора."
    result, calls = _speak_with_fake_clock([0.5, 9.0], text)
    assert result.ok is True
    assert len(calls) == 2, calls


def test_tts_does_not_retry_when_call_lasts_long_enough():
    text = "Это достаточно длинная фраза для проверки повтора."
    result, calls = _speak_with_fake_clock([9.0], text)
    assert result.ok is True
    assert len(calls) == 1, calls


def test_tts_retry_is_limited_to_one_repeat():
    text = "Это достаточно длинная фраза для проверки повтора."
    result, calls = _speak_with_fake_clock([0.5, 0.5, 0.5], text)
    assert result.ok is True
    assert len(calls) == 2, calls


def test_tts_sacrificial_warmup_phrase_runs_once_after_idle():
    import tempfile
    from pathlib import Path

    import app.tts as tts_module

    original_run = subprocess.run
    original_sleep = tts_module.time.sleep
    original_stamp = tts_module._STAMP
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="",
            stderr="",
        )

    with tempfile.TemporaryDirectory() as tmp:
        tts_module._STAMP = Path(tmp) / "tts_last_use"
        subprocess.run = fake_run
        tts_module.time.sleep = lambda seconds: None
        try:
            first = TextToSpeech(retry=False, backend="termux").speak_result("раз")
            second = TextToSpeech(retry=False, backend="termux").speak_result("два")
        finally:
            subprocess.run = original_run
            tts_module.time.sleep = original_sleep
            tts_module._STAMP = original_stamp

    assert first.ok is True
    assert second.ok is True
    assert len(calls) == 3
    assert calls[0][-1] == tts_module.WARMUP_TEXT
    assert calls[1][-1] == "раз"
    assert calls[2][-1] == "два"


class _Patched:
    """Manual monkeypatch: set attributes, restore them on exit."""

    def __init__(self, *items):
        self.items = items
        self.saved = []

    def __enter__(self):
        for obj, name, value in self.items:
            self.saved.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)
        return self

    def __exit__(self, *exc):
        for obj, name, value in reversed(self.saved):
            setattr(obj, name, value)
        return False


def test_tts_auto_prefers_piper():
    import app.tts as tts_module

    calls = []

    def fake_piper(self, text):
        calls.append(text)
        return TTSResult(ok=True)

    def forbidden_termux(*args, **kwargs):
        raise AssertionError("Termux fallback must not run after Piper succeeds")

    with _Patched(
        (TextToSpeech, "_piper_available", lambda self: True),
        (TextToSpeech, "_speak_piper", fake_piper),
        (tts_module, "_touch_stamp", lambda: None),
        (subprocess, "run", forbidden_termux),
    ):
        result = TextToSpeech(warmup=False).speak_result("Проверка Piper")

    assert result.ok is True
    assert calls == ["Проверка Piper"]


def test_tts_auto_falls_back_to_termux_when_piper_fails():
    import app.tts as tts_module

    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="", stderr=""
        )

    with _Patched(
        (TextToSpeech, "_piper_available", lambda self: True),
        (
            TextToSpeech,
            "_speak_piper",
            lambda self, text: TTSResult(ok=False, error="PIPER_FAILED:test"),
        ),
        (tts_module, "_touch_stamp", lambda: None),
        (subprocess, "run", fake_run),
    ):
        result = TextToSpeech(warmup=False, retry=False).speak_result("Запасной голос")

    assert result.ok is True
    assert len(calls) == 1
    assert calls[0][0][0][0] == "termux-tts-speak"


def test_tts_auto_uses_termux_when_piper_files_are_missing():
    import tempfile
    from pathlib import Path

    import app.tts as tts_module

    with tempfile.TemporaryDirectory() as tmp:
        with _Patched(
            (tts_module, "PIPER_SCRIPT", Path(tmp) / "missing_say.py"),
            (tts_module, "PIPER_MODEL", Path(tmp) / "missing_model.onnx"),
        ):
            assert TextToSpeech()._piper_available() is False
