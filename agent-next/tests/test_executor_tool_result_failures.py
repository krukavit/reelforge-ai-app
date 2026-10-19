from pathlib import Path
from tempfile import TemporaryDirectory

from app.executor import TaskExecutor
from app.state_store import StateStore


class FakeWorker:
    def __init__(self, result):
        self.result = result

    def call_tool(self, *args, **kwargs):
        return self.result


def test_ls_tool_failure_result_marks_task_failed():
    with TemporaryDirectory() as td:
        state = StateStore(Path(td) / "state.db")
        worker = FakeWorker({
            "id": 2,
            "jsonrpc": "2.0",
            "result": {
                "content": [{"type": "text", "text": "Cannot access: /missing"}]
            },
        })
        ex = TaskExecutor(state=state, worker=worker)

        result = ex.execute("ls-failure", "ls", arguments={"path": "/missing"})

        assert result["state"] == "FAILED"
        assert result["reason"] == "TOOL_FAILURE"
        assert state.get_task("ls-failure")["status"] == "FAILED"


def test_read_tool_failure_result_marks_task_failed():
    with TemporaryDirectory() as td:
        state = StateStore(Path(td) / "state.db")
        worker = FakeWorker({
            "id": 2,
            "jsonrpc": "2.0",
            "result": {
                "content": [{"type": "text", "text": "Cannot read: /missing.txt"}]
            },
        })
        ex = TaskExecutor(state=state, worker=worker)

        result = ex.execute("read-failure", "read", arguments={"path": "/missing.txt"})

        assert result["state"] == "FAILED"
        assert result["reason"] == "TOOL_FAILURE"
        assert state.get_task("read-failure")["status"] == "FAILED"


def test_normal_ls_result_still_completes():
    with TemporaryDirectory() as td:
        state = StateStore(Path(td) / "state.db")
        worker = FakeWorker({
            "id": 2,
            "jsonrpc": "2.0",
            "result": {
                "content": [{"type": "text", "text": "total 1\nfile.txt"}]
            },
        })
        ex = TaskExecutor(state=state, worker=worker)

        result = ex.execute("ls-success", "ls", arguments={"path": "."})

        assert result["state"] == "COMPLETED"
        assert state.get_task("ls-success")["status"] == "COMPLETED"


def test_tilde_path_is_canonicalized_without_target_root():
    with TemporaryDirectory() as td:
        state = StateStore(Path(td) / "state.db")
        worker = FakeWorker({
            "id": 2,
            "jsonrpc": "2.0",
            "result": {
                "content": [{"type": "text", "text": "ok"}]
            },
        })
        ex = TaskExecutor(state=state, worker=worker)

        args = ex._resolve_target_root_arguments(
            None, "ls", {"path": "~/reelforge-agent-next"}
        )

        assert args["path"] == str(Path.home() / "reelforge-agent-next")


def test_structured_mcp_tool_error_marks_task_failed():
    with TemporaryDirectory() as td:
        state = StateStore(Path(td) / "state.db")
        worker = FakeWorker({
            "id": 2,
            "jsonrpc": "2.0",
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": "tool failed"}],
            },
        })
        ex = TaskExecutor(state=state, worker=worker)

        result = ex.execute("structured-failure", "health")

        assert result["state"] == "FAILED"
        assert result["reason"] == "TOOL_FAILURE"
        assert state.get_task("structured-failure")["status"] == "FAILED"
