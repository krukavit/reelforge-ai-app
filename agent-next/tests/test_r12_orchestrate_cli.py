import json
import os
import subprocess
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENT = PROJECT_ROOT / "agent.py"


def run_cli(root, *args):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)

    proc = subprocess.run(
        ["python3", str(root / "agent.py"), *args],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
    )

    if proc.returncode != 0:
        raise AssertionError(
            f"CLI failed: {args}\n"
            f"stdout={proc.stdout}\n"
            f"stderr={proc.stderr}"
        )

    return json.loads(proc.stdout)


def test_r12_orchestrate_registers_stage1_without_execution():
    with tempfile.TemporaryDirectory(prefix="reelforge-r12-orchestrate-") as tmp:
        root = Path(tmp)

        shutil_target = root / "app"
        import shutil
        shutil.copy2(AGENT, root / "agent.py")
        shutil.copytree(PROJECT_ROOT / "app", shutil_target)

        (root / "config").mkdir()
        (root / "state").mkdir()
        (root / "config" / "mcp.txt").write_text(
            "MCP_URL=http://127.0.0.1:9/mcp\n"
            "MCP_PROTOCOL=2025-06-18\n"
            "MCP_TOKEN_FILE=/nonexistent/r12-test-token\n"
            "MCP_ACCEPT=text/event-stream,application/json\n"
            "MCP_SESSION_REQUIRED=true\n"
            "MCP_TIMEOUT=1\n",
            encoding="utf-8",
        )

        target = root / "target"
        target.mkdir()

        result = run_cli(
            root,
            "orchestrate",
            "inspect and prepare ReelForge",
            "--profile",
            "EDIT",
            "--target-root",
            str(target),
        )

        assert result["status"] == "ORCHESTRATION_REGISTERED"
        assert result["execution_started"] is False
        assert result["mcp_calls"] == 0
        assert result["master"]["profile"] == "MASTER"
        assert len(result["children"]) == 8
        assert all(
            child["target_root"] == str(target.resolve())
            for child in result["children"]
        )


if __name__ == "__main__":
    test_r12_orchestrate_registers_stage1_without_execution()
    print("R12_ORCHESTRATE_CLI_CONTRACT_OK")
