import os
import sys
import tempfile
from pathlib import Path


from app.executor import TaskExecutor
from app.state_store import StateStore


class FakeWorker:
    def __init__(self):
        self.calls = []

    def call_tool(self, capability, arguments=None, operation=None, *, validated=False, confirmed=False, task_id=None):
        self.calls.append((capability, dict(arguments or {}), operation, validated, confirmed, task_id))
        return {"ok": True, "capability": capability}


def test_confirmation_resume_survives_new_executor_instance():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "state.db"
        store = StateStore(db)
        first_worker = FakeWorker()
        first = TaskExecutor(state=store, worker=first_worker)
        pending = first.execute(
            "restart-confirm-task", "delete", {"id": 7},
            operation="item", profile="EDIT", validated=False,
        )
        assert pending["state"] == "BLOCKED_ON_USER_ACTION"
        assert pending["reason"] == "CONFIRM_REQUIRED"
        token = pending["confirmation_token"]

        second_worker = FakeWorker()
        second = TaskExecutor(state=StateStore(db), worker=second_worker)
        resumed = second.confirm_and_resume(
            "restart-confirm-task", token, arguments={"id": 7}
        )
        assert resumed["state"] == "COMPLETED"
        assert resumed["allowed"] is True
        assert second_worker.calls[0][4] is True
        assert second_worker.calls[0][1]["confirmed"] is True


if __name__ == "__main__":
    test_confirmation_resume_survives_new_executor_instance()
    print("EXECUTOR_CONFIRMATION_RESTART_OK")
