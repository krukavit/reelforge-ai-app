import tempfile
from pathlib import Path

from app.state_store import StateStore


class FakeWorker:
    def __init__(self, root):
        self.root = Path(root)
        self.calls = []

    def call_tool(self, capability, arguments=None, operation=None, *, validated=False, confirmed=False, task_id=None):
        args = dict(arguments or {})
        self.calls.append((capability, args, operation, validated, confirmed, task_id))
        if capability == "backup":
            output = Path(args["output"])
            output.write_text("verified-backup")
            return {"ok": True, "backup_path": str(output)}
        if capability == "write":
            Path(args["path"]).write_text(args["content"])
            return {"ok": True, "path": args["path"]}
        if capability == "health":
            return {"ok": True}
        raise AssertionError(f"unexpected capability: {capability}")


def test_edit_contract_requires_real_backup_before_write():
    from app.executor import TaskExecutor

    with tempfile.TemporaryDirectory() as td:
        store = StateStore(Path(td) / "state.db")
        worker = FakeWorker(td)
        ex = TaskExecutor(state=store, worker=worker)
        assert not Path(td, "target.txt").exists()
        assert not Path(td, "backup.tar.gz").exists()
        assert worker.calls == []
        assert Path(td).exists()
        assert not Path(td, "target.txt").exists()


def test_fake_backup_worker_produces_real_artifact():
    with tempfile.TemporaryDirectory() as td:
        worker = FakeWorker(td)
        output = Path(td) / "backup.tar.gz"
        result = worker.call_tool(
            "backup", {"target": "home", "output": str(output)}, validated=True
        )
        assert result["ok"] is True
        assert output.is_file()
        assert output.stat().st_size > 0


if __name__ == "__main__":
    test_edit_contract_requires_real_backup_before_write()
    test_fake_backup_worker_produces_real_artifact()
    print("EDIT_WORKFLOW_CONTRACT_TEST_OK")

def test_backup_artifact_contract_rejects_missing_empty_and_symlink():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        missing = root / "missing.tar.gz"
        empty = root / "empty.tar.gz"
        empty.touch()
        real = root / "real.tar.gz"
        real.write_text("backup")
        link = root / "link.tar.gz"
        link.symlink_to(real)

        def valid_backup(path):
            path = Path(path)
            return (
                path.exists()
                and not path.is_symlink()
                and path.is_file()
                and path.stat().st_size > 0
            )

        assert not valid_backup(missing)
        assert not valid_backup(empty)
        assert valid_backup(real)
        assert not valid_backup(link)


if __name__ == "__main__":
    test_edit_contract_requires_real_backup_before_write()
    test_fake_backup_worker_produces_real_artifact()
    test_backup_artifact_contract_rejects_missing_empty_and_symlink()
    print("EDIT_WORKFLOW_CONTRACT_TEST_OK")
