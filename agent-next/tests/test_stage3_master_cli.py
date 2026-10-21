import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENT = PROJECT_ROOT / "agent.py"


def run_cli(root, *args, expect_ok=True):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)

    proc = subprocess.run(
        ["python3", str(root / "agent.py"), *args],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
    )

    if expect_ok and proc.returncode != 0:
        raise AssertionError(
            f"CLI failed: {args}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )

    if not expect_ok and proc.returncode == 0:
        raise AssertionError(f"CLI unexpectedly succeeded: {args}")

    return proc


def parse_json(stdout):
    text = stdout.strip()
    if not text:
        raise AssertionError("CLI produced no JSON output")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"CLI output is not JSON: {stdout}") from exc


def complete_child_steps(root, task_id):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)
    script = """
import sys
from pathlib import Path
from app.state_store import StateStore

task_id = sys.argv[1]
store = StateStore(Path(sys.argv[2]))
steps = store.list_steps(task_id)
assert len(steps) == 8, f"Expected 8 steps for {task_id}, got {len(steps)}"
for step in steps:
    store.complete_step_and_task(
        task_id,
        step["id"],
        result={"test_fixture": "completed"},
    )
assert store.get_task(task_id)["status"] == "COMPLETED"
"""
    proc = subprocess.run(
        [
            "python3", "-c", script, task_id,
            str(root / "state" / "agent_next.db"),
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"Could not complete child {task_id} through StateStore"
            f"\\nstdout={proc.stdout}\\nstderr={proc.stderr}"
        )


def test_stage3_master_cli():
    with tempfile.TemporaryDirectory(prefix="reelforge-stage3-cli-") as tmp:
        root = Path(tmp)

        shutil.copy2(AGENT, root / "agent.py")
        shutil.copytree(PROJECT_ROOT / "app", root / "app")

        (root / "config").mkdir()
        (root / "state").mkdir()
        (root / "config" / "mcp.txt").write_text(
            "MCP_URL=http://127.0.0.1:9/mcp\n"
            "MCP_PROTOCOL=2025-06-18\n"
            "MCP_TOKEN_FILE=/nonexistent/stage3-cli-test-token\n"
            "MCP_ACCEPT=text/event-stream,application/json\n",
            encoding="utf-8",
        )

        master = "STAGE3-AUTO-MASTER-001"
        child_a = "STAGE3-AUTO-CHILD-A"
        child_b = "STAGE3-AUTO-CHILD-B"

        result = parse_json(run_cli(root, "master-create", "--task-id", master).stdout)
        assert result["id"] == master
        assert result["status"] == "RECEIVED"

        result = parse_json(
            run_cli(root, "master-add-stage1", master, child_a).stdout
        )
        assert result["id"] == child_a
        assert result["status"] == "PLANNED"

        result = parse_json(
            run_cli(
                root,
                "master-add-stage1",
                master,
                child_b,
                "--depends-on",
                child_a,
            ).stdout
        )
        assert result["id"] == child_b
        assert result["status"] == "PLANNED"
        assert json.loads(result["metadata"])["dependencies"] == [child_a]

        result = parse_json(run_cli(root, "master-status", master).stdout)
        children = {item["id"]: item for item in result["children"]}
        assert children[child_a]["status"] == "PLANNED"
        assert children[child_b]["status"] == "PLANNED"

        run_cli(root, "master-start-child", master, child_b, expect_ok=False)

        result = parse_json(run_cli(root, "master-status", master).stdout)
        children = {item["id"]: item for item in result["children"]}
        assert children[child_b]["status"] == "PLANNED"

        # Complete A through the normal lifecycle in the isolated test DB.
        run_cli(root, "master-start-child", master, child_a)
        complete_child_steps(root, child_a)

        result = parse_json(
            run_cli(root, "master-start-child", master, child_b).stdout
        )
        assert result["id"] == child_b
        assert result["status"] == "RUNNING"

        result = parse_json(run_cli(root, "master-reconcile", master).stdout)
        assert result["master"]["status"] == "RECEIVED"
        assert result["reason"] == "CHILD_WORKFLOW_INCOMPLETE"

        # Complete B through the normal lifecycle in the isolated test DB.
        complete_child_steps(root, child_b)

        result = parse_json(run_cli(root, "master-reconcile", master).stdout)
        assert result["master"]["status"] == "COMPLETED"
        assert result["reason"] == "ALL_CHILD_WORKFLOWS_COMPLETED"

        print("STAGE3_MASTER_CLI_CONTRACT_OK")


if __name__ == "__main__":
    test_stage3_master_cli()
