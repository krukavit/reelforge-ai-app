from pathlib import Path
import uuid

from app.executor import TaskExecutor
from app.state_store import StateStore
import tempfile


class FakeWorker:
    def __init__(self):
        self.calls = []

    def call_tool(self, capability, arguments=None, operation=None, *, validated=False, confirmed=False, task_id=None):
        self.calls.append({
            "capability": capability, "arguments": arguments or {},
            "operation": operation, "validated": validated, "confirmed": confirmed,
            "task_id": task_id,
        })
        if capability == "backup":
            output = Path((arguments or {}).get("output", "test-backup.tar.gz"))
            output.write_text("verified-backup")
            return {"ok": True, "backup_path": str(output)}
        return {"ok": True}


def test_executor_forwards_validated_to_worker():
    with tempfile.TemporaryDirectory() as td:
        ex = TaskExecutor(state=StateStore(Path(td) / "state.db"))
        fake = FakeWorker()
        ex.worker = fake
        task_id = "executor-validated-" + uuid.uuid4().hex[:8]
        # Абсолютный путь - однозначен для FakeWorker (пишет туда,
        # куда сказано) и для _verify_backup_artifact (реальный Core
        # резолвит относительные пути от $HOME, а не от cwd теста).
        output_path = str(Path(td) / "test-backup.tar.gz")
        result = ex.execute(
            task_id, "backup", validated=True,
            arguments={"output": output_path},
        )
        assert result["state"] == "COMPLETED", result
        assert fake.calls and fake.calls[0]["validated"] is True, fake.calls
        assert fake.calls[0].get("task_id") == task_id, fake.calls


def test_executor_does_not_forward_confirm_without_real_confirmation():
    with tempfile.TemporaryDirectory() as td:
        ex = TaskExecutor(state=StateStore(Path(td) / "state.db"))
        fake = FakeWorker()
        ex.worker = fake
        task_id = "executor-confirm-" + uuid.uuid4().hex[:8]
        result = ex.execute(task_id, "backup", validated=True)
        assert result["allowed"] is True
        assert fake.calls[0]["confirmed"] is False


def test_confirmation_token_is_bound_to_exact_arguments():
    with tempfile.TemporaryDirectory() as td:
        ex = TaskExecutor(state=StateStore(Path(td) / "state.db"))
        fake = FakeWorker()
        ex.worker = fake
        task_id = "executor-confirm-bind-" + uuid.uuid4().hex[:8]
        first = ex.execute(task_id, "delete", arguments={"path": "A"})
        assert first["state"] == "BLOCKED_ON_USER_ACTION"
        token = first["confirmation_token"]
        wrong = ex.confirm_and_resume(task_id, token, arguments={"path": "B"})
        assert wrong["allowed"] is False and wrong["reason"] == "NO_PENDING_CONFIRMATION"
        right = ex.confirm_and_resume(task_id, token, arguments={"path": "A"})
        assert right["state"] == "COMPLETED"
        assert fake.calls[-1]["confirmed"] is True


def test_executor_marks_unknown_outcome_without_retrying():
    from app.mcp_client import MCPUnknownOutcomeError

    class UnknownWorker:
        def __init__(self):
            self.calls = 0

        def call_tool(self, *args, **kwargs):
            self.calls += 1
            raise MCPUnknownOutcomeError(
                "MCP request failed with unknown outcome; not retried: tools/call"
            )

    with tempfile.TemporaryDirectory() as td:
        ex = TaskExecutor(state=StateStore(Path(td) / "state.db"))
        fake = UnknownWorker()
        ex.worker = fake
        task_id = "executor-unknown-" + uuid.uuid4().hex[:8]
        result = ex.execute(task_id, "backup", validated=True)
        assert result["state"] == "FAILED"
        assert result["reason"] == "UNKNOWN_OUTCOME"
        assert fake.calls == 1
        task = ex.runner.load_task(task_id)
        assert task["status"] == "FAILED"
        assert task["recovery_reason"] == "UNKNOWN"
