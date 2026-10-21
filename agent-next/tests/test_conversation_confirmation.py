from pathlib import Path
from tempfile import TemporaryDirectory

from app.conversation_service import ConversationService


class FakeExecutor:
    def __init__(self):
        self.calls = []

    def confirm_and_resume(self, task_id, confirmation_token, arguments=None):
        self.calls.append((task_id, confirmation_token, arguments))
        return {
            "task_id": task_id,
            "state": "COMPLETED",
            "allowed": True,
        }


def test_conversation_service_confirmation_delegates_without_forging_confirmation():
    executor = FakeExecutor()
    service = ConversationService(executor=executor)

    result = service.confirm("chat-write-abc123", "one-time-token")

    assert result["state"] == "COMPLETED"
    assert executor.calls == [
        ("chat-write-abc123", "one-time-token", None)
    ]


def test_conversation_service_confirmation_passes_arguments_only_when_supplied():
    executor = FakeExecutor()
    service = ConversationService(executor=executor)

    service.confirm(
        "chat-write-def456",
        "another-token",
        arguments={"path": "test.txt", "content": "hello"},
    )

    assert executor.calls == [
        (
            "chat-write-def456",
            "another-token",
            {"path": "test.txt", "content": "hello"},
        )
    ]


def test_conversation_service_confirmation_has_no_confirmed_argument():
    executor = FakeExecutor()
    service = ConversationService(executor=executor)

    service.confirm("chat-delete-ghi789", "token")

    assert executor.calls[0][2] is None

def test_conversation_service_confirmation_returns_executor_result_unchanged():
    class ResultExecutor:
        def confirm_and_resume(self, task_id, confirmation_token, arguments=None):
            return {
                "task_id": task_id,
                "state": "BLOCKED_ON_USER_ACTION",
                "allowed": False,
                "reason": "EXPIRED",
            }

    service = ConversationService(executor=ResultExecutor())

    result = service.confirm("chat-write-expired", "expired-token")

    assert result == {
        "task_id": "chat-write-expired",
        "state": "BLOCKED_ON_USER_ACTION",
        "allowed": False,
        "reason": "EXPIRED",
    }



def test_conversation_service_handle_defaults_target_root_to_agent_project():
    class CaptureExecutor:
        def __init__(self):
            self.calls = []

        def execute(
            self,
            task_id,
            capability,
            arguments=None,
            operation=None,
            profile="READ_ONLY",
            validated=False,
            target_root=None,
        ):
            self.calls.append({
                "task_id": task_id,
                "capability": capability,
                "arguments": arguments,
                "target_root": target_root,
            })
            return {"task_id": task_id, "state": "COMPLETED", "allowed": True}

    executor = CaptureExecutor()
    service = ConversationService(executor=executor)

    result = service.handle("Прочитай файл app/policy.py")

    assert result["result"]["state"] == "COMPLETED"
    assert executor.calls[0]["capability"] == "read"
    assert executor.calls[0]["arguments"] == {"path": "app/policy.py"}
    assert executor.calls[0]["target_root"] == str(
        Path(__file__).resolve().parent.parent
    )


def test_conversation_service_handle_preserves_explicit_target_root():
    class CaptureExecutor:
        def __init__(self):
            self.target_root = None

        def execute(
            self,
            task_id,
            capability,
            arguments=None,
            operation=None,
            profile="READ_ONLY",
            validated=False,
            target_root=None,
        ):
            self.target_root = target_root
            return {"task_id": task_id, "state": "COMPLETED", "allowed": True}

    executor = CaptureExecutor()
    service = ConversationService(executor=executor)

    with TemporaryDirectory() as td:
        service.handle(
            "Прочитай файл app/policy.py",
            target_root=td,
        )

        assert executor.target_root == td

def test_r5_conversation_confirmation_round_trip_is_one_time_and_preserves_arguments():
    from app.executor import TaskExecutor
    from app.state_store import StateStore

    class FakeWorker:
        def __init__(self):
            self.calls = []

        def call_tool(self, capability, arguments, operation=None,
                      validated=False, confirmed=False, task_id=None):
            self.calls.append({
                "capability": capability,
                "arguments": dict(arguments or {}),
                "operation": operation,
                "validated": validated,
                "confirmed": confirmed,
                "task_id": task_id,
            })
            return {"ok": True}

    with TemporaryDirectory() as td:
        target_root = Path(td) / "target"
        target_root.mkdir()
        (target_root / "test.txt").write_text("test")

        executor = TaskExecutor(state=StateStore(Path(td) / "state.db"))
        worker = FakeWorker()
        executor.worker = worker
        service = ConversationService(executor=executor)

        pending = service.handle(
            "Удали файл test.txt",
            target_root=str(target_root),
        )

        assert pending["capability"] == "delete"
        assert pending["result"]["state"] == "BLOCKED_ON_USER_ACTION"
        assert pending["result"]["reason"] == "CONFIRM_REQUIRED"

        task_id = pending["task_id"]
        token = pending["result"]["confirmation_token"]

        resumed = service.confirm(task_id, token)

        assert resumed["state"] == "COMPLETED"
        assert resumed["allowed"] is True
        assert len(worker.calls) == 1
        assert worker.calls[0]["capability"] == "delete"
        assert worker.calls[0]["arguments"]["path"] == str(target_root / "test.txt")
        assert worker.calls[0]["arguments"]["confirmed"] is True
        assert worker.calls[0]["confirmed"] is True
        assert worker.calls[0]["validated"] is False

        replay = service.confirm(task_id, token)

        assert replay["allowed"] is False
        assert replay["reason"] in {
            "NOT_AWAITING_CONFIRMATION",
            "NO_PENDING_CONFIRMATION",
        }
        assert len(worker.calls) == 1
