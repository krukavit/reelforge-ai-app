"""Bounded local verification runner for an explicitly selected target root.

This module does not edit files and does not invoke MCP.
Only fixed compile/test commands are exposed.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from app.path_resolver import TargetRootResolver


MAX_TIMEOUT_SECONDS = 60
MAX_CAPTURE_BYTES = 256 * 1024

# Deliberately explicit: do not accept test paths supplied by an LLM.
ALLOWED_TESTS = {
    "path_resolver": "tests/test_path_resolver.py",
    "stage4_orchestrator": "tests/test_stage4_orchestrator.py",
    "executor_tool_result_failures": (
        "tests/test_executor_tool_result_failures.py"
    ),
}

def redact_output(value: str) -> str:
    """Best-effort redaction of common credential formats."""
    if not isinstance(value, str):
        return ""
    patterns = (
        (
            r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|password|secret|credential|private[_-]?key)(\s*[:=]\s*)([^\s,;]+)",
            r"\1\2[REDACTED]",
        ),
        (r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]"),
        (r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED_KEY]"),
    )
    for pattern, replacement in patterns:
        value = re.sub(pattern, replacement, value)
    return value


SECRET_ENV_MARKERS = (
    "API_KEY",
    "ACCESS_TOKEN",
    "AUTH_TOKEN",
    "PASSWORD",
    "SECRET",
    "CREDENTIAL",
    "PRIVATE_KEY",
)


@dataclass(frozen=True)
class LocalTestResult:
    mode: str
    target_root: str
    command: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def passed(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def as_dict(self) -> dict:
        # Never persist process output: it may contain credentials or secrets.
        # Keep the result schema stable while retaining minimal diagnostic metadata.
        return {
            "mode": self.mode,
            "target_root": self.target_root,
            "command": list(self.command),
            "returncode": self.returncode,
            "stdout": "",
            "stderr": "",
            "stdout_bytes": len(self.stdout.encode("utf-8", errors="replace")),
            "stderr_bytes": len(self.stderr.encode("utf-8", errors="replace")),
            "output_omitted": True,
            "timed_out": self.timed_out,
            "passed": self.passed,
        }


def _read_bounded(pipe, destination: bytearray, limit: int) -> None:
    """Drain a pipe to avoid deadlock while retaining at most limit bytes."""
    try:
        while True:
            chunk = pipe.read(8192)
            if not chunk:
                break
            remaining = limit - len(destination)
            if remaining > 0:
                destination.extend(chunk[:remaining])
    finally:
        pipe.close()


class LocalTestRunner:
    """Run only fixed local verification commands inside a target root."""

    def __init__(
        self,
        target_root,
        *,
        timeout_seconds: int = 30,
        output_limit: int = MAX_CAPTURE_BYTES,
    ):
        if type(timeout_seconds) is not int or not (
            1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds must be between 1 and 60")

        if type(output_limit) is not int or not (
            1 <= output_limit <= MAX_CAPTURE_BYTES
        ):
            raise ValueError("output_limit is outside the permitted range")

        self.resolver = TargetRootResolver(target_root)
        self.root = self.resolver.root
        self.timeout_seconds = timeout_seconds
        self.output_limit = output_limit

    def run_compile(self) -> LocalTestResult:
        """Parse Python sources without executing them or writing bytecode."""
        bootstrap = (
            "import ast, pathlib, sys\n"
            "root = pathlib.Path(sys.argv[1])\n"
            "files = [root / 'agent.py'] + "
            "sorted((root / 'app').rglob('*.py')) + "
            "sorted((root / 'tests').rglob('*.py'))\n"
            "files = [p for p in files if p.is_file() "
            "and '__pycache__' not in p.parts]\n"
            "errors = 0\n"
            "for path in files:\n"
            "    try:\n"
            "        ast.parse(path.read_bytes(), filename=str(path))\n"
            "    except (SyntaxError, UnicodeError, OSError) as exc:\n"
            "        errors += 1\n"
            "        print('SYNTAX_ERROR', path.relative_to(root), "
            "getattr(exc, 'lineno', 'unknown'))\n"
            "print('PARSED', len(files), 'FILES')\n"
            "sys.exit(1 if errors else 0)\n"
        )
        argv = (sys.executable, "-c", bootstrap, str(self.root))
        return self._run("compile", argv)

    def run_test(self, name: str) -> LocalTestResult:
        """Run one named, pre-approved function-test module."""
        if not isinstance(name, str) or name not in ALLOWED_TESTS:
            raise ValueError("test name is not allowlisted")

        relative_path = ALLOWED_TESTS[name]
        test_path = self.resolver.resolve(relative_path, must_exist=True)

        if not test_path.is_file() or test_path.suffix != ".py":
            raise ValueError("allowlisted test must be a Python file")

        # The bootstrap is fixed code. The only variable argument is a path
        # selected from ALLOWED_TESTS and verified by TargetRootResolver.
        bootstrap = (
            "import importlib.util, pathlib, sys, traceback; "
            "p=pathlib.Path(sys.argv[1]); "
            "spec=importlib.util.spec_from_file_location('_local_test_module', p); "
            "m=importlib.util.module_from_spec(spec); "
            "spec.loader.exec_module(m); "
            "tests=[(n, getattr(m,n)) for n in dir(m) "
            "if n.startswith('test_') and callable(getattr(m,n))]; "
            "tests or sys.exit('No test functions discovered'); "
            "failed=0; "
            "exec('for name, fn in tests:\\n"
            " try:\\n"
            "  fn()\\n"
            "  print(\\\"PASS \\\"+name)\\n"
            " except Exception:\\n"
            "  failed+=1\\n"
            "  print(\\\"FAIL \\\"+name)\\n"
            "  traceback.print_exc()'); "
            "sys.exit(1 if failed else 0)"
        )

        argv = (
            sys.executable,
            "-c",
            bootstrap,
            str(test_path),
        )
        return self._run("test:" + name, argv)

    def _run(self, mode: str, argv: tuple[str, ...]) -> LocalTestResult:
        env = {
            key: value
            for key, value in os.environ.items()
            if not any(marker in key.upper() for marker in SECRET_ENV_MARKERS)
        }
        env["PYTHONPATH"] = str(self.root)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONNOUSERSITE"] = "1"

        proc = subprocess.Popen(
            list(argv),
            cwd=str(self.root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )

        stdout_data = bytearray()
        stderr_data = bytearray()

        out_thread = threading.Thread(
            target=_read_bounded,
            args=(proc.stdout, stdout_data, self.output_limit),
            daemon=True,
        )
        err_thread = threading.Thread(
            target=_read_bounded,
            args=(proc.stderr, stderr_data, self.output_limit),
            daemon=True,
        )
        out_thread.start()
        err_thread.start()

        timed_out = False
        try:
            proc.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                proc.kill()
            proc.wait()

        out_thread.join()
        err_thread.join()

        def decode(data: bytearray) -> str:
            result = bytes(data).decode("utf-8", errors="replace")
            if len(data) >= self.output_limit:
                result += "\n[output capture limit reached]"
            return redact_output(result)

        return LocalTestResult(
            mode=mode,
            target_root=str(self.root),
            command=tuple(argv),
            returncode=proc.returncode,
            stdout=decode(stdout_data),
            stderr=decode(stderr_data),
            timed_out=timed_out,
        )
