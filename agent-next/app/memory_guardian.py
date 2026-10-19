"""Conservative Android RAM guardian for Agent Next.

Default mode is observation-only. No process is stopped unless the caller
explicitly passes allow_stop=True.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

THRESHOLD_KIB = 450 * 1024
MEMINFO_PATH = Path("/proc/meminfo")
MAX_STOPS_PER_CALL = 1

# Exact allowlist: never add system, Termux, or Agent Next packages here.
TARGET_PACKAGES = {
    "com.instagram.android",
    "com.instagram.barcelona",
    "com.instagram.basel",
    "org.telegram.messenger",
}


class GuardianError(RuntimeError):
    pass


def _adb(*args: str, timeout: int = 10) -> str:
    if not shutil.which("adb"):
        raise GuardianError("ADB_NOT_FOUND")

    try:
        result = subprocess.run(
            ["adb", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GuardianError("ADB_COMMAND_FAILED") from exc

    if result.returncode != 0:
        raise GuardianError("ADB_COMMAND_FAILED")

    return result.stdout


def _one_device() -> None:
    lines = _adb("devices").splitlines()
    entries = [
        line.split()
        for line in lines[1:]
        if line.strip()
    ]
    # Fail closed for extra devices, offline devices, or unauthorized devices.
    if len(entries) != 1 or len(entries[0]) < 2 or entries[0][1] != "device":
        raise GuardianError("ADB_DEVICE_STATE_NOT_EXACTLY_ONE_DEVICE")


def _available_kib() -> tuple[int, int]:
    raw = _adb("shell", "cat", "/proc/meminfo")
    values = {}
    for line in raw.splitlines():
        match = re.match(r"^(MemTotal|MemAvailable):\s+(\d+)\s+kB", line)
        if match:
            values[match.group(1)] = int(match.group(2))

    if not values.get("MemTotal") or "MemAvailable" not in values:
        raise GuardianError("MEMINFO_UNREADABLE")

    return values["MemAvailable"], values["MemTotal"]


def _foreground_packages() -> set[str]:
    raw = _adb("shell", "dumpsys", "activity", "activities")
    lines = [
        line for line in raw.splitlines()
        if "topResumedActivity" in line or "mResumedActivity" in line
    ]
    if not lines:
        raise GuardianError("FOREGROUND_UNKNOWN")

    # Protect every package mentioned in resumed-activity records.
    packages = set()
    for line in lines:
        match = re.search(r"\b([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)/", line)
        if match:
            packages.add(match.group(1))

    if not packages:
        raise GuardianError("FOREGROUND_UNPARSEABLE")

    return packages


def _package_rss_kib(package: str) -> int:
    raw = _adb("shell", "dumpsys", "meminfo", package)
    match = re.search(r"^\s*TOTAL PSS:\s*\d+.*?TOTAL RSS:\s*(\d+)", raw, re.M)
    if not match:
        return 0
    return int(match.group(1))


def _local_available_kib() -> tuple[int, int]:
    """Read Android memory directly from Termux; ADB is not required."""
    try:
        raw = MEMINFO_PATH.read_text()
    except OSError as exc:
        raise GuardianError("LOCAL_MEMINFO_UNREADABLE") from exc

    values = {}
    for line in raw.splitlines():
        match = re.match(r"^(MemTotal|MemAvailable):\s+(\d+)\s+kB", line)
        if match:
            values[match.group(1)] = int(match.group(2))

    if not values.get("MemTotal") or "MemAvailable" not in values:
        raise GuardianError("LOCAL_MEMINFO_UNREADABLE")

    return values["MemAvailable"], values["MemTotal"]


def inspect(*, allow_stop: bool = False) -> dict:
    """Inspect memory; only stop one allowlisted app when explicitly enabled."""
    try:
        available, total = _local_available_kib()

        result = {
            "status": "OK",
            "mode": "ENABLED" if allow_stop else "OBSERVE_ONLY",
            "available_mib": round(available / 1024, 1),
            "total_mib": round(total / 1024, 1),
            "threshold_mib": round(THRESHOLD_KIB / 1024, 1),
            "stopped_package": None,
            "reason": "MEMORY_SUFFICIENT",
            "candidates": [],
        }

        if available >= THRESHOLD_KIB:
            return result

        result["reason"] = "LOW_MEMORY"
        if not allow_stop:
            result["reason"] = "LOW_MEMORY_OBSERVATION_ONLY"
            return result

        _one_device()
        foreground = _foreground_packages()
        candidates = []

        for package in sorted(TARGET_PACKAGES):
            # Never target anything visible in resumed-activity records.
            if package in foreground:
                continue
            rss = _package_rss_kib(package)
            if rss > 0:
                candidates.append({"package": package, "rss_mib": round(rss / 1024, 1)})

        candidates.sort(key=lambda item: item["rss_mib"], reverse=True)
        result["candidates"] = candidates

        if not allow_stop:
            result["reason"] = "LOW_MEMORY_OBSERVATION_ONLY"
            return result

        if not candidates:
            result["reason"] = "NO_SAFE_CANDIDATE"
            return result

        # Revalidate immediately before the only permitted stop.
        package = candidates[0]["package"]
        if package not in TARGET_PACKAGES:
            result["reason"] = "PACKAGE_NOT_ALLOWLISTED"
            return result

        _one_device()
        latest_available, _ = _available_kib()
        if latest_available >= THRESHOLD_KIB:
            result["reason"] = "MEMORY_RECOVERED_BEFORE_ACTION"
            result["available_mib_before_action"] = round(
                latest_available / 1024, 1
            )
            return result

        latest_foreground = _foreground_packages()
        if package in latest_foreground:
            result["reason"] = "CANDIDATE_BECAME_FOREGROUND"
            return result

        _adb("shell", "am", "force-stop", package, timeout=12)
        result["stopped_package"] = package
        result["reason"] = "ONE_APP_FORCE_STOPPED"

        after, _ = _available_kib()
        result["available_mib_after"] = round(after / 1024, 1)
        return result

    except GuardianError as exc:
        # Fail closed: a diagnostic problem must never trigger a stop.
        return {
            "status": "SAFE_SKIP",
            "reason": str(exc),
            "stopped_package": None,
        }
    except Exception:
        return {
            "status": "SAFE_SKIP",
            "reason": "UNEXPECTED_DIAGNOSTIC_ERROR",
            "stopped_package": None,
        }
