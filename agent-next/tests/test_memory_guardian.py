from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app import memory_guardian as guardian


def test_memory_inspection_works_without_adb_when_memory_is_sufficient():
    with TemporaryDirectory() as tmp:
        meminfo = Path(tmp) / "meminfo"
        meminfo.write_text(
            "MemTotal: 3495680 kB\nMemAvailable: 700000 kB\n"
        )
        with patch.object(guardian, "MEMINFO_PATH", meminfo), \
             patch.object(guardian, "_one_device", side_effect=AssertionError("ADB must not be called")):
            result = guardian.inspect()

    assert result["status"] == "OK"
    assert result["mode"] == "OBSERVE_ONLY"
    assert result["reason"] == "MEMORY_SUFFICIENT"
    assert result["stopped_package"] is None


def test_low_memory_without_adb_is_observation_only():
    with TemporaryDirectory() as tmp:
        meminfo = Path(tmp) / "meminfo"
        meminfo.write_text(
            "MemTotal: 3495680 kB\nMemAvailable: 300000 kB\n"
        )
        with patch.object(guardian, "MEMINFO_PATH", meminfo), \
             patch.object(guardian, "_one_device", side_effect=AssertionError("ADB must not be called")):
            result = guardian.inspect(allow_stop=False)

    assert result["status"] == "OK"
    assert result["reason"] == "LOW_MEMORY_OBSERVATION_ONLY"
    assert result["stopped_package"] is None


def test_unreadable_local_meminfo_fails_closed():
    with patch.object(
        guardian, "_local_available_kib",
        side_effect=guardian.GuardianError("LOCAL_MEMINFO_UNREADABLE"),
    ):
        result = guardian.inspect()

    assert result["status"] == "SAFE_SKIP"
    assert result["reason"] == "LOCAL_MEMINFO_UNREADABLE"
    assert result["stopped_package"] is None
